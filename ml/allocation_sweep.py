"""How much of the day's alert budget should the rules get? (D92)

    python ml/allocation_sweep.py

The budget has always been spent **rules first**: every rule alert is raised,
and the model competes for whatever is left (D61b). The confusion report (D91)
showed what that costs. At 75 a day the rules take 45 alerts at precision
0.605, and the model alone at the same volume would catch more of the fraud it
has seen (incident recall 0.921 against 0.890). At 60 a day the rules take 45
of 53 and the desk's recall collapses to 0.690 against 0.882.

But recall on fraud the model has *seen* is the wrong single measure: the rules
exist for fraud it has not (D59, D82b). So this sweeps one knob — the share of
the budget reserved for rule alerts — and measures both, on the same rows:

* **seen fraud**: train on the oldest 75% of the corpus, test on the newest
  37 days with every fraud type present;
* **unseen fraud**: seven runs, each holding one fraud type out of training
  entirely, and asking what share of *that* type's incidents the desk catches.

The allocation under test, at every share:

1. A veto rule (sanctions, a known mule) always alerts. It is not discretionary.
2. Discretionary rule alerts are ranked by the model's probability and take at
   most ``share`` of the budget.
3. The model spends everything left, over every payment not already alerted —
   including rule-flagged ones it ranks highly, which therefore still get
   through on merit.

Share 1.0 is today's rules-first behaviour. Share 0.0 is the model alone with
vetoes. The criterion is stated before the run: **the smallest share whose mean
unseen-type recall is within one standard error of the best, and whose seen
recall is not below rules-first**. Smallest, because every alert a rule takes
is one the model could have spent, and a tie should go to the layer that is
measured to rank better.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from offline_rules import RULE_CONFIGS, compute_signals, rule_masks  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

from evaluate_system import CREDIT_FEATURES  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
OUT = REPO_ROOT / "ml" / "artifacts" / "allocation-sweep.json"
BUDGETS = (120, 75, 60)
SHARES = [round(s, 2) for s in np.arange(0.0, 1.01, 0.05)]
VETO = ("SANCTIONED_BENEFICIARY", "KNOWN_MULE_BENEFICIARY")
TYPOLOGIES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING", "SOCIAL_ENGINEERING",
              "SIM_SWAP", "DORMANT_ACCOUNT", "CARD_CLONING")


def allocate(p: np.ndarray, rules: np.ndarray, veto: np.ndarray, *, total: int, share: float) -> np.ndarray:
    """Which payments alert, given a budget of ``total`` and a rule share."""
    flag = veto.copy()
    quota = max(int(round(total * share)) - int(flag.sum()), 0)
    discretionary = np.flatnonzero(rules & ~flag)
    if quota and len(discretionary):
        chosen = discretionary[np.argsort(-p[discretionary], kind="stable")[:quota]]
        flag[chosen] = True
    left = total - int(flag.sum())
    if left > 0:
        rest = np.flatnonzero(~flag)
        flag[rest[np.argsort(-p[rest], kind="stable")[:left]]] = True
    return flag


def incident_recall(flag: np.ndarray, fraud: np.ndarray, inc: np.ndarray) -> float:
    incidents = {i for i in inc[fraud] if i}
    caught = {i for i in inc[flag & fraud] if i}
    return len(caught) / max(len(incidents), 1)


def seen_pass(d, keep, meta) -> dict:
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
    order = np.argsort(occ)
    X, y, typ, occ, inc = (a[order] for a in (X, y, typ, occ, inc))
    m = {k: v[order] for k, v in meta.items()}
    cut = int(0.75 * len(y))
    days = (occ[-1] - occ[cut]).total_seconds() / 86400.0
    print(f"seen-fraud pass: training on {cut:,} rows ...", flush=True)
    p = TimeSplitCalibratedBooster().fit(X[:cut][:, keep], y[:cut],
                                         times=occ[:cut]).predict_proba(X[cut:][:, keep])[:, 1]
    yt, it, tt = y[cut:], inc[cut:], typ[cut:]
    fraud = yt == 1
    signals = compute_signals(X[cut:], {k: v[cut:] for k, v in m.items()}, FEATURE_NAMES, dict(RULE_CONFIGS))
    rules, by_code = rule_masks(signals)
    veto = np.zeros(len(yt), bool)
    for code in VETO:
        veto |= by_code.get(code, np.zeros(len(yt), bool))

    per_rule = {}
    for code, mask in sorted(by_code.items()):
        n = int(mask.sum())
        own = {i for i in it[mask & fraud] if i}
        per_rule[code] = {
            "alerts_per_day": round(n / days, 1),
            "precision": round(float((mask & fraud).sum()) / max(n, 1), 3),
            "incidents_touched": len(own),
        }

    credit = json.loads((REPO_ROOT / "ml" / "artifacts" / "receiving-side.json").read_text())["chosen"]
    out = {"days": round(days, 1), "rows": int(len(yt)), "per_rule": per_rule, "by_budget": {}}
    for budget in BUDGETS:
        total = int((budget - credit["alerts_per_day"]) * days)
        rows = []
        for share in SHARES:
            flag = allocate(p, rules, veto, total=total, share=share)
            tp = int((flag & fraud).sum())
            rows.append({
                "share": share,
                "rule_alerts_per_day": round(float((flag & rules).sum()) / days, 1),
                "incident_recall": round(incident_recall(flag, fraud, it), 4),
                "precision": round(tp / max(int(flag.sum()), 1), 4),
                "false_alerts_per_day": round(float((flag & ~fraud).sum()) / days, 1),
                "per_type": {t: round(incident_recall(flag, fraud & (tt == t), it), 3) for t in TYPOLOGIES},
            })
        out["by_budget"][budget] = rows
    return out


def unseen_pass(d, keep, meta, budget: int) -> dict:
    """Seven runs: each fraud type held out of training entirely (D10b)."""
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
    span = (occ.max() - occ.min()).total_seconds() / 86400.0
    credit = json.loads((REPO_ROOT / "ml" / "artifacts" / "receiving-side.json").read_text())["chosen"]
    per_type: dict[str, dict] = {}
    for held_out in TYPOLOGIES:
        rng = np.random.default_rng(20260909)
        legit = np.flatnonzero(y == 0)
        rng.shuffle(legit)
        cut = int(0.75 * len(legit))
        train = np.concatenate([legit[:cut], np.flatnonzero((y == 1) & (typ != held_out))])
        test = np.concatenate([legit[cut:], np.flatnonzero((y == 1) & (typ == held_out))])
        test.sort()
        print(f"unseen pass: holding out {held_out} ({len(train):,} train rows) ...", flush=True)
        model = TimeSplitCalibratedBooster().fit(X[train][:, keep], y[train], times=occ[train])
        p = model.predict_proba(X[test][:, keep])[:, 1]
        yt, it, tt = y[test], inc[test], typ[test]
        fraud = yt == 1
        signals = compute_signals(X[test], {k: v[test] for k, v in meta.items()}, FEATURE_NAMES, dict(RULE_CONFIGS))
        rules, by_code = rule_masks(signals)
        veto = np.zeros(len(yt), bool)
        for code in VETO:
            veto |= by_code.get(code, np.zeros(len(yt), bool))
        days = max(span * 0.25, 1.0)
        total = int((budget - credit["alerts_per_day"]) * days)
        held = fraud & (tt == held_out)
        per_type[held_out] = {
            "incidents": len({i for i in it[held] if i}),
            "by_share": {str(s): round(incident_recall(allocate(p, rules, veto, total=total, share=s), held, it), 4)
                         for s in SHARES},
        }
        print("   " + json.dumps(per_type[held_out]["by_share"]), flush=True)
    return {"budget": budget, "per_type": per_type}


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    keep = [j for j, n in enumerate(FEATURE_NAMES) if n not in CREDIT_FEATURES]
    meta = {k: d[k] for k in ("instrument", "channel", "ip_region", "has_beneficiary")}
    report = {"seen": seen_pass(d, keep, meta), "unseen": unseen_pass(d, keep, meta, 75)}

    # The stated criterion.
    unseen = report["unseen"]["per_type"]
    means = {s: float(np.mean([unseen[t]["by_share"][str(s)] for t in TYPOLOGIES])) for s in SHARES}
    se = {s: float(np.std([unseen[t]["by_share"][str(s)] for t in TYPOLOGIES], ddof=1) / np.sqrt(len(TYPOLOGIES)))
          for s in SHARES}
    best = max(means, key=lambda s: means[s])
    seen75 = {r["share"]: r for r in report["seen"]["by_budget"][75]}
    floor = seen75[1.0]["incident_recall"]
    ok = [s for s in SHARES if means[s] >= means[best] - se[best] and seen75[s]["incident_recall"] >= floor]
    chosen = min(ok) if ok else best
    report["choice"] = {
        "criterion": "smallest rule share within one standard error of the best unseen-type mean, "
                     "and no worse than rules-first on seen fraud",
        "unseen_mean_by_share": {str(s): round(means[s], 4) for s in SHARES},
        "unseen_se_at_best": round(se[best], 4),
        "best_unseen_share": best,
        "eligible": ok,
        "chosen_share": chosen,
        "at_75": {"rules_first": seen75[1.0], "chosen": seen75[chosen]},
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["choice"], indent=2)[:2000])


if __name__ == "__main__":
    main()

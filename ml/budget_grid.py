"""Tune the velocity rule and the model threshold together, under one budget.

    python ml/budget_grid.py

Plain English
-------------
The desk can review 120 alerts a day. Two things spend that budget: the
"five or more payments in an hour" rule, and the model. Measured on its own,
the rule at its current setting raises 175 alerts a day — 146% of the entire
budget — and 84% of those are wrong. So the model's threshold cannot be chosen
in isolation; it has to be chosen *together* with the rule's.

This tries every combination of rule setting and model threshold that keeps
the whole system at or under 120 a day, and reports which combination catches
the most fraud incidents.

It simulates live operation, not the held-out-crime test: the model is trained
on the oldest 75% of the corpus by time and scored on the newest 25%, with
every kind of fraud present, because that is what the running system sees.

The card-testing rule is kept on throughout. It raises 17 alerts a day at a
precision of 0.998 — almost nothing it flags is wrong — so it is the cheapest
fraud detection in the whole system and is never the thing to cut.

Approximation, stated: alerts are modelled as "model above threshold, or an
escalating rule fired". The suppression rule is ignored, which slightly
overstates volume, so any setting this recommends is conservative.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

BUDGET = 120
CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
TYPES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    # Load each array once — indexing an NpzFile decompresses it every time.
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]

    print("reading instruments ...", flush=True)
    inst = np.array([json.loads(line)["instrument"]
                     for line in open(REPO_ROOT / "ml" / "data" / "corpus.jsonl", encoding="utf-8")])

    order = np.argsort(occ)
    X, y, typ, occ, inc, inst = X[order], y[order], typ[order], occ[order], inc[order], inst[order]
    cut = int(0.75 * len(y))
    test_days = (occ[-1] - occ[cut]).total_seconds() / 86400.0

    print(f"training on the oldest 75% ({cut:,} rows) ...", flush=True)
    model = TimeSplitCalibratedBooster().fit(X[:cut], y[:cut], times=occ[:cut])
    p = model.predict_proba(X[cut:])[:, 1]

    Xt, yt, tt, it, nt = X[cut:], y[cut:], typ[cut:], inc[cut:], inst[cut:]
    col = lambda name: Xt[:, FEATURE_NAMES.index(name)]  # noqa: E731
    velocity = col("txn_count_1h_account")
    card = (nt == "CARD") & (col("decline_rate_24h_account") >= 0.5) & (col("failed_attempts_1h_account") >= 3)

    totals = {t: len(set(it[tt == t])) for t in TYPES}
    print(f"test: newest 25%, {len(yt):,} rows over {test_days:.1f} days, "
          f"incidents {totals}\n")

    allowed = BUDGET * test_days
    rows = []
    for k in (None, 5, 6, 7, 8, 9, 10, 12, 15):
        rule = card | (velocity >= k) if k else card.copy()
        spare = allowed - rule.sum()
        if spare < 0:
            rows.append({"velocity_min": k, "feasible": False,
                         "rules_per_day": round(rule.sum() / test_days, 1)})
            continue
        # The model spends what is left, among payments the rules did not flag.
        free = np.flatnonzero(~rule)
        ranked = free[np.argsort(-p[free], kind="stable")]
        model_flag = np.zeros(len(yt), bool)
        model_flag[ranked[: int(spare)]] = True
        system = rule | model_flag

        caught = {t: len(set(it[system & (tt == t)])) for t in TYPES}
        recall = {t: caught[t] / totals[t] for t in TYPES}
        rows.append({
            "velocity_min": k,
            "feasible": True,
            "rules_per_day": round(rule.sum() / test_days, 1),
            "model_per_day": round(model_flag.sum() / test_days, 1),
            "system_per_day": round(system.sum() / test_days, 1),
            "precision": round(float((system & (yt == 1)).sum() / max(system.sum(), 1)), 3),
            "incidents_caught": caught,
            "incident_recall": {t: round(v, 3) for t, v in recall.items()},
            "mean_incident_recall": round(float(np.mean(list(recall.values()))), 4),
        })

    print(f"{'velocity rule':<16}{'rules/day':>10}{'model/day':>10}{'total':>8}"
          f"{'precision':>11}   ATO    MULE   CARD   mean")
    for r in rows:
        label = "off" if r["velocity_min"] is None else f">= {r['velocity_min']}"
        if not r["feasible"]:
            print(f"{label:<16}{r['rules_per_day']:>10}   — rules alone exceed the budget")
            continue
        ir = r["incident_recall"]
        print(f"{label:<16}{r['rules_per_day']:>10}{r['model_per_day']:>10}{r['system_per_day']:>8}"
              f"{r['precision']:>11}  {ir['ACCOUNT_TAKEOVER']:.3f}  {ir['MULE_FANOUT']:.3f}  "
              f"{ir['CARD_TESTING']:.3f}  {r['mean_incident_recall']:.3f}")

    best = max((r for r in rows if r["feasible"]), key=lambda r: r["mean_incident_recall"])
    print(f"\nbest within {BUDGET}/day: velocity rule "
          f"{'off' if best['velocity_min'] is None else '>= ' + str(best['velocity_min'])}, "
          f"mean incident recall {best['mean_incident_recall']:.3f}")

    out = REPO_ROOT / "ml" / "artifacts" / "budget-grid.json"
    out.write_text(json.dumps({"budget_per_day": BUDGET, "test_days": round(test_days, 1),
                               "incidents": totals, "grid": rows, "best": best}, indent=2),
                   encoding="utf-8")
    print(f"written to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

"""The budget menu: what each alert budget buys, in the units banks benchmark (WP-09).

    python ml/budget_menu.py

Plain English
-------------
Banks do not compare fraud systems on counts of false alarms. They compare on
two numbers:

* **False alerts per confirmed fraud incident** — how many legitimate payments
  an analyst looks at for every real fraud found. Published best-in-class banks
  sit at 12 to 14 to 1; strong banks run up to and beyond 30 to 1.
* **Value detection rate** — the share of the *money* in fraud that the system
  flagged. A system that catches many small frauds and misses the large ones
  looks good on counts and fails on this.

This measures both at the three budgets the team is choosing between (D67c):
120 a day (today), 75 and 60. It re-runs the time-ordered test behind
``budget-sweep-gated.json`` — model trained on the oldest 75% of the corpus,
scored on the newest 30 days with every fraud type present — and adds the fraud
value, which that sweep did not record. With ``--check-sweep`` it stops if the
false alarms it measures disagree with the sweep; that check only means
something on the pre-D77 corpus the sweep was run on.

D77: the takeover sequence rule joins the offline rule mask, and
``--without-events`` measures the same corpus without it.

Held-out recall (fraud the model never saw) is carried over from
``system-evaluation*.json`` rather than re-measured: it needs one model per
hidden fraud type and does not change with this script.

Approximation, stated (as in ``budget_grid.py``): alerts are "an escalating
rule fired, or the model is in the top of what the budget has left". The
suppressing rules and list rules are not modelled offline.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from offline_rules import RULE_CONFIGS, compute_signals, rule_masks  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402
from train import TYPOLOGIES  # noqa: E402

BUDGETS = (120, 75, 60)
TYPES = TYPOLOGIES
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
CORPUS = REPO_ROOT / "ml" / "data"
HELD_OUT = {120: "system-evaluation.json", 75: "system-evaluation-75.json", 60: "system-evaluation-60.json"}
HELD_OUT_WITHOUT_EVENTS = {b: f.replace(".json", "-without-events.json") for b, f in HELD_OUT.items()}
HELD_OUT_WITHOUT_CREDITS = {b: f.replace(".json", "-without-credits.json") for b, f in HELD_OUT.items()}
FULL_SYSTEM_ARM = "rules + gradient boosting (ours)"
# The sweep rounds to 0.1 a day; anything further apart is a different measurement.
SWEEP_TOLERANCE = 0.2


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def held_out(budget: int, without_events: bool = False, without_credits: bool = False) -> dict | None:
    table = HELD_OUT_WITHOUT_EVENTS if without_events else HELD_OUT
    name = table[budget]
    if not (ARTIFACTS / name).exists():
        return None
    evaluation = json.loads((ARTIFACTS / name).read_text(encoding="utf-8"))
    if evaluation.get("feature_spec") != FEATURE_SPEC_VERSION:
        # Written before D77, on the previous corpus. Its recall belongs to a
        # different bank and must not sit in a row measured on this one.
        return None
    out = {}
    for t in TYPES:
        arm = next(a for a in evaluation["per_typology"][t] if a["arm"] == FULL_SYSTEM_ARM)
        out[t] = {"recall": round(arm["incident_recall"], 3), "ci95": arm["ci95"]}
    return {"per_typology": out, "mean": round(evaluation["mean_incident_recall"][FULL_SYSTEM_ARM], 3)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-sweep", action="store_true",
                        help="stop unless false alarms match budget-sweep-gated.json (pre-D77 corpus only)")
    parser.add_argument("--without-events", action="store_true", help="D77 ablation: no sequence rule")
    parser.add_argument("--without-credits", action="store_true",
                        help="D78: no credit alerts reserved (the receiving side switched off)")
    parser.add_argument("--no-second-leg", action="store_true",
                        help="D79 ablation: without the rule on the payment that moves received money on")
    parser.add_argument("--without-rule", action="append", help="D82 ablation: switch a rule off (repeatable)")
    parser.add_argument("--out", default=str(ARTIFACTS / "budget-menu.json"))
    args = parser.parse_args()

    d = np.load(CORPUS / "corpus.features.npz", allow_pickle=True)
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
    names = list(FEATURE_NAMES)
    if args.without_events:
        # The model is trained without the event features too, not only the rule.
        from evaluate_system import EVENT_FEATURES

        keep = [j for j, n in enumerate(names) if n not in EVENT_FEATURES]
        X, names = X[:, keep], [names[j] for j in keep]
    # D78: the model is not given the receiving-side features.
    from evaluate_system import CREDIT_FEATURES

    keep = [j for j, n in enumerate(names) if n not in CREDIT_FEATURES]
    X, names = X[:, keep], [names[j] for j in keep]

    # D82: the transaction fields the rules read travel in the cache.
    meta_all = {k: d[k] for k in ("instrument", "channel", "ip_region", "has_beneficiary")}
    amount = d["amount_minor"]

    order = np.argsort(occ)
    X, y, typ, occ, inc, amount = (a[order] for a in (X, y, typ, occ, inc, amount))
    meta_all = {k: v[order] for k, v in meta_all.items()}
    cut = int(0.75 * len(y))
    days = (occ[-1] - occ[cut]).total_seconds() / 86400.0

    print(f"training on the oldest 75% ({cut:,} rows) ...", flush=True)
    p = TimeSplitCalibratedBooster().fit(X[:cut], y[:cut], times=occ[:cut]).predict_proba(X[cut:])[:, 1]

    yt, tt, it, at = y[cut:], typ[cut:], inc[cut:], amount[cut:]
    # D82: the shipped rules, run by the real engine over the full feature matrix
    # (the rules read features the model is not given, D78b), one copy for every script.
    configs = dict(RULE_CONFIGS)
    if args.without_events:
        from evaluate_system import EVENT_RULES

        configs = {k: v for k, v in configs.items() if k not in EVENT_RULES}
    if args.no_second_leg:
        configs.pop("SECOND_LEG_ONWARD_PAYMENT", None)
    for code in args.without_rule or []:
        configs.pop(code, None)
    print("evaluating the rules on the test period ...", flush=True)
    full = d["X"][order][cut:]
    signals = compute_signals(full, {k: v[cut:] for k, v in meta_all.items()}, FEATURE_NAMES, configs)
    rules, by_code = rule_masks(signals)
    for code, mask in sorted(by_code.items()):
        print(f"  {code:<34}{mask.sum() / days:7.1f} a day", flush=True)

    # D78: credits raise alerts too, from the same desk. Their measured volume is
    # reserved first, so the model only spends what payments and credits leave.
    credit_alerts_per_day = 0.0
    receiving = ARTIFACTS / "receiving-side.json"
    if not args.without_credits and receiving.exists():
        credit_alerts_per_day = json.loads(receiving.read_text(encoding="utf-8"))["chosen"]["alerts_per_day"]
        print(f"reserving {credit_alerts_per_day} credit alerts a day for the receiving side", flush=True)

    fraud = yt == 1
    totals = {t: len(set(it[tt == t])) for t in TYPES}
    incidents = sum(totals.values())
    fraud_value = int(at[fraud].sum())
    print(f"test: {len(yt):,} rows over {days:.1f} days, incidents {totals}\n", flush=True)

    sweep = {r["budget"]: r for r in json.loads((ARTIFACTS / "budget-sweep-gated.json").read_text(encoding="utf-8"))}

    options = []
    for budget in BUDGETS:
        spare = int((budget - credit_alerts_per_day) * days - rules.sum())
        free = np.flatnonzero(~rules)
        ranked = free[np.argsort(-p[free], kind="stable")]
        model_flag = np.zeros(len(yt), bool)
        model_flag[ranked[: max(spare, 0)]] = True
        system = rules | model_flag

        false_day = float((system & ~fraud).sum()) / days
        expected = sweep[budget]["false_alarms_day"]
        if args.check_sweep and abs(false_day - expected) > SWEEP_TOLERANCE:
            raise SystemExit(f"budget {budget}: {false_day:.1f} false alarms a day, "
                             f"budget-sweep-gated.json says {expected}. Not the D67c measurement; stopping.")

        caught_incidents = {i for i in it[system & fraud] if i}
        caught = {t: len(set(it[system & fraud & (tt == t)])) for t in TYPES}
        # Transaction value: money on the fraud payments that were themselves
        # alerted. Incident value: all the money in incidents with at least one
        # alert, since one alert opens the case (D24a). The first is the floor.
        in_caught = fraud & np.isin(it, list(caught_incidents))
        options.append({
            "budget_per_day": budget,
            "alerts_per_day": round(float(system.sum()) / days + credit_alerts_per_day, 1),
            "credit_alerts_per_day": credit_alerts_per_day,
            "false_alerts_per_day": round(false_day, 1),
            "incidents_per_day": round(incidents / days, 2),
            "false_alerts_per_incident": round(false_day / (incidents / days), 1),
            "seen_fraud": {t: {"caught": caught[t], "incidents": totals[t], "ci95": wilson(caught[t], totals[t])}
                           for t in TYPES},
            "value_detection_rate": round(float(at[system & fraud].sum()) / fraud_value, 3),
            "value_in_caught_incidents": round(float(at[in_caught].sum()) / fraud_value, 3),
            # D79: the money in mule rings that the system put in front of a person,
            # payment by payment: what a hold on the onward payment could keep.
            "mule_value_detection_rate": round(
                float(at[system & fraud & (tt == "MULE_FANOUT")].sum())
                / max(float(at[fraud & (tt == "MULE_FANOUT")].sum()), 1.0), 3),
            # D82: money flagged, fraud type by fraud type.
            "value_detection_rate_by_type": {
                t: round(float(at[system & fraud & (tt == t)].sum()) / max(float(at[fraud & (tt == t)].sum()), 1.0), 3)
                for t in TYPES},
            "rule_alerts_per_day": {code: round(float(mask.sum()) / days, 1) for code, mask in sorted(by_code.items())},
            "held_out": held_out(budget, args.without_events, args.without_credits),
        })

    print(f"{'budget':>6}{'false/day':>10}{'ratio':>8}{'VDR':>7}{'mule VDR':>9}{'VDR inc':>9}   seen, by type   held-out mean")
    for o in options:
        s = o["seen_fraud"]
        print(f"{o['budget_per_day']:>6}{o['false_alerts_per_day']:>10}{o['false_alerts_per_incident']:>7}:1"
              f"{o['value_detection_rate']:>7}{o['mule_value_detection_rate']:>9}{o['value_in_caught_incidents']:>9}   "
              + " ".join(f"{s[t]['caught']}/{s[t]['incidents']}" for t in TYPES)
              + f"   {o['held_out']['mean'] if o['held_out'] else '-'}")

    out = Path(args.out)
    out.write_text(json.dumps({
        "measured": "time-ordered test: train oldest 75%, score newest 30 days, every fraud type present; "
                    "held-out recall from system-evaluation*.json"
                    + ("; without event features or the sequence rule (D77 ablation)" if args.without_events else ""),
        "test_days": round(days, 1),
        "fraud_value_minor": fraud_value,
        "benchmarks": {
            "false_alerts_per_incident": {"best_in_class": [12, 14], "strong_banks_up_to": 30,
                                          "source": "Feedzai, How Top Banks Benchmark Fraud Performance"},
            "value_detection_rate": {"market_leading": [0.60, 0.70],
                                     "source": "Feedzai, How Top Banks Benchmark Fraud Performance"},
        },
        "options": options,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()

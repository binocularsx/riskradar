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
value, which that sweep did not record. It stops if the false alarms it
measures disagree with the sweep, so the menu can never drift from D67c.

Held-out recall (fraud the model never saw) is carried over from
``system-evaluation*.json`` rather than re-measured: it needs one model per
hidden fraud type and does not change with this script.

Approximation, stated (as in ``budget_grid.py``): alerts are "an escalating
rule fired, or the model is in the top of what the budget has left". The
suppressing rules and list rules are not modelled offline.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

BUDGETS = (120, 75, 60)
TYPES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
CORPUS = REPO_ROOT / "ml" / "data"
HELD_OUT = {120: "system-evaluation.json", 75: "system-evaluation-75.json", 60: "system-evaluation-60.json"}
FULL_SYSTEM_ARM = "rules + gradient boosting (ours)"
# The sweep rounds to 0.1 a day; anything further apart is a different measurement.
SWEEP_TOLERANCE = 0.2


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def held_out(budget: int) -> dict:
    evaluation = json.loads((ARTIFACTS / HELD_OUT[budget]).read_text(encoding="utf-8"))
    out = {}
    for t in TYPES:
        arm = next(a for a in evaluation["per_typology"][t] if a["arm"] == FULL_SYSTEM_ARM)
        out[t] = {"recall": round(arm["incident_recall"], 3), "ci95": arm["ci95"]}
    return {"per_typology": out, "mean": round(evaluation["mean_incident_recall"][FULL_SYSTEM_ARM], 3)}


def main() -> None:
    d = np.load(CORPUS / "corpus.features.npz", allow_pickle=True)
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]

    print("reading instruments and amounts ...", flush=True)
    inst, amount = [], []
    with open(CORPUS / "corpus.jsonl", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            inst.append(row["instrument"])
            amount.append(row["amount_minor"])
    inst, amount = np.array(inst), np.array(amount, dtype=np.int64)

    order = np.argsort(occ)
    X, y, typ, occ, inc, inst, amount = (a[order] for a in (X, y, typ, occ, inc, inst, amount))
    cut = int(0.75 * len(y))
    days = (occ[-1] - occ[cut]).total_seconds() / 86400.0

    print(f"training on the oldest 75% ({cut:,} rows) ...", flush=True)
    p = TimeSplitCalibratedBooster().fit(X[:cut], y[:cut], times=occ[:cut]).predict_proba(X[cut:])[:, 1]

    Xt, yt, tt, it, nt, at = X[cut:], y[cut:], typ[cut:], inc[cut:], inst[cut:], amount[cut:]
    col = lambda name: Xt[:, FEATURE_NAMES.index(name)]  # noqa: E731
    count, first_seen = col("txn_count_1h_account"), col("beneficiary_first_seen_days")
    card = (nt == "CARD") & (col("decline_rate_24h_account") >= 0.5) & (col("failed_attempts_1h_account") >= 3)
    # D67, as seeded: 5+ to a destination new to the bank within a day; 10+ with none.
    velocity = ((count >= 5) & (first_seen >= 0) & (first_seen < 1)) | ((count >= 10) & (first_seen < 0))
    rules = card | velocity

    fraud = yt == 1
    totals = {t: len(set(it[tt == t])) for t in TYPES}
    incidents = sum(totals.values())
    fraud_value = int(at[fraud].sum())
    print(f"test: {len(yt):,} rows over {days:.1f} days, incidents {totals}\n", flush=True)

    sweep = {r["budget"]: r for r in json.loads((ARTIFACTS / "budget-sweep-gated.json").read_text(encoding="utf-8"))}

    options = []
    for budget in BUDGETS:
        spare = int(budget * days - rules.sum())
        free = np.flatnonzero(~rules)
        ranked = free[np.argsort(-p[free], kind="stable")]
        model_flag = np.zeros(len(yt), bool)
        model_flag[ranked[: max(spare, 0)]] = True
        system = rules | model_flag

        false_day = float((system & ~fraud).sum()) / days
        expected = sweep[budget]["false_alarms_day"]
        if abs(false_day - expected) > SWEEP_TOLERANCE:
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
            "alerts_per_day": round(float(system.sum()) / days, 1),
            "false_alerts_per_day": round(false_day, 1),
            "incidents_per_day": round(incidents / days, 2),
            "false_alerts_per_incident": round(false_day / (incidents / days), 1),
            "seen_fraud": {t: {"caught": caught[t], "incidents": totals[t], "ci95": wilson(caught[t], totals[t])}
                           for t in TYPES},
            "value_detection_rate": round(float(at[system & fraud].sum()) / fraud_value, 3),
            "value_in_caught_incidents": round(float(at[in_caught].sum()) / fraud_value, 3),
            "held_out": held_out(budget),
        })

    print(f"{'budget':>6}{'false/day':>10}{'ratio':>8}{'VDR':>7}{'VDR inc':>9}   seen ATO  MULE  CARD   held-out mean")
    for o in options:
        s = o["seen_fraud"]
        print(f"{o['budget_per_day']:>6}{o['false_alerts_per_day']:>10}{o['false_alerts_per_incident']:>7}:1"
              f"{o['value_detection_rate']:>7}{o['value_in_caught_incidents']:>9}   "
              f"{s['ACCOUNT_TAKEOVER']['caught']:>7}{s['MULE_FANOUT']['caught']:>6}{s['CARD_TESTING']['caught']:>6}"
              f"   {o['held_out']['mean']}")

    out = ARTIFACTS / "budget-menu.json"
    out.write_text(json.dumps({
        "measured": "time-ordered test: train oldest 75%, score newest 30 days, every fraud type present; "
                    "held-out recall from system-evaluation*.json",
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
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

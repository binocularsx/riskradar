"""What spending the alert budget by tier buys (WP-08, D80).

    python ml/tiers.py

Today every alert goes to an analyst and counts against the 75 a day. Some
alerts do not need an investigation to know what to do: a burst of declined
card probes (precision 0.998) calls for the card to be blocked, and a takeover
sequence (0.96) is exactly what CBN's 24-hour watch-list flag and customer call
exist for. This measures a three-tier policy against today's single queue, on
the time-ordered test the budget menu uses (train oldest 75%, score the newest
30 days, every fraud type present):

* MACHINE_ACTION: named high-precision signals. The system takes the named
  action (a hold directive, a flag) and a person confirms by contact; they do
  not count against analyst review capacity.
* HUMAN_REVIEW: everything else the rules raise, plus the model's highest
  probabilities, up to the review budget (75 a day less the credit alerts).
* AUTO_CLOSE: a candidate only. The lowest-probability model alerts in the
  review pool, measured for what closing them unread would miss.

Reported per arm: machine actions a day and their precision; analyst reviews
a day and false alerts among them; incidents caught per fraud type; share of
fraud value detected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from riskradar.features.spec import FEATURE_NAMES, MODEL_FEATURE_NAMES  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"
DATA = REPO_ROOT / "ml" / "data"
TYPES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")
BUDGET = 75
MACHINE_SIGNALS = ("CARD_TESTING_PROBES", "ACCOUNT_TAKEOVER_SEQUENCE")


def main() -> None:
    d = np.load(DATA / "corpus.features.npz", allow_pickle=True)
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
    inst, amount = [], []
    with open(DATA / "corpus.jsonl", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            inst.append(r["instrument"])
            amount.append(r["amount_minor"])
    inst, amount = np.array(inst), np.array(amount, dtype=np.int64)
    order = np.argsort(occ)
    X, y, typ, occ, inc, inst, amount = (a[order] for a in (X, y, typ, occ, inc, inst, amount))
    cut = int(0.75 * len(y))
    days = (occ[-1] - occ[cut]).total_seconds() / 86400.0
    model_cols = [FEATURE_NAMES.index(n) for n in MODEL_FEATURE_NAMES]

    print(f"training on the oldest 75% ({cut:,} rows) ...", flush=True)
    p = TimeSplitCalibratedBooster().fit(X[:cut][:, model_cols], y[:cut], times=occ[:cut]).predict_proba(
        X[cut:][:, model_cols])[:, 1]

    Xt, yt, tt, it, nt, at = X[cut:], y[cut:], typ[cut:], inc[cut:], inst[cut:], amount[cut:]
    col = lambda n: Xt[:, FEATURE_NAMES.index(n)]  # noqa: E731
    count, first_seen = col("txn_count_1h_account"), col("beneficiary_first_seen_days")
    signals = {
        "CARD_TESTING_PROBES": (nt == "CARD") & (col("decline_rate_24h_account") >= 0.5) & (col("failed_attempts_1h_account") >= 3),
        "VELOCITY_BURST_1H": ((count >= 5) & (first_seen >= 0) & (first_seen < 1)) | ((count >= 10) & (first_seen < 0)),
        "ACCOUNT_TAKEOVER_SEQUENCE": ((((col("device_bound_hours") >= 0) & (col("device_bound_hours") < 24)).astype(int)
                                      + (col("sim_changed_hours") < 24) + (col("credential_changed_hours") < 24)
                                      + (col("failed_logins_1h_subject") >= 3)) >= 2) & (col("beneficiary_is_new_to_account") == 1),
        "SECOND_LEG_ONWARD_PAYMENT": (col("distinct_remitters_24h_account") >= 3) & (col("inbound_count_ratio_24h_vs_daily_mean_30d") >= 5)
                                     & (col("minutes_since_last_credit") <= 180) & (col("pass_through_ratio_24h") >= 0.5),
    }
    rules = np.zeros(len(yt), bool)
    for m in signals.values():
        rules |= m
    machine = np.zeros(len(yt), bool)
    for code in MACHINE_SIGNALS:
        machine |= signals[code]

    receiving = json.loads((ARTIFACTS / "receiving-side.json").read_text(encoding="utf-8"))["chosen"]
    credit_per_day = receiving["alerts_per_day"]
    fraud = yt == 1
    totals = {t: len(set(it[tt == t])) for t in TYPES}
    fraud_value = float(at[fraud].sum())

    def fill(pool_rules: np.ndarray, review_per_day: float) -> np.ndarray:
        spare = int(review_per_day * days - pool_rules.sum())
        free = np.flatnonzero(~(pool_rules | machine_in_play))
        ranked = free[np.argsort(-p[free], kind="stable")]
        flag = np.zeros(len(yt), bool)
        flag[ranked[: max(spare, 0)]] = True
        return flag

    def report(name: str, reviewed: np.ndarray, machine_mask: np.ndarray, model_flag: np.ndarray) -> dict:
        system = reviewed | machine_mask
        caught = {t: len(set(it[system & fraud & (tt == t)])) for t in TYPES}
        row = {
            "arm": name,
            "machine_actions_per_day": round(float(machine_mask.sum()) / days, 1),
            "machine_precision": round(float((machine_mask & fraud).sum()) / max(int(machine_mask.sum()), 1), 3),
            "reviews_per_day": round(float(reviewed.sum()) / days + credit_per_day, 1),
            "false_reviews_per_day": round(float((reviewed & ~fraud).sum()) / days + receiving["false_alerts_per_day"], 1),
            "incidents_caught": {t: f"{caught[t]}/{totals[t]}" for t in TYPES},
            "value_detection_rate": round(float(at[system & fraud].sum()) / fraud_value, 3),
            "model_reviews_per_day": round(float(model_flag.sum()) / days, 1),
        }
        print(f"{name:<38}{row['machine_actions_per_day']:>9}{row['machine_precision']:>8}{row['reviews_per_day']:>9}"
              f"{row['false_reviews_per_day']:>8}{row['value_detection_rate']:>7}   "
              + "  ".join(row["incidents_caught"][t] for t in TYPES))
        return row

    print(f"\ntest: {len(yt):,} payments over {days:.1f} days; incidents {totals}; "
          f"{credit_per_day} credit reviews a day reserved\n")
    print(f"{'arm':<38}{'machine':>9}{'prec':>8}{'reviews':>9}{'false':>8}{'VDR':>7}   ATO     MULE    CARD")
    review_budget = BUDGET - credit_per_day
    rows = []

    machine_in_play = np.zeros(len(yt), bool)
    single = fill(rules, review_budget)
    rows.append(report("one queue (today)", rules | single, np.zeros(len(yt), bool), single))

    machine_in_play = machine
    tiered_model = fill(rules & ~machine, review_budget)
    tiered_reviews = (rules & ~machine) | tiered_model
    rows.append(report("tiered: machine actions off the budget", tiered_reviews, machine, tiered_model))

    # Auto-close candidate: the lowest fifth of the model's review slots.
    model_idx = np.flatnonzero(tiered_model)
    low = model_idx[np.argsort(p[model_idx], kind="stable")][: len(model_idx) // 5]
    closed = np.zeros(len(yt), bool)
    closed[low] = True
    rows.append(report("tiered + auto-close lowest fifth", tiered_reviews & ~closed, machine, tiered_model & ~closed))
    # The O5 arm: take the machine tier off the queue and do NOT refill it. The
    # model keeps exactly today's review slots, so detection matches today while
    # analysts review less.
    kept_reviews = (rules & ~machine) | single
    rows.append(report("tiered: freed capacity kept", kept_reviews, machine, single))
    kept_idx = np.flatnonzero(single & ~rules)
    kept_low = kept_idx[np.argsort(p[kept_idx], kind="stable")][: len(kept_idx) // 5]
    kept_closed = np.zeros(len(yt), bool)
    kept_closed[kept_low] = True
    rows.append(report("tiered, kept + auto-close lowest fifth", kept_reviews & ~kept_closed, machine,
                       single & ~kept_closed))
    auto_close_cut = float(p[kept_low].max()) if len(kept_low) else 0.0
    kept_lost = {i for i in it[kept_closed & fraud] if i
                 and not (kept_reviews & ~kept_closed & fraud & (it == i)).any()
                 and not (machine & fraud & (it == i)).any()}
    print(f"\nauto-close cut (highest probability closed): {auto_close_cut:.6f}; "
          f"incidents lost in the kept arm: {len(kept_lost)}")

    closed_fraud_incidents = {i for i in it[closed & fraud] if i}
    auto_close = {
        "closed_per_day": round(float(closed.sum()) / days, 1),
        "fraud_payments_closed": int((closed & fraud).sum()),
        "incidents_touched": len(closed_fraud_incidents),
        "incidents_lost": len({i for i in closed_fraud_incidents
                               if not (tiered_reviews & ~closed & fraud & (it == i)).any()
                               and not (machine & fraud & (it == i)).any()}),
    }
    print(f"\nauto-close candidate: {auto_close}")

    out = ARTIFACTS / "tiers.json"
    out.write_text(json.dumps({"budget_per_day": BUDGET, "test_days": round(days, 1), "machine_signals": MACHINE_SIGNALS,
                               "credit_reviews_reserved_per_day": credit_per_day, "arms": rows,
                               "auto_close_candidate": auto_close,
                               "auto_close_probability_cut": auto_close_cut,
                               "incidents_lost_to_auto_close_in_kept_arm": len(kept_lost)}, indent=2), encoding="utf-8")
    print(f"written to {out}")


if __name__ == "__main__":
    main()

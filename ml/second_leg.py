"""Tune the second-leg rule on the payments that move received money on (D79).

    python ml/second_leg.py

The receiving-side playbook's central point is that the recoverable moment is
the onward payment, not the credit: a hold on the payment that moves the money
on keeps it in the bank. Every payment already carries its account's
receiving-side profile (D78), so the second leg is a rule on the outgoing
payment: the account took money from several senders on an unusual day, the
money arrived recently, and much of it is already leaving.

For each variant, over the payments after a 30-day warm-up (as D78c): alerts a
day, false alerts a day, precision, mule rings with at least one payment
flagged, and how many of those rings the credit-side fan-in rule had *not*
already caught, which is what the second leg adds. Nothing is trained; the
rule reads features only, and the held-out evaluation then judges the system.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402

DATA = REPO_ROOT / "ml" / "data"
WARM_UP_DAYS = 30
CHOSEN = (180, 0.5)  # max minutes since the last credit, min pass-through: as seeded


def main() -> None:
    d = np.load(DATA / "corpus.features.npz", allow_pickle=True)
    X, y, typ, inc, occ = d["X"], d["y"].astype(int), d["typology"], d["incident_id"], d["occurred_at"]
    measured_from = min(occ) + timedelta(days=WARM_UP_DAYS)
    keep = np.array([t >= measured_from for t in occ])
    X, y, typ, inc, occ = X[keep], y[keep], typ[keep], inc[keep], occ[keep]
    days = (max(occ) - measured_from).total_seconds() / 86400.0
    col = lambda n: X[:, FEATURE_NAMES.index(n)]  # noqa: E731

    senders, ratio = col("distinct_remitters_24h_account"), col("inbound_count_ratio_24h_vs_daily_mean_30d")
    since, through = col("minutes_since_last_credit"), col("pass_through_ratio_24h")
    fanin = (senders >= 3) & (ratio >= 5.0)
    mule_rings = {i for i in inc[(y == 1) & (typ == "MULE_FANOUT")] if i}

    credit_side = set()
    receiving = REPO_ROOT / "ml" / "artifacts" / "receiving-side.json"
    cd = DATA / "corpus.credits.features.npz"
    if cd.exists():
        c = np.load(cd, allow_pickle=True)
        cX, cy, cinc, cocc = c["X"], c["y"].astype(int), c["incident_id"], c["occurred_at"]
        ckeep = np.array([t >= measured_from for t in cocc])
        ccol = lambda n: cX[ckeep, FEATURE_NAMES.index(n)]  # noqa: E731
        flagged = (ccol("distinct_remitters_24h_account") >= 3) & (ccol("inbound_count_ratio_24h_vs_daily_mean_30d") >= 5.0)
        credit_side = {i for i in cinc[ckeep][flagged & (cy[ckeep] == 1)] if i}
    _ = receiving

    rows = []
    print(f"{len(y):,} payments over {days:.1f} days after a {WARM_UP_DAYS}-day warm-up; "
          f"{len(mule_rings)} mule rings; credit-side rule already caught {len(credit_side & mule_rings)}\n")
    print(f"{'within min':>10}{'pass >=':>8}{'alerts/day':>11}{'false/day':>10}{'precision':>10}{'rings':>9}{'new':>6}")
    for within in (60, 180, 360, 1440):
        for min_pass in (0.0, 0.3, 0.5, 0.8):
            flag = fanin & (since <= within) & (through >= min_pass)
            caught = {i for i in inc[flag & (y == 1)] if i} & mule_rings
            row = {
                "max_minutes_since_credit": within, "min_pass_through": min_pass,
                "alerts_per_day": round(float(flag.sum()) / days, 2),
                "false_alerts_per_day": round(float((flag & (y == 0)).sum()) / days, 2),
                "precision": round(float((flag & (y == 1)).sum()) / max(int(flag.sum()), 1), 3),
                "rings_caught": len(caught), "ring_recall": round(len(caught) / max(len(mule_rings), 1), 3),
                "rings_not_caught_on_credit": len(caught - credit_side),
                "chosen": (within, min_pass) == CHOSEN,
            }
            rows.append(row)
            print(f"{within:>10}{min_pass:>8}{row['alerts_per_day']:>11}{row['false_alerts_per_day']:>10}"
                  f"{row['precision']:>10}{row['rings_caught']:>5}/{len(mule_rings)}{row['rings_not_caught_on_credit']:>6}")

    out = REPO_ROOT / "ml" / "artifacts" / "second-leg.json"
    chosen = next((r for r in rows if r["chosen"]), None)
    out.write_text(json.dumps({"warm_up_days": WARM_UP_DAYS, "days": round(days, 1), "mule_rings": len(mule_rings),
                               "caught_on_credit": len(credit_side & mule_rings), "chosen": chosen,
                               "variants": rows}, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()

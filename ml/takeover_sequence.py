"""Tune the account-takeover sequence rule on the corpus, not by intuition (D77).

    python ml/takeover_sequence.py

The rule fires on a payment to a destination new to the account when a takeover
precursor is recent: the paying device freshly bound, a SIM or credential
change, or a burst of failed logins. Each precursor also happens to ordinary
customers, so the settings are chosen by what they cost and what they catch.

For each variant, on the whole corpus: alerts a day from the rule alone, false
alerts a day, precision, and the share of incidents of each fraud type with at
least one payment the rule flags. The rule reads features only, so no model is
trained and nothing here is held out: this chooses the rule, and the held-out
evaluation (``evaluate_system.py``) then judges the system it belongs to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
TYPES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    X, y, typ, inc, occ = d["X"], d["y"].astype(int), d["typology"], d["incident_id"], d["occurred_at"]
    days = (max(occ) - min(occ)).total_seconds() / 86400.0
    col = lambda name: X[:, FEATURE_NAMES.index(name)]  # noqa: E731
    new_dest = col("beneficiary_is_new_to_account") == 1
    device, sim, cred = col("device_bound_hours"), col("sim_changed_hours"), col("credential_changed_hours")
    failed = col("failed_logins_1h_subject")
    totals = {t: len(set(inc[typ == t])) for t in TYPES}

    def recent(values, hours):
        return (values >= 0) & (values < hours)

    variants = {}
    for hours in (6, 12, 24, 48):
        for min_failed in (3, 5):
            variants[f"any precursor < {hours}h, failed >= {min_failed}"] = new_dest & (
                recent(device, hours) | recent(sim, hours) | recent(cred, hours) | (failed >= min_failed))
    variants["device bound < 24h only"] = new_dest & recent(device, 24)
    variants["SIM changed < 24h only"] = new_dest & recent(sim, 24)
    variants["credential changed < 24h only"] = new_dest & recent(cred, 24)
    variants["failed logins >= 3 only"] = new_dest & (failed >= 3)
    variants["two precursors < 24h"] = new_dest & (
        (recent(device, 24).astype(int) + recent(sim, 24) + recent(cred, 24) + (failed >= 3)) >= 2)

    rows = []
    print(f"corpus {len(y):,} payments over {days:.1f} days; incidents {totals}\n")
    print(f"{'variant':<42}{'alerts/day':>11}{'false/day':>10}{'precision':>10}   ATO    MULE   CARD")
    for name, mask in variants.items():
        caught = {t: len(set(inc[mask & (y == 1) & (typ == t)])) for t in TYPES}
        row = {
            "variant": name,
            "alerts_per_day": round(float(mask.sum()) / days, 2),
            "false_alerts_per_day": round(float((mask & (y == 0)).sum()) / days, 2),
            "precision": round(float((mask & (y == 1)).sum()) / max(int(mask.sum()), 1), 3),
            "incident_recall": {t: round(caught[t] / totals[t], 3) for t in TYPES},
        }
        rows.append(row)
        r = row["incident_recall"]
        print(f"{name:<42}{row['alerts_per_day']:>11}{row['false_alerts_per_day']:>10}{row['precision']:>10}"
              f"  {r['ACCOUNT_TAKEOVER']:.3f}  {r['MULE_FANOUT']:.3f}  {r['CARD_TESTING']:.3f}")

    out = REPO_ROOT / "ml" / "artifacts" / "takeover-sequence.json"
    out.write_text(json.dumps({"days": round(days, 1), "incidents": totals, "variants": rows}, indent=2),
                   encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

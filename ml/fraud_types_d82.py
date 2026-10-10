"""Tune the four D82 rules on the corpus, and check every fraud type's legitimate twin.

    python ml/fraud_types_d82.py

For each rule, every variant in a small grid is measured on the whole corpus:
alerts a day, false alerts a day, precision, and the share of incidents of each
fraud type it touches. The rules read features only, so nothing is trained and
nothing is held out here; this chooses the settings, and the held-out
evaluation (``evaluate_system.py``) then judges the system with them.

The choice is made by a stated criterion, not by eye: the variant that catches
the most incidents of its own fraud type while raising at most
``MAX_FALSE_PER_DAY`` false alerts a day at precision of at least
``MIN_PRECISION``; ties go to fewer alerts. A rule no variant can satisfy is
reported as not earning a place, and is left to the model.

Speed: the grid runs as numpy conditions. The chosen variant is then re-run
through the real rules engine (``offline_rules.py``) on a sample and the two
must agree row for row, so the numpy copy cannot quietly differ from the rule.

Before any of that, a giveaway check (D57): for each new measurement, the share
of legitimate rows that one threshold could discard without losing a single
incident of the fraud type it was built for. A measurement that separates a
fraud type on its own is a simulator artefact, not detection.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from offline_rules import compute_signals  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from train import TYPOLOGIES  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
OUT = REPO_ROOT / "ml" / "artifacts" / "fraud-types-d82.json"
# One of four new rules sharing a 75-a-day desk: at most 5 false alerts a day,
# right at least a quarter of the time.
MAX_FALSE_PER_DAY = 5.0
MIN_PRECISION = 0.25
GIVEAWAY_LIMIT = 0.15  # D57: no single measurement may discard more than this for free


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    X, y, typ, inc, occ = d["X"], d["y"].astype(int), d["typology"], d["incident_id"], d["occurred_at"]
    instrument, channel, has_ben = d["instrument"], d["channel"], d["has_beneficiary"]
    days = (max(occ) - min(occ)).total_seconds() / 86400.0
    col = lambda name: X[:, FEATURE_NAMES.index(name)]  # noqa: E731
    fraud = y == 1
    totals = {t: len({i for i in inc[typ == t] if i}) for t in TYPOLOGIES}
    print(f"corpus {len(y):,} payments over {days:.1f} days; incidents {totals}\n")

    # --- giveaway check -----------------------------------------------------------
    giveaways = []
    # Compared within the population the fraud type lives in: a card cash-out is
    # compared with other card-present payments, not with transfers it could never
    # be; a scam with other transfers to a destination. A precursor that defines the
    # type (a SIM swap always changes the SIM) is reported as definitional: it can
    # separate the type by construction, in the world as in the simulator.
    transfer = has_ben
    card_present = (instrument == "CARD") & np.isin(channel, ("POS", "ATM", "AGENT"))
    everyone = np.ones(len(y), dtype=bool)
    definitional = {("sim_changed_hours", "SIM_SWAP")}
    for feature, target, population in (("beneficiary_distinct_senders_24h", "SOCIAL_ENGINEERING", transfer),
                                        ("sim_changed_hours", "SIM_SWAP", transfer),
                                        ("days_since_account_activity", "DORMANT_ACCOUNT", transfer),
                                        ("card_present_count_1h_account", "CARD_CLONING", card_present),
                                        ("region_is_new_to_subject", "CARD_CLONING", card_present),
                                        ("hour_of_day_local", "SIM_SWAP", everyone),
                                        ("hour_of_day_local", "CARD_CLONING", everyone)):
        v = col(feature)
        t_rows = fraud & (typ == target) & population
        # Keep everything on the side of the threshold where every incident of the
        # type still has at least one row; count what that discards for free.
        best = 0.0
        for side in ("low", "high"):
            per_incident = {}
            for i, value in zip(inc[t_rows], v[t_rows]):
                per_incident[i] = max(per_incident.get(i, -np.inf), value) if side == "low" else \
                    min(per_incident.get(i, np.inf), value)
            if not per_incident:
                continue
            cut = min(per_incident.values()) if side == "low" else max(per_incident.values())
            legit = v[~fraud & population]
            discarded = float(np.mean(legit < cut)) if side == "low" else float(np.mean(legit > cut))
            best = max(best, discarded)
        verdict = ("definitional" if (feature, target) in definitional
                   else "giveaway" if best > GIVEAWAY_LIMIT else "ok")
        giveaways.append({"feature": feature, "fraud_type": target, "legit_discarded_free": round(best, 4),
                          "verdict": verdict})
        print(f"giveaway check  {feature:<34} {target:<20} discards {best:6.1%} of comparable legitimate rows "
              f"for free  {verdict.upper()}")

    # --- grids ----------------------------------------------------------------------
    new_to_account = col("beneficiary_is_new_to_account") == 1
    grids = {
        "SCAM_BENEFICIARY_FANIN": ("SOCIAL_ENGINEERING", [
            ({"min_other_senders": o, "max_beneficiary_age_days": a, **({"min_amount_log10": m} if m else {}),
              **({"min_amount_ratio": r} if r else {})},
             new_to_account & (col("beneficiary_distinct_senders_24h") >= o)
             & (col("beneficiary_first_seen_days") >= 0) & (col("beneficiary_first_seen_days") <= a)
             & ((col("amount_log10") >= m) if m else True)
             & ((col("amount_ratio_to_account_p95_30d") >= r) if r else True))
            for o, a, m, r in itertools.product((1, 2, 3), (3, 7, 14), (None, 4.7), (None, 1.5, 2.5, 4.0))]),
        "SIM_SWAP_TRANSFER": ("SIM_SWAP", [
            ({"within_hours": h, **({"channels": list(c)} if c else {}), **({"min_amount_log10": m} if m else {}),
              **({"min_amount_ratio": r} if r else {})},
             new_to_account & (col("sim_changed_hours") >= 0) & (col("sim_changed_hours") < h)
             & (np.isin(channel, c) if c else True)
             & ((col("amount_log10") >= m) if m else True)
             & ((col("amount_ratio_to_account_p95_30d") >= r) if r else True))
            for h, c, m, r in itertools.product((6, 12, 24), (None, ("USSD", "MOBILE_APP")), (None, 4.3, 4.7),
                                                (None, 1.0, 1.5))]),
        "DORMANT_ACCOUNT_REACTIVATION": ("DORMANT_ACCOUNT", [
            ({"min_dormant_days": q, "min_amount_log10": m, **({} if r else {"require_new_destination": False})},
             (col("days_since_account_activity") >= q) & (col("amount_log10") >= m)
             & ((~has_ben | new_to_account) if r else True))
            for q, m, r in itertools.product((30, 60, 90, 180), (4.0, 4.7, 5.0, 5.3, 5.7), (True, False))]),
        "CARD_PRESENT_NEW_REGION_CASHOUT": ("CARD_CLONING", [
            ({"min_count_1h": c},
             (instrument == "CARD") & np.isin(channel, ("POS", "ATM", "AGENT"))
             & (col("region_is_new_to_subject") == 1) & (col("card_present_count_1h_account") >= c))
            for c in (2, 3, 4, 5)]),
    }

    report: dict = {"days": round(days, 1), "incidents": totals, "giveaway_check": giveaways,
                    "criterion": {"max_false_alerts_per_day": MAX_FALSE_PER_DAY, "min_precision": MIN_PRECISION},
                    "rules": {}}
    for code, (target, variants) in grids.items():
        rows = []
        for params, mask in variants:
            mask = np.asarray(mask, dtype=bool)
            caught = {t: len({i for i in inc[mask & fraud & (typ == t)] if i}) for t in TYPOLOGIES}
            rows.append({
                "params": params,
                "alerts_per_day": round(float(mask.sum()) / days, 2),
                "false_alerts_per_day": round(float((mask & ~fraud).sum()) / days, 2),
                "precision": round(float((mask & fraud).sum()) / max(int(mask.sum()), 1), 3),
                "target_incident_recall": round(caught[target] / max(totals[target], 1), 3),
                "incident_recall": {t: round(caught[t] / max(totals[t], 1), 3) for t in TYPOLOGIES},
            })
        eligible = [r for r in rows if r["false_alerts_per_day"] <= MAX_FALSE_PER_DAY and r["precision"] >= MIN_PRECISION]
        chosen = max(eligible, key=lambda r: (r["target_incident_recall"], -r["alerts_per_day"])) if eligible else None
        print(f"\n{code} (for {target}): {len(eligible)} of {len(rows)} variants meet the criterion")
        for r in sorted(rows, key=lambda r: -r["target_incident_recall"])[:6]:
            mark = " <- chosen" if r is chosen else ""
            print(f"  {json.dumps(r['params']):<70} {r['alerts_per_day']:>7}/day false {r['false_alerts_per_day']:>6}"
                  f" prec {r['precision']:.3f} recall {r['target_incident_recall']:.3f}{mark}")
        report["rules"][code] = {"target": target, "chosen": chosen, "variants": rows}

    # --- the numpy copy must be the engine ----------------------------------------------
    rng = np.random.default_rng(20260917)
    sample = rng.choice(len(y), size=min(len(y), 150_000), replace=False)
    configs = {code: {"enabled": True, "params": entry["chosen"]["params"]}
               for code, entry in report["rules"].items() if entry["chosen"]}
    signals = compute_signals(X[sample], {"instrument": instrument[sample], "channel": channel[sample],
                                          "ip_region": d["ip_region"][sample], "has_beneficiary": has_ben[sample]},
                              FEATURE_NAMES, configs)
    for code, (target, variants) in grids.items():
        entry = report["rules"][code]
        if not entry["chosen"]:
            continue
        mask = next(np.asarray(m, dtype=bool) for p, m in variants if p == entry["chosen"]["params"])[sample]
        engine = np.array([any(s.code == code for s in sig) for sig in signals])
        disagree = int((mask != engine).sum())
        entry["engine_check"] = {"sample": int(len(sample)), "disagreements": disagree}
        print(f"engine check {code}: {disagree} disagreements in {len(sample):,} rows")
        if disagree:
            raise SystemExit(f"{code}: the numpy grid is not the rule; fix before choosing parameters")

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten to {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

"""Derive thresholds backwards from the alert budget (D11d).

    python scripts/derive_thresholds.py --publish

Never "80 sounds high". The arithmetic runs the other way:

1. Take the probability distribution the active model actually produces on
   recent traffic.
2. Work out the daily transaction volume that traffic implies.
3. Find the probability at which alert volume equals the budget — analysts
   multiplied by reviewable alerts per day (75/day, D76).
4. Place REVIEW and HOLD at tighter slices of the same budget, so the bands
   below them stay inside it.

This only works because the model is **calibrated** (D11). An uncalibrated score
cannot be turned into a volume prediction, which is precisely why calibration is
not a nicety here: without it there is no way to answer "how many alerts will
this threshold produce tomorrow", and that question is the one that decides
whether the desk drowns.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

from riskradar.audit import chain  # noqa: E402
from riskradar.config import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive thresholds from the alert budget")
    parser.add_argument("--budget", type=int, default=None, help="alerts/day (default: app_config)")
    parser.add_argument("--publish", action="store_true", help="write a new threshold version")
    parser.add_argument("--min-sample", type=int, default=2000)
    parser.add_argument(
        "--last-days", type=float, default=None,
        help="measure only the most recent N days of traffic (default: all of it)",
    )
    args = parser.parse_args()

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        budget = args.budget
        if budget is None:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key = 'alert_budget_per_day'"
            ).fetchone()
            budget = int(row["value"]) if row else 75

        from riskradar.policy import calibration

        sample = calibration.load_sample(conn, last_days=args.last_days)
        try:
            from riskradar.policy.budget import load_config

            result = calibration.derive(sample, budget, min_sample=args.min_sample,
                                        rule_share=load_config(conn).rule_share)
        except calibration.CalibrationRefused as exc:
            print(f"REFUSING: {exc}")
            raise SystemExit(2) from exc

        # The budget is for the WHOLE SYSTEM, not for the model (D61b): the
        # rules' own alerts are spent first and the model competes for the rest.
        if result.get("note"):
            print(f"  note: {result['note']}\n")
        print("rule-driven alerts (raised even if the model scored zero):")
        print(f"  {result['rules_alone_per_day']:.1f}/day of a {budget}/day budget")
        for code, n in result["rules_per_day"].items():
            print(f"    {code:<28} {n:7.1f}/day")
        print()
        p_monitor, p_review, p_hold = result["p_monitor"], result["p_review"], result["p_hold"]
        implied = result["implied"]
        print("derivation")
        print(f"  scored sample        {result['sample']} decisions over {result['span_days']:.2f} days")
        print(f"  implied daily volume {result['daily_volume']:,.0f} transactions/day")
        print(f"  alert budget         {budget}/day  ({100 * result['alert_share']:.4f}% of traffic)")
        print()
        print(f"  p_monitor            {p_monitor:.12f}")
        print(f"  p_review             {p_review:.12f}")
        print(f"  p_hold               {p_hold:.12f}")
        print()
        print("  implied alert volume at these thresholds:")
        for key, value in implied.items():
            print(f"    {key:<30} {value}")
        p = sample.p

        if not args.publish:
            print("\n  (dry run — pass --publish to write a new threshold version)")
            return

        old = conn.execute(
            "SELECT version, p_monitor, p_review, p_hold FROM threshold_sets WHERE is_active"
        ).fetchone()
        version = conn.execute(
            "SELECT coalesce(max(version), 0) + 1 AS v FROM threshold_sets"
        ).fetchone()["v"]
        sys_uid = conn.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]

        conn.execute("UPDATE threshold_sets SET is_active = false WHERE is_active")
        conn.execute(
            """
            INSERT INTO threshold_sets
                (version, p_monitor, p_review, p_hold, alert_min_level, notes, is_active)
            VALUES (%s, %s, %s, %s, 'MEDIUM', %s, true)
            """,
            (
                version, p_monitor, p_review, p_hold,
                f"derived from a {budget}/day alert budget over {len(p)} scored decisions",
            ),
        )
        chain.append(
            conn,
            actor_user_id=sys_uid,
            action="THRESHOLDS_CHANGED",
            object_type="threshold_set",
            object_id=version,
            from_state=json.dumps(dict(old), default=str) if old else None,
            to_state=json.dumps(
                {"p_monitor": p_monitor, "p_review": p_review, "p_hold": p_hold}, default=str
            ),
            payload={
                "via": "scripts/derive_thresholds.py",
                "alert_budget_per_day": budget,
                "sample_size": len(p),
                "implied_volume": implied,
            },
        )
        conn.commit()
        print(f"\n  published threshold set v{version} (audited)")


if __name__ == "__main__":
    main()

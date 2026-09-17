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

import numpy as np

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

        rows = conn.execute(
            """
            SELECT d.p_fraud, t.occurred_at, d.signals
              FROM decisions d
              JOIN transactions t ON t.id = d.transaction_id
             WHERE d.rule_only_mode = false
               AND (%(last_days)s::float IS NULL
                    OR t.occurred_at >= (SELECT max(occurred_at) FROM transactions)
                                        - make_interval(secs => %(last_days)s::float * 86400))
             ORDER BY d.decided_at DESC
             LIMIT 500000
            """,
            {"last_days": args.last_days},
        ).fetchall()

        if len(rows) < args.min_sample:
            raise SystemExit(
                f"only {len(rows)} scored decisions — need at least {args.min_sample}. "
                "Seed history and let the worker drain first."
            )

        p = np.array([float(r["p_fraud"]) for r in rows])
        times = [r["occurred_at"] for r in rows]
        span_days = max((max(times) - min(times)).total_seconds() / 86400.0, 0.5)
        daily_volume = len(p) / span_days

        # The budget is for the WHOLE SYSTEM, not for the model (D61b).
        #
        # The first version solved the threshold from the model's scores alone.
        # But the rules raise alerts on their own — an ESCALATE rule lifts a
        # payment to HIGH whatever the model thinks — so the desk received the
        # model's 120 plus every rule alert on top: 1.5x to 2.2x the budget in
        # offline tests. The operations screen promised analysts a workload the
        # system did not deliver.
        #
        # Now: work out which payments the rules would alert on even if the
        # model said zero, using the real policy engine on the stored signals so
        # this cannot drift from what the worker does. Those alerts are spent
        # first. The model gets whatever budget is left, and competes only among
        # payments the rules did not already flag.
        from riskradar.policy.engine import Thresholds, apply as apply_policy
        from riskradar.rules.engine import Signal

        never = Thresholds(id=0, version=0, p_monitor=2.0, p_review=2.0, p_hold=2.0,
                           alert_min_level="MEDIUM")

        def rules_alone(raw) -> bool:
            sigs = raw if isinstance(raw, list) else json.loads(raw or "[]")
            signals = [Signal(code=x["code"], power=x["power"], severity=x["severity"],
                              evidence=x.get("evidence") or {}) for x in sigs]
            return apply_policy(0.0, signals, never).actionable

        rule_driven = np.array([rules_alone(r["signals"]) for r in rows])
        rule_per_day = float(rule_driven.sum()) / span_days
        remaining = budget - rule_per_day

        from collections import Counter
        by_rule = Counter()
        for r, flagged in zip(rows, rule_driven):
            if flagged:
                sigs = r["signals"] if isinstance(r["signals"], list) else json.loads(r["signals"] or "[]")
                for x in sigs:
                    if x["power"] in ("ESCALATE", "OVERRIDE"):
                        by_rule[x["code"]] += 1

        print("rule-driven alerts (raised even if the model scored zero):")
        print(f"  {rule_per_day:.1f}/day of a {budget}/day budget")
        for code, n in by_rule.most_common():
            print(f"    {code:<28} {n / span_days:7.1f}/day")
        print()

        if remaining <= 0:
            print(f"REFUSING: the rules alone use {rule_per_day:.0f}/day, over the whole "
                  f"{budget}/day budget. No model threshold can fix that — retune the")
            print("rules above (Administration > Rules) or raise the budget, then re-run.")
            raise SystemExit(2)

        # The model competes only for the budget the rules left, and only among
        # payments the rules did not already flag.
        p_free = p[~rule_driven]
        alert_rate = min(1.0, remaining / max(daily_volume * (1 - rule_driven.mean()), 1e-9))
        def within(share: float) -> float:
            """The lowest threshold whose alert share does not exceed ``share``.

            A calibrated model gives many payments exactly the same probability.
            A plain quantile can land on such a value, and every tied payment
            then alerts: on IEEE-CIS a 100-a-day budget became 486 a day. Stepping
            up to the next distinct value keeps the volume inside the budget.
            """
            t = float(np.quantile(p_free, 1.0 - share))
            if float(np.mean(p_free >= t)) <= share:
                return t
            above = np.unique(p_free[p_free > t])
            return float(above[0]) if len(above) else float(np.nextafter(t, 1.0))

        p_monitor = within(alert_rate)
        # REVIEW takes a third of the model's share, HOLD a tenth: the bands stay
        # nested inside the same envelope rather than each having its own.
        p_review = within(alert_rate / 3.0)
        p_hold = within(alert_rate / 10.0)

        # Ordering is a database constraint too, but a degenerate distribution
        # (every probability identical) would otherwise produce three equal
        # numbers and a meaningless band structure.
        p_review = max(p_review, p_monitor)
        p_hold = max(p_hold, p_review)

        system_alerts = rule_driven | (p >= p_monitor)
        implied = {
            "whole_system_alerts_per_day": round(float(system_alerts.sum()) / span_days, 1),
            "of_which_rules_alone_per_day": round(rule_per_day, 1),
            "monitor_and_above_per_day": round(float(np.mean(p >= p_monitor)) * daily_volume, 1),
            "review_and_above_per_day": round(float(np.mean(p >= p_review)) * daily_volume, 1),
            "hold_per_day": round(float(np.mean(p >= p_hold)) * daily_volume, 1),
        }

        print("derivation")
        print(f"  scored sample        {len(p)} decisions over {span_days:.2f} days")
        print(f"  implied daily volume {daily_volume:,.0f} transactions/day")
        print(f"  alert budget         {budget}/day  ({100 * alert_rate:.4f}% of traffic)")
        print()
        print(f"  p_monitor            {p_monitor:.6f}")
        print(f"  p_review             {p_review:.6f}")
        print(f"  p_hold               {p_hold:.6f}")
        print()
        print("  implied alert volume at these thresholds:")
        for key, value in implied.items():
            print(f"    {key:<30} {value}")

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

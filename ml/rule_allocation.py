"""Per-rule alert-budget caps, derived from the measured firing rates (D92).

D92 split the alert budget into a rules envelope and a model envelope and named
the next refinement: one number for eight rules of very different quality lets a
noisy rule spend the whole rules envelope and defer a better one behind it. This
derives a per-rule daily cap so each rule may raise up to its own share before it
defers.

Criterion, stated before the numbers are read
----------------------------------------------
A rule's cap is the ceiling of its measured alerts-per-day at the 75-a-day
operating point (D76), times a burst margin of 1.5 — enough headroom for a
genuinely busy day, but a rule firing far past its tuned rate (a traffic shift,
the velocity burst on a market day) defers rather than consuming the rules
envelope. The caps are individual ceilings; the rules envelope (0.6 x 75 = 45)
stays the collective one, so the caps only ever redistribute a spike, never
raise the total.

The rates are read from the measurements already published, not re-run here:
`ml/artifacts/tiers.json` (rule_precision) for eight rules, and
`ml/artifacts/receiving-side.json` for the inbound fan-in rule. OVERRIDE rules
(sanctions, known mule) are vetoes — always mandatory, always outside the caps —
so they are not capped.

    python ml/rule_allocation.py            # write ml/artifacts/rule-allocation.json
    python ml/rule_allocation.py --apply    # also publish the caps to app_config
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"

BURST_MARGIN = 1.5


def measured_rates() -> dict[str, dict[str, float]]:
    """Each ESCALATE rule's measured alerts-per-day and precision, from the
    published artifacts."""
    rates: dict[str, dict[str, float]] = {}
    tiers = json.loads((ARTIFACTS / "tiers.json").read_text(encoding="utf-8"))
    for code, m in tiers["rule_precision"].items():
        rates[code] = {"per_day": float(m["per_day"]), "precision": float(m["precision"])}
    # The inbound fan-in rule is measured in its own study (D78a).
    recv = json.loads((ARTIFACTS / "receiving-side.json").read_text(encoding="utf-8"))["chosen"]
    rates["MULE_INBOUND_FANIN"] = {"per_day": float(recv["alerts_per_day"]),
                                   "precision": float(recv["precision"])}
    return rates


def derive_caps(rates: dict[str, dict[str, float]], margin: float = BURST_MARGIN) -> dict[str, int]:
    return {code: max(1, math.ceil(m["per_day"] * margin)) for code, m in rates.items()}


def build() -> dict:
    rates = measured_rates()
    caps = derive_caps(rates)
    rule_envelope = int(round(0.6 * 75))
    return {
        "criterion": f"cap = ceil(measured alerts/day x {BURST_MARGIN}); OVERRIDE rules are uncapped vetoes",
        "operating_budget_per_day": 75,
        "rule_envelope_per_day": rule_envelope,
        "burst_margin": BURST_MARGIN,
        "rules": {
            code: {"measured_per_day": rates[code]["per_day"],
                   "precision": rates[code]["precision"], "cap": caps[code]}
            for code in sorted(caps)
        },
        "caps": caps,
        "cap_sum": sum(caps.values()),
        "note": (f"caps sum to {sum(caps.values())} against a {rule_envelope}/day rules envelope, so the "
                 "envelope stays the binding total; the caps only stop one rule taking a disproportionate "
                 "share on a spike"),
    }


def apply_to_db(caps: dict[str, int]) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import psycopg  # noqa: E402

    from riskradar.config import settings  # noqa: E402

    with psycopg.connect(settings().migrate_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config (key, value) VALUES ('alert_budget_rule_caps', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (json.dumps(caps),),
            )
        conn.commit()
    print(f"published {len(caps)} per-rule caps to app_config.alert_budget_rule_caps")


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive per-rule alert-budget caps (D92)")
    ap.add_argument("--apply", action="store_true", help="publish the caps to app_config")
    args = ap.parse_args()

    result = build()
    out = ARTIFACTS / "rule-allocation.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    for code, r in result["rules"].items():
        print(f"  {code:<32} {r['measured_per_day']:>5.1f}/day  prec {r['precision']:.3f}  -> cap {r['cap']}")
    print(f"  {'sum':<32} {'':>5}       {'':>10}  -> {result['cap_sum']} "
          f"(rules envelope {result['rule_envelope_per_day']})")
    if args.apply:
        apply_to_db(result["caps"])


if __name__ == "__main__":
    main()

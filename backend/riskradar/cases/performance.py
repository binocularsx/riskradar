"""Detection performance per rule and per model, from analyst outcomes (D69d).

The enterprise base PRD (FR-505) asks for the false-positive rate **per rule and
per model, not only in aggregate**, and ranks "show me false positives by rule"
as the first question that ends a vendor conversation. An aggregate rate hides
the one noisy rule that is drowning the desk.

Attribution is at **case** level, because that is the unit an analyst decides:

* A case counts once for every raising rule (ESCALATE or OVERRIDE) that fired on
  any of its alerts. A case where two rules fired counts for both — each rule
  was, on its own, enough reason to look.
* A case where **no** raising rule fired was put in front of the analyst by the
  model alone, and counts for ``MODEL``.
* Suppressing rules lower risk, so they never cause an alert and cannot be
  judged on alerts. They are reported separately (see the metrics endpoint).

Precision leaves INCONCLUSIVE out of the denominator: "we could not tell" is not
evidence the alert was wrong. Every rate carries a 95% Wilson interval and a
flag for thin evidence, because a precision measured on six cases is a rumour.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

MODEL = "MODEL"
RAISING_POWERS = {"ESCALATE", "OVERRIDE"}
# Below this many decided cases a rate is shown but marked as thin evidence.
MIN_DECIDED_FOR_EVIDENCE = 30


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval; None when there is nothing to measure."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def raising_codes(signals: Iterable[dict[str, Any]] | None) -> set[str]:
    return {
        s["code"]
        for s in (signals or [])
        if isinstance(s, dict) and s.get("power") in RAISING_POWERS and s.get("code")
    }


def by_driver(alert_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarise decided cases by what put them in front of an analyst.

    ``alert_rows``: one row per alert on a decided case, each with ``case_id``,
    ``outcome`` and ``signals`` (the decision's signal list).
    """
    case_outcome: dict[int, str] = {}
    case_codes: dict[int, set[str]] = defaultdict(set)
    for row in alert_rows:
        case_outcome[row["case_id"]] = row["outcome"]
        case_codes[row["case_id"]] |= raising_codes(row.get("signals"))

    tally: dict[str, dict[str, int]] = defaultdict(
        lambda: {"CONFIRMED_FRAUD": 0, "FALSE_POSITIVE": 0, "INCONCLUSIVE": 0}
    )
    for case_id, outcome in case_outcome.items():
        drivers = case_codes[case_id] or {MODEL}
        for driver in drivers:
            if outcome in tally[driver]:
                tally[driver][outcome] += 1

    out = []
    for driver, t in tally.items():
        fraud, false_alarm, unsure = t["CONFIRMED_FRAUD"], t["FALSE_POSITIVE"], t["INCONCLUSIVE"]
        decided = fraud + false_alarm
        out.append({
            "driver": driver,
            "cases": fraud + false_alarm + unsure,
            "confirmed_fraud": fraud,
            "false_positive": false_alarm,
            "inconclusive": unsure,
            "precision": round(fraud / decided, 3) if decided else None,
            "precision_ci95": wilson(fraud, decided),
            "false_alarm_rate": round(false_alarm / decided, 3) if decided else None,
            "enough_evidence": decided >= MIN_DECIDED_FOR_EVIDENCE,
        })
    # Noisiest first: the rule most in need of attention leads the table.
    out.sort(key=lambda r: (-(r["false_positive"]), r["driver"]))
    return out

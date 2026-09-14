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

WP-09 adds the two figures banks benchmark on: false alerts per confirmed fraud
incident, and (from the evaluation) the share of fraud value detected.
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


def alert_ratio(case_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """False alerts per confirmed fraud incident, from analyst outcomes (WP-09).

    Banks benchmark this ratio, not a count of false alarms: published
    best-in-class sits at 12-14 to 1. A case is one incident (D13a), so the
    denominator is confirmed fraud cases and the numerator is every alert on a
    case closed as a false positive.

    It is a **floor**: an alert on a legitimate payment inside a confirmed fraud
    case is not counted as false, because analysts label the case, not each
    alert. INCONCLUSIVE cases sit on neither side.

    ``case_rows``: one row per decided case with ``outcome``, ``alerts`` and
    ``alerted_value_minor`` (the money on that case's alerted payments).
    """
    false_alerts = confirmed = unsure = 0
    fraud_value = 0
    for row in case_rows:
        if row["outcome"] == "FALSE_POSITIVE":
            false_alerts += int(row["alerts"])
        elif row["outcome"] == "CONFIRMED_FRAUD":
            confirmed += 1
            fraud_value += int(row["alerted_value_minor"] or 0)
        elif row["outcome"] == "INCONCLUSIVE":
            unsure += 1
    return {
        "false_alerts": false_alerts,
        "confirmed_incidents": confirmed,
        "inconclusive_cases": unsure,
        "false_alerts_per_incident": round(false_alerts / confirmed, 1) if confirmed else None,
        # Only the money we flagged is known. The fraud we missed has no label,
        # so a live value detection rate cannot be computed; it comes from the
        # evaluation (the budget menu).
        "confirmed_fraud_value_minor": fraud_value,
        "enough_evidence": confirmed >= MIN_DECIDED_FOR_EVIDENCE,
    }


def budget_menu(menu: dict[str, Any] | None, current_budget: int) -> dict[str, Any]:
    """The measured budget options, with today's setting marked (WP-09, D67c).

    ``menu`` is ``ml/artifacts/budget-menu.json`` or None when it has not been
    produced. Display only: choosing a budget reopens D61b and is the team's
    call, so nothing here changes the configured budget.
    """
    if not menu:
        return {"available": False, "current_budget_per_day": current_budget, "options": []}
    options = [dict(o, current=o["budget_per_day"] == current_budget) for o in menu["options"]]
    return {
        "available": True,
        "current_budget_per_day": current_budget,
        # A budget nobody measured must not borrow another budget's figures.
        "current_is_measured": any(o["current"] for o in options),
        "measured": menu.get("measured"),
        "test_days": menu.get("test_days"),
        "benchmarks": menu.get("benchmarks"),
        "options": options,
    }

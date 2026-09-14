"""Aggregates and search (FR-032, FR-034).

D14b: transactions are presented as **aggregates and a searchable table**, never
as a live feed of every transaction. A scrolling wall of every payment is
unreadable, unmonitorable, and would push more bytes at the browser than the
alert stream by three orders of magnitude.

FR-034 lives here too rather than on its own screen (D25): alert volume, case
outcomes and model metrics are the same query family as the charts, and a
separate metrics page was one of the compensating cuts.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...cases import performance
from ...config import settings
from ...security.rbac import Permission
from ..deps import get_conn, requires

router = APIRouter(prefix="/v1", tags=["metrics"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


@router.get("/metrics/overview")
def overview(
    hours: int = Query(24, ge=1, le=720),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    window = f"{int(hours)} hours"

    volume = _rows(
        conn,
        f"""
        SELECT date_trunc('hour', occurred_at) AS bucket,
               count(*)                        AS transactions,
               count(*) FILTER (WHERE auth_result = 'APPROVED') AS approved,
               count(*) FILTER (WHERE auth_result IN ('DECLINED','FAILED')) AS declined,
               coalesce(sum(amount_minor) FILTER (WHERE auth_result = 'APPROVED'), 0)
                                               AS approved_value_minor
          FROM transactions
         WHERE occurred_at > now() - interval '{window}'
         GROUP BY 1 ORDER BY 1
        """,
    )

    alerts = _rows(
        conn,
        f"""
        SELECT date_trunc('hour', raised_at) AS bucket,
               risk_level,
               count(*) AS alerts
          FROM alerts
         WHERE raised_at > now() - interval '{window}'
         GROUP BY 1, 2 ORDER BY 1
        """,
    )

    risk_mix = _rows(
        conn,
        f"""
        SELECT risk_level, decision, count(*) AS n
          FROM decisions
         WHERE decided_at > now() - interval '{window}'
         GROUP BY 1, 2 ORDER BY 1
        """,
    )

    outcomes = _rows(
        conn,
        """
        SELECT coalesce(outcome::text, 'PENDING') AS outcome, state, count(*) AS n
          FROM cases GROUP BY 1, 2 ORDER BY 1
        """,
    )

    signals = _rows(
        conn,
        f"""
        SELECT s->>'code' AS code, s->>'power' AS power, count(*) AS n
          FROM decisions d, jsonb_array_elements(d.signals) s
         WHERE d.decided_at > now() - interval '{window}'
         GROUP BY 1, 2 ORDER BY n DESC
        """,
    )

    # NFR-001 is a p95 latency claim, so the dashboard shows p95 — not a mean,
    # which would hide exactly the tail the NFR is about.
    latency = _rows(
        conn,
        f"""
        SELECT count(*) AS scored,
               percentile_disc(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
               max(latency_ms) AS max_ms,
               count(*) FILTER (WHERE rule_only_mode) AS rule_only
          FROM decisions
         WHERE decided_at > now() - interval '{window}'
        """,
    )[0]

    queue = _rows(
        conn,
        """
        SELECT count(*) AS depth,
               coalesce(max(extract(epoch FROM now() - enqueued_at)), 0) AS oldest_seconds,
               coalesce(sum(attempts), 0) AS total_attempts
          FROM scoring_queue
        """,
    )[0]

    budget_row = _rows(
        conn,
        "SELECT value FROM app_config WHERE key = 'alert_budget_per_day'",
    )
    budget = int(budget_row[0]["value"]) if budget_row else 75
    today = _rows(
        conn,
        "SELECT count(*) AS n FROM alerts WHERE raised_at > now() - interval '24 hours'",
    )[0]["n"]

    model = _rows(
        conn,
        """
        SELECT name, version, calibration, metrics, trained_at, promoted_at
          FROM model_versions WHERE is_active LIMIT 1
        """,
    )

    return {
        "window_hours": hours,
        "transaction_volume": volume,
        "alert_volume": alerts,
        "risk_mix": risk_mix,
        "case_outcomes": outcomes,
        "signal_frequency": signals,
        "latency": latency,
        "queue": queue,
        # D11d: the budget is not decoration — it is the number every threshold
        # in the system was solved backwards from, so it belongs on the screen
        # next to the volume it constrains.
        "alert_budget": {
            "per_day": budget,
            "last_24h": today,
            "utilisation": round(today / budget, 3) if budget else None,
        },
        "active_model": model[0] if model else None,
    }


@router.get("/metrics/detection")
def detection(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """False alarms per rule and per model, from analyst outcomes (D69d).

    Base PRD FR-505: an aggregate false-positive rate hides the one rule that is
    drowning the desk. ``by_driver`` answers "which rule, or the model, put
    cases in front of analysts, and how often were they right". ``rules``
    answers, per rule, how often it fired and how often that became an alert;
    for suppressing rules, how often it lowered risk on a customer who had a
    confirmed fraud case within a day either side, which is the closest this
    system can get to a missed-fraud count without labels on every payment.
    """
    window = f"{days} days"
    alert_rows = _rows(
        conn,
        f"""
        SELECT a.case_id, c.outcome, d.signals
          FROM alerts a
          JOIN cases c     ON c.id = a.case_id
          JOIN decisions d ON d.id = a.decision_id
         WHERE c.outcome IS NOT NULL
           AND c.opened_at > now() - interval '{window}'
        """,
    )
    decided_cases = len({r["case_id"] for r in alert_rows})

    rules = _rows(
        conn,
        f"""
        SELECT s->>'code' AS code, s->>'power' AS power,
               count(*) AS fired,
               count(*) FILTER (WHERE a.id IS NOT NULL) AS on_alerts
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN alerts a ON a.decision_id = d.id,
               jsonb_array_elements(d.signals) s
         WHERE d.decided_at > now() - interval '{window}'
           -- Replayed history is scored but can never alert (D8d); counting it
           -- would make a rule look as if it fired without consequence.
           AND t.raise_alerts
         GROUP BY 1, 2
         ORDER BY fired DESC
        """,
    )
    suppressed_on_fraud = {
        r["code"]: int(r["n"])
        for r in _rows(
            conn,
            f"""
            SELECT s->>'code' AS code, count(*) AS n
              FROM decisions d
              JOIN transactions t ON t.id = d.transaction_id,
                   jsonb_array_elements(d.signals) s
             WHERE s->>'power' = 'SUPPRESS'
               AND d.decided_at > now() - interval '{window}'
               AND EXISTS (
                   SELECT 1 FROM cases c
                    WHERE c.subject_token = t.subject_token
                      AND c.outcome = 'CONFIRMED_FRAUD'
                      AND c.opened_at BETWEEN t.occurred_at - interval '24 hours'
                                          AND t.occurred_at + interval '24 hours')
             GROUP BY 1
            """,
        )
    }
    for rule in rules:
        rule["fired"] = int(rule["fired"])
        rule["on_alerts"] = int(rule["on_alerts"])
        if rule["power"] == "SUPPRESS":
            rule["lowered_risk_on_confirmed_fraud_customers"] = suppressed_on_fraud.get(rule["code"], 0)

    case_rows = _rows(
        conn,
        f"""
        SELECT c.id, c.outcome::text AS outcome,
               count(a.id) AS alerts,
               coalesce(sum(t.amount_minor), 0) AS alerted_value_minor
          FROM cases c
          JOIN alerts a       ON a.case_id = c.id
          JOIN transactions t ON t.id = a.transaction_id
         WHERE c.outcome IS NOT NULL
           AND c.opened_at > now() - interval '{window}'
         GROUP BY c.id, c.outcome
        """,
    )

    return {
        "window_days": days,
        "decided_cases": decided_cases,
        "min_decided_for_evidence": performance.MIN_DECIDED_FOR_EVIDENCE,
        "by_driver": performance.by_driver(alert_rows),
        "rules": rules,
        # WP-09: the unit banks benchmark, not a raw count of false alarms.
        "ratio": performance.alert_ratio(case_rows),
    }


@router.get("/metrics/budget-menu")
def budget_menu(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """What each alert budget buys, as measured offline (WP-09, D67c).

    The figures come from ``ml/budget_menu.py``, not from live traffic: the
    fraud a live system misses carries no label, so recall and value detection
    can only be measured where the truth is known. Read-only by design.
    """
    path = settings().artifact_dir / "budget-menu.json"
    menu = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    budget_row = _rows(conn, "SELECT value FROM app_config WHERE key = 'alert_budget_per_day'")
    budget = int(budget_row[0]["value"]) if budget_row else 75
    return performance.budget_menu(menu, budget)


@router.get("/transactions/search")
def search_transactions(
    q: str | None = Query(None, description="transaction_ref or display name fragment"),
    channel: str | None = None,
    auth_result: str | None = None,
    risk_level: str | None = None,
    min_amount_minor: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    where = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if q:
        where.append("(t.transaction_ref ILIKE %(q)s OR t.display_name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if channel:
        where.append("t.channel = %(channel)s")
        params["channel"] = channel
    if auth_result:
        where.append("t.auth_result = %(auth_result)s")
        params["auth_result"] = auth_result
    if risk_level:
        where.append("d.risk_level = %(risk_level)s")
        params["risk_level"] = risk_level
    if min_amount_minor is not None:
        where.append("t.amount_minor >= %(min_amount)s")
        params["min_amount"] = min_amount_minor

    items = _rows(
        conn,
        f"""
        SELECT t.id, t.transaction_ref, t.occurred_at, t.amount_minor, t.currency,
               t.channel, t.instrument, t.rail, t.auth_result, t.decline_reason,
               t.display_name, t.ip_region,
               d.score_0_100, d.risk_level, d.decision,
               (a.id IS NOT NULL) AS alerted, a.case_id
          FROM transactions t
          LEFT JOIN decisions d ON d.transaction_id = t.id
          LEFT JOIN alerts a    ON a.transaction_id = t.id
         WHERE {' AND '.join(where)}
         ORDER BY t.occurred_at DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    return {"items": items, "limit": limit, "offset": offset}

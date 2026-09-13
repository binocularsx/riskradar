"""Alert queue, case detail and the investigation workflow (FR-020 to FR-024).

The state machine (D13b)::

    OPEN --> UNDER_REVIEW --> {CONFIRMED_FRAUD | FALSE_POSITIVE | INCONCLUSIVE} --> CLOSED
                  |
                  +--> ESCALATED --> (INFOSEC | FRAUD_OPS) --> back to review, or CLOSED

``INCONCLUSIVE`` is mandatory and is offered with equal weight in the UI.
Forcing a binary under time pressure produces analysts who pick whichever option
is faster, and those answers become training labels (D13c) — false certainty in,
false confidence out.

Every transition is audited with actor, timestamp, from-state and to-state
(FR-023). The audit write shares the transaction with the state change, so an
audit row can never describe a transition that rolled back.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...audit import chain
from ...security.rbac import Permission
from ..deps import get_conn, requires
from ..schemas import AssignIn, EscalateIn, NoteIn, OutcomeIn

router = APIRouter(prefix="/v1", tags=["cases"])

OPEN_STATES = ("OPEN", "UNDER_REVIEW", "ESCALATED")


def _fetch_case(conn: Any, case_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="case not found")
    return dict(row) if not isinstance(row, dict) else row


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.get("/cases")
def list_cases(
    state: str | None = Query(None),
    risk_level: str | None = Query(None),
    channel: str | None = Query(None),
    assigned_to_me: bool = Query(False),
    sort: str = Query("score", pattern="^(score|recent|opened)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """FR-033: sortable by score, filterable by risk level, channel and state."""
    where = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if state:
        where.append("c.state = %(state)s")
        params["state"] = state
    else:
        where.append("c.state = ANY(%(open_states)s)")
        params["open_states"] = list(OPEN_STATES)
    if risk_level:
        where.append("c.risk_level = %(risk_level)s")
        params["risk_level"] = risk_level
    if assigned_to_me:
        where.append("c.assignee_id = %(uid)s")
        params["uid"] = user["id"]
    if channel:
        where.append(
            "EXISTS (SELECT 1 FROM alerts a JOIN transactions t ON t.id = a.transaction_id "
            "WHERE a.case_id = c.id AND t.channel = %(channel)s)"
        )
        params["channel"] = channel

    order = {
        "score": "max_score DESC NULLS LAST, c.last_alert_at DESC",
        "recent": "c.last_alert_at DESC",
        "opened": "c.opened_at DESC",
    }[sort]

    sql = f"""
        SELECT c.id, c.subject_token, c.state, c.outcome, c.risk_level,
               c.opened_at, c.last_alert_at, c.alert_count, c.assignee_id,
               c.escalated_to,
               u.display_name AS assignee_name,
               (SELECT max(a.score_0_100) FROM alerts a WHERE a.case_id = c.id) AS max_score,
               (SELECT t.display_name FROM alerts a
                  JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id ORDER BY a.raised_at LIMIT 1) AS subject_display_name
          FROM cases c
          LEFT JOIN users u ON u.id = c.assignee_id
         WHERE {' AND '.join(where)}
         ORDER BY {order}
         LIMIT %(limit)s OFFSET %(offset)s
    """
    items = _rows(conn, sql, params)

    total = _rows(
        conn,
        f"SELECT count(*) AS n FROM cases c WHERE {' AND '.join(where)}",
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )[0]["n"]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/alerts")
def list_alerts(
    limit: int = Query(50, ge=1, le=200),
    since_id: int = Query(0, ge=0),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    items = _rows(
        conn,
        """
        SELECT a.id, a.case_id, a.risk_level, a.score_0_100, a.raised_at,
               t.transaction_ref, t.amount_minor, t.currency, t.channel,
               t.instrument, t.rail, t.auth_result, t.display_name,
               d.decision, d.signals
          FROM alerts a
          JOIN transactions t ON t.id = a.transaction_id
          JOIN decisions   d ON d.id = a.decision_id
         WHERE a.id > %s
         ORDER BY a.id DESC
         LIMIT %s
        """,
        (since_id, limit),
    )
    return {"items": items}


# ---------------------------------------------------------------------------
# Case detail (FR-024, D24b)
# ---------------------------------------------------------------------------


@router.get("/cases/{case_id}")
def case_detail(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Everything the analyst needs to decide, in one response.

    Including — per D24b — the subject's **full transaction timeline across the
    correlation window, alerted and un-alerted alike**. Without that, the
    incident-level recall claim in §12.3 would be unearned: the analyst would
    only ever see the slice that tripped a threshold, and could not tell a
    six-transaction extraction from a one-off.
    """
    case = _fetch_case(conn, case_id)

    alerts = _rows(
        conn,
        """
        SELECT a.id, a.risk_level, a.score_0_100, a.raised_at,
               t.id AS transaction_id, t.transaction_ref, t.occurred_at,
               t.amount_minor, t.currency, t.channel, t.instrument, t.rail,
               t.auth_result, t.decline_reason, t.ip_region, t.merchant_category,
               t.display_name, t.product_type, t.origin_sol_id,
               d.p_fraud, d.decision, d.signals, d.attributions, d.policy_trace,
               d.features, d.rule_only_mode, d.feature_spec_version,
               mv.name AS model_name, mv.version AS model_version,
               rs.version AS ruleset_version, ts.version AS threshold_version
          FROM alerts a
          JOIN transactions t   ON t.id = a.transaction_id
          JOIN decisions d      ON d.id = a.decision_id
          LEFT JOIN model_versions mv ON mv.id = d.model_version_id
          JOIN rulesets rs      ON rs.id = d.ruleset_id
          JOIN threshold_sets ts ON ts.id = d.threshold_set_id
         WHERE a.case_id = %s
         ORDER BY a.raised_at
        """,
        (case_id,),
    )

    timeline = _rows(
        conn,
        """
        SELECT t.id, t.transaction_ref, t.occurred_at, t.amount_minor, t.currency,
               t.channel, t.instrument, t.rail, t.auth_result, t.decline_reason,
               t.account_token, t.beneficiary_token, t.ip_region, t.display_name,
               d.score_0_100, d.risk_level, d.decision,
               (al.id IS NOT NULL) AS alerted
          FROM transactions t
          LEFT JOIN decisions d ON d.transaction_id = t.id
          LEFT JOIN alerts al   ON al.transaction_id = t.id AND al.case_id = %(case_id)s
         WHERE t.subject_token = %(subject)s
           AND t.occurred_at >= %(from)s
           AND t.occurred_at <= %(to)s
         ORDER BY t.occurred_at
         LIMIT 500
        """,
        {
            "case_id": case_id,
            "subject": case["subject_token"],
            "from": case["opened_at"],
            "to": case["correlation_expires_at"],
        },
    )

    # The behavioural baseline the analyst compares against. Read from the
    # mutable dimension deliberately — this is the UI, not the feature package,
    # and "what is true about this account now" is the right question here.
    baseline = _rows(
        conn,
        """
        SELECT acc.account_token, acc.product_type, acc.origin_sol_id,
               acc.account_opened_at, acc.last_activity_at,
               (SELECT count(*) FROM transactions x
                 WHERE x.account_token = acc.account_token
                   AND x.occurred_at > now() - interval '30 days') AS txn_30d,
               (SELECT coalesce(sum(x.amount_minor), 0) FROM transactions x
                 WHERE x.account_token = acc.account_token
                   AND x.auth_result = 'APPROVED'
                   AND x.occurred_at > now() - interval '30 days') AS approved_value_30d_minor
          FROM accounts acc
         WHERE acc.subject_token = %s
        """,
        (case["subject_token"],),
    )

    notes = _rows(
        conn,
        """
        SELECT n.id, n.body, n.created_at, u.display_name AS author
          FROM case_notes n JOIN users u ON u.id = n.author_id
         WHERE n.case_id = %s ORDER BY n.created_at
        """,
        (case_id,),
    )

    history = _rows(
        conn,
        """
        SELECT occurred_at, action, from_state, to_state, payload,
               (SELECT display_name FROM users WHERE id = actor_user_id) AS actor
          FROM audit_log
         WHERE object_type = 'case' AND object_id = %s
           AND action <> 'CASE_VIEWED'
         ORDER BY occurred_at
        """,
        (str(case_id),),
    )

    # D69k (base PRD FR-408): who looked at a customer's case is recorded, not
    # only who changed it. A fraud desk can read anyone's payments, so reading
    # is the access worth proving afterwards. Appended last, because the append
    # takes the record's lock until this request commits.
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_VIEWED",
        object_type="case",
        object_id=case_id,
        payload={"alerts": len(alerts), "timeline_rows": len(timeline)},
    )

    return {
        "case": case,
        "alerts": alerts,
        "timeline": timeline,
        "baseline": baseline,
        "notes": notes,
        "history": history,
    }


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


def _transition(
    conn: Any,
    case: dict[str, Any],
    user: dict[str, Any],
    *,
    to_state: str,
    action: str,
    extra_sql: str = "",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(params or {})
    params.update({"case_id": case["id"], "to_state": to_state})
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cases SET state = %(to_state)s {extra_sql} WHERE id = %(case_id)s",
            params,
        )
    chain.append(
        conn,
        actor_user_id=user["id"],
        action=action,
        object_type="case",
        object_id=case["id"],
        from_state=case["state"],
        to_state=to_state,
        payload=payload or {},
    )
    return _fetch_case(conn, case["id"])


@router.post("/cases/{case_id}/review")
def start_review(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id)
    if case["state"] not in ("OPEN", "ESCALATED"):
        raise HTTPException(400, f"cannot start review from {case['state']}")
    return _transition(
        conn,
        case,
        user,
        to_state="UNDER_REVIEW",
        action="CASE_REVIEW_STARTED",
        extra_sql=", assignee_id = COALESCE(assignee_id, %(uid)s), escalated_to = NULL",
        params={"uid": user["id"]},
    )


@router.post("/cases/{case_id}/notes")
def add_note(
    case_id: int,
    body: NoteIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "cannot annotate a closed case")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s) RETURNING id",
            (case_id, user["id"], body.body),
        )
        row = cur.fetchone()
        note_id = int(row["id"] if isinstance(row, dict) else row[0])
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_NOTE_ADDED",
        object_type="case",
        object_id=case_id,
        payload={"note_id": note_id, "length": len(body.body)},
    )
    return {"id": note_id}


@router.post("/cases/{case_id}/outcome")
def set_outcome(
    case_id: int,
    body: OutcomeIn,
    user: dict = Depends(requires(Permission.CASES_SET_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Record the analyst's verdict.

    D13c: these are not a UI status. ``CONFIRMED_FRAUD`` and ``FALSE_POSITIVE``
    are analyst-labelled ground truth — the only non-circular labels this system
    will ever produce, and the eventual exit from simulator-only training.
    """
    case = _fetch_case(conn, case_id)
    if case["state"] not in ("UNDER_REVIEW", "ESCALATED"):
        raise HTTPException(400, "open the case for review before recording an outcome")
    if body.note:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                (case_id, user["id"], body.note),
            )
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET outcome = %s WHERE id = %s", (body.outcome, case_id))
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_OUTCOME_SET",
        object_type="case",
        object_id=case_id,
        from_state=case["outcome"],
        to_state=body.outcome,
        payload={"role": user["role"]},
    )
    return _fetch_case(conn, case_id)


@router.post("/cases/{case_id}/escalate")
def escalate(
    case_id: int,
    body: EscalateIn,
    user: dict = Depends(requires(Permission.CASES_ESCALATE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "cannot escalate a closed case")
    if body.note:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                (case_id, user["id"], body.note),
            )
    return _transition(
        conn,
        case,
        user,
        to_state="ESCALATED",
        action="CASE_ESCALATED",
        extra_sql=", escalated_to = %(target)s",
        params={"target": body.target},
        payload={"target": body.target},
    )


@router.post("/cases/{case_id}/assign")
def assign(
    case_id: int,
    body: AssignIn,
    user: dict = Depends(requires(Permission.CASES_REASSIGN)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET assignee_id = %s WHERE id = %s", (body.assignee_id, case_id))
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_REASSIGNED",
        object_type="case",
        object_id=case_id,
        from_state=str(case["assignee_id"]),
        to_state=str(body.assignee_id),
    )
    return _fetch_case(conn, case_id)


@router.post("/cases/{case_id}/close")
def close_case(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_CLOSE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Closing is a FRAUD_OPS_LEAD power, not an analyst one (D12b).

    Closing a confirmed-fraud case also promotes its beneficiaries onto the
    ``KNOWN_MULE`` list, which is the loop closing: the second transfer to a
    destination an analyst has already confirmed should not have to re-convince a
    model. Recorded in the audit trail with the tokens added, because it is a
    control change made by a human and needs to be attributable.
    """
    case = _fetch_case(conn, case_id)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "case is already closed")
    if not case["outcome"]:
        raise HTTPException(400, "record an outcome before closing")

    promoted: list[str] = []
    if case["outcome"] == "CONFIRMED_FRAUD":
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO beneficiary_lists (kind, token, note, added_by)
                SELECT DISTINCT 'KNOWN_MULE', t.beneficiary_token,
                       'auto: confirmed fraud on case ' || %s, %s
                  FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = %s AND t.beneficiary_token IS NOT NULL
                ON CONFLICT DO NOTHING
                RETURNING token
                """,
                (case_id, user["id"], case_id),
            )
            promoted = [
                (r["token"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()
            ]

    updated = _transition(
        conn,
        case,
        user,
        to_state="CLOSED",
        action="CASE_CLOSED",
        extra_sql=", closed_at = now(), closed_by = %(uid)s",
        params={"uid": user["id"]},
        payload={"outcome": case["outcome"], "known_mule_tokens_added": promoted},
    )
    return updated

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
from ...clocks import sweep as clock_sweep
from ...clocks import watchlist
from ...events import publish
from ...security import visibility
from ...security.rbac import Permission
from ..deps import get_conn, requires
from ..schemas import (
    AssignIn,
    EscalateIn,
    MilestoneIn,
    NoteIn,
    OutcomeIn,
    ReportIn,
    WatchlistContactIn,
    WatchlistLiftIn,
    WatchlistPlaceIn,
)

router = APIRouter(prefix="/v1", tags=["cases"])

OPEN_STATES = ("OPEN", "UNDER_REVIEW", "ESCALATED")


def _fetch_case(conn: Any, case_id: int, user: dict[str, Any] | None = None) -> dict[str, Any]:
    """One case, and — when the caller is named — only if they may see it (D94).

    404, not 403: a person outside the scope should not learn that a case with
    that number exists, which is what a different answer would tell them.
    """
    sql, params = ("TRUE", {}) if user is None else visibility.predicate(user)
    with conn.cursor() as cur:
        cur.execute(f"SELECT c.* FROM cases c WHERE c.id = %(case_id)s AND {sql}", {**params, "case_id": case_id})
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
    """FR-033: sortable by score, filterable by risk level, channel and state.

    D94: the list is scoped on the server. An analyst's "all" is their own work.
    """
    scope_sql, scope_params = visibility.predicate(user)
    where = [scope_sql]
    params: dict[str, Any] = {"limit": limit, "offset": offset, **scope_params}

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
    # D94: alerts belong to cases, so they are scoped like cases.
    scope_sql, scope_params = visibility.predicate(user)
    items = _rows(
        conn,
        """
        SELECT a.id, a.case_id, a.risk_level, a.score_0_100, a.raised_at, a.source,
               t.transaction_ref, t.amount_minor, t.currency, t.channel,
               t.instrument, t.rail, t.auth_result, t.display_name,
               d.decision, d.signals
          FROM alerts a
          JOIN cases c ON c.id = a.case_id
          JOIN transactions t ON t.id = a.transaction_id
          JOIN decisions   d ON d.id = a.decision_id
         WHERE a.id > %(since_id)s AND {scope}
         ORDER BY a.id DESC
         LIMIT %(limit)s
        """.format(scope=scope_sql),
        {"since_id": since_id, "limit": limit, **scope_params},
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
    case = _fetch_case(conn, case_id, user)

    alerts = _rows(
        conn,
        """
        SELECT a.id, a.risk_level, a.score_0_100, a.raised_at, a.source,
               t.id AS transaction_id, t.transaction_ref, t.occurred_at,
               t.amount_minor, t.currency, t.channel, t.instrument, t.rail,
               t.auth_result, t.decline_reason, t.ip_region, t.merchant_category,
               t.display_name, t.product_type, t.origin_sol_id, t.direction, t.remitter_bank_code,
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
        # WP-05: empty until the customer reports; then every CBN clock's state.
        "clocks": clock_sweep.clocks_for_case(conn, case_id),
        # WP-06: this customer's recent temporary watch-list flags, newest first.
        "watchlist": watchlist.flags_for_subject(conn, case["subject_token"]),
        # D75: the customer's identity as far as Risk Radar knows it (never the
        # BVN itself), and flags other institutions placed on that BVN.
        "identity": _identity(conn, case["subject_token"]),
        "industry_flags": watchlist.industry_flags(conn, [case["subject_token"]], active_only=False)
                                   .get(case["subject_token"], []),
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
    case = _fetch_case(conn, case_id, user)
    if case["state"] not in ("OPEN", "ESCALATED"):
        raise HTTPException(400, f"cannot start review from {case['state']}")
    return _transition(
        conn,
        case,
        user,
        to_state="UNDER_REVIEW",
        action="CASE_REVIEW_STARTED",
        # D83: taking an escalated case makes it the taker's. The analyst who
        # escalated stays on record (escalated_by) and gets it back on return.
        extra_sql=(", assignee_id = %(uid)s" if case["state"] == "ESCALATED"
                   else ", assignee_id = COALESCE(assignee_id, %(uid)s)") + ", escalated_to = NULL",
        params={"uid": user["id"]},
    )


@router.post("/cases/{case_id}/notes")
def add_note(
    case_id: int,
    body: NoteIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id, user)
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
    user: dict = Depends(requires(Permission.CASES_SUBMIT_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Propose the analyst's verdict (D93). It is not the outcome until a lead approves.

    This endpoint used to write ``cases.outcome`` directly, which let one person
    create both the bank's fraud record and the model's training label (D13c).
    It now raises a submission, exactly as ``POST /v1/cases/{id}/submissions``
    does, so the old caller keeps working and the control holds either way.
    The note travels as the rationale when no separate one is given.
    """
    from ...cases import submissions as subs

    case = _fetch_case(conn, case_id, user)
    if case["state"] not in ("UNDER_REVIEW", "ESCALATED"):
        raise HTTPException(400, "open the case for review before proposing an outcome")
    rationale = (body.note or "").strip()
    if len(rationale) < 20:
        raise HTTPException(422, "a proposed outcome needs a rationale of at least 20 characters in `note`: "
                                 "a lead has to decide on something")
    try:
        submission = subs.submit(conn, case_id=case_id, user=user, proposed_outcome=body.outcome,
                                 rationale=rationale, restrictions=[], expected_version=None)
    except subs.SubmissionError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"case": _fetch_case(conn, case_id, user), "submission": submission,
            "note": "proposed, not decided: a Fraud Ops Lead who did not write it must approve (D93)"}


@router.post("/cases/{case_id}/escalate")
def escalate(
    case_id: int,
    body: EscalateIn,
    user: dict = Depends(requires(Permission.CASES_ESCALATE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _fetch_case(conn, case_id, user)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "cannot escalate a closed case")
    # D108: InfoSec is retired. The target stays in the schema so that cases
    # escalated there before the change still read back, but nothing new may go
    # to a queue nobody works — account takeover, login abuse and MFA problems
    # are actioned on this desk by proposing the containment the bank applies.
    if body.target == "INFOSEC":
        raise HTTPException(
            400,
            "InfoSec is retired: escalate to the Fraud Ops lead. For a suspected account "
            "takeover, propose session termination, a credential reset or MFA re-enrolment "
            "on the finding instead.",
        )
    # D83: a reason is required. The team receiving it has to know why without
    # calling the analyst, and the record has to say why it moved.
    if not (body.note or "").strip():
        raise HTTPException(400, "say why you are escalating; the receiving team reads it first")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
            (case_id, user["id"], f"Escalated to {body.target}: {body.note}"),
        )
    # The case leaves the escalating analyst's queue and waits, unassigned, in
    # the receiving team's escalated queue.
    return _transition(
        conn,
        case,
        user,
        to_state="ESCALATED",
        action="CASE_ESCALATED",
        extra_sql=(", escalated_to = %(target)s, escalated_by = %(uid)s, escalated_at = now(), "
                   "escalation_reason = %(reason)s, assignee_id = NULL"),
        params={"target": body.target, "uid": user["id"], "reason": body.note},
        payload={"target": body.target, "reason": body.note[:500]},
    )


# ---------------------------------------------------------------------------
# Regulatory clocks (WP-05, D71)
# ---------------------------------------------------------------------------

MILESTONE_COLUMN = {
    "ACKNOWLEDGED": "acknowledged_at",
    "COUNTERPARTY_NOTIFIED": "counterparty_notified_at",
    "INVESTIGATION_CONCLUDED": "investigation_concluded_at",
    "REIMBURSED": "reimbursed_at",
}


def _now(conn: Any):
    with conn.cursor() as cur:
        cur.execute("SELECT now() AS ts")
        row = cur.fetchone()
    return row["ts"] if isinstance(row, dict) else row[0]


def _note(conn: Any, case_id: int, user: dict[str, Any], body: str | None) -> None:
    if body:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                (case_id, user["id"], body),
            )


@router.post("/cases/{case_id}/report")
def record_report(
    case_id: int,
    body: ReportIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The customer reported the fraud: start the CBN clocks.

    Recorded once. The first report is what the refund clock counts from, so a
    second call cannot move it later — correcting it would be rewriting the
    moment the bank's obligations began, and belongs in a note, not an update.
    The case is pinned to the clock policy in force now.
    """
    from ...cases import reports

    try:
        # D90: the same path as POST /v1/reports: clocks, the stream, the audit record.
        reports.start_clocks(conn, case_id=case_id, user=user, reported_at=body.reported_at, channel=body.channel,
                             counterparty_institution=body.counterparty_institution, note=body.note)
    except reports.ReportError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"case": _fetch_case(conn, case_id, user), "clocks": clock_sweep.clocks_for_case(conn, case_id)}


@router.post("/cases/{case_id}/milestones")
def record_milestone(
    case_id: int,
    body: MilestoneIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """A moment that stops a clock: acknowledged, receiving bank told, investigation
    concluded, customer reimbursed. Each is recorded once, never edited.

    Reimbursement pays money back, so it needs ``cases:close`` (a Fraud Ops Lead),
    and it needs a concluded investigation that confirmed fraud first.
    """
    case = _fetch_case(conn, case_id, user)
    column = MILESTONE_COLUMN[body.milestone]
    if not case["first_reported_at"]:
        raise HTTPException(400, "record the customer's report first; it starts the clocks")
    if case[column]:
        raise HTTPException(409, f"{body.milestone.lower()} is already recorded")
    if body.milestone == "REIMBURSED":
        if Permission.CASES_CLOSE.value not in user["permissions"]:
            raise HTTPException(403, "recording a reimbursement needs a Fraud Ops Lead")
        if not case["investigation_concluded_at"] or case["outcome"] != "CONFIRMED_FRAUD":
            raise HTTPException(400, "reimbursement follows an investigation that confirmed fraud")
    if body.milestone == "INVESTIGATION_CONCLUDED" and not case["outcome"]:
        raise HTTPException(400, "record the outcome before concluding the investigation")
    if body.milestone == "COUNTERPARTY_NOTIFIED" and not (
        body.counterparty_institution or case["counterparty_institution"]
    ):
        raise HTTPException(400, "name the institution that was notified")

    now = _now(conn)
    at = body.at or now
    if at > now:
        raise HTTPException(400, "a milestone cannot be in the future")
    if at < case["first_reported_at"]:
        raise HTTPException(400, "a milestone cannot come before the customer's report")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE cases SET {column} = %s,
                   counterparty_institution = COALESCE(%s, counterparty_institution)
             WHERE id = %s
            """,
            (at, body.counterparty_institution, case_id),
        )
    _note(conn, case_id, user, body.note)
    chain.append(
        conn,
        actor_user_id=user["id"],
        action=f"CLOCK_{body.milestone}",
        object_type="case",
        object_id=case_id,
        payload={"at": at.isoformat(), "recorded_at": now.isoformat(),
                 "counterparty_institution": body.counterparty_institution},
    )
    return {"case": _fetch_case(conn, case_id, user), "clocks": clock_sweep.clocks_for_case(conn, case_id)}


# ---------------------------------------------------------------------------
# The twenty-four hour flag (WP-06, D73)
# ---------------------------------------------------------------------------


def _identity(conn: Any, subject_token: str) -> dict[str, Any]:
    rows = _rows(
        conn,
        """
        SELECT ci.source, ci.verification_status, ci.verified_at, ci.verification_source,
               (SELECT count(*) FROM customer_identities o WHERE o.bvn_token = ci.bvn_token) AS records_with_this_bvn
          FROM customer_identities ci WHERE ci.subject_token = %s
        """,
        (subject_token,),
    )
    return {"bvn_known": bool(rows), **(rows[0] if rows else {})}


def _watchlist_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except watchlist.WatchlistError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/cases/{case_id}/watchlist")
def place_watchlist_flag(
    case_id: int,
    body: WatchlistPlaceIn,
    user: dict = Depends(requires(Permission.CASES_ESCALATE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Flag the case's customer for at most 24 hours while the bank contacts them.

    Risk Radar records the flag; the bank applies it to the customer's BVN.
    """
    case = _fetch_case(conn, case_id, user)
    flag = _watchlist_call(watchlist.place, conn, case=case, user_id=user["id"],
                           reason=body.reason, hours=body.hours)
    return {"flag": flag}


@router.post("/watchlist/{flag_id}/contact")
def record_watchlist_contact(
    flag_id: int,
    body: WatchlistContactIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The customer was reached. Recorded once, while the flag is active."""
    flag = _watchlist_call(watchlist.record_contact, conn, flag_id=flag_id, user_id=user["id"],
                           outcome=body.outcome)
    if flag["case_id"]:
        _note(conn, flag["case_id"], user, body.note)
    return {"flag": flag}


@router.post("/watchlist/{flag_id}/lift")
def lift_watchlist_flag(
    flag_id: int,
    body: WatchlistLiftIn,
    user: dict = Depends(requires(Permission.CASES_ESCALATE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Lift early because the customer is cleared. Expiry needs no call."""
    flag = _watchlist_call(watchlist.lift, conn, flag_id=flag_id, user_id=user["id"], note=body.note)
    if flag["case_id"]:
        _note(conn, flag["case_id"], user, body.note)
    return {"flag": flag}


@router.get("/watchlist")
def list_watchlist(
    active: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Every flag in force now (or recent ones, ``active=false``), soonest to expire first."""
    where = "w.lifted_at IS NULL AND w.expires_at > now()" if active else "true"
    order = "w.expires_at ASC" if active else "w.placed_at DESC"
    rows = _rows(conn, f"SELECT {watchlist.FLAG_COLUMNS} FROM subject_watchlist w "
                       f"WHERE {where} ORDER BY {order} LIMIT %s", (limit,))
    now = watchlist._now(conn)
    return {"items": [watchlist.describe(r, now) for r in rows]}


@router.get("/clocks/policy")
def clock_policy(
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The clock policy in force and the holiday calendar it counts against."""
    policy = clock_sweep.active_policy(conn)
    holidays = _rows(
        conn,
        "SELECT holiday_date, name, confirmed, source FROM public_holidays "
        "WHERE holiday_date >= current_date - 30 ORDER BY holiday_date",
    )
    return {"policy": policy, "holidays": holidays}


@router.post("/cases/{case_id}/assign")
def assign(
    case_id: int,
    body: AssignIn,
    user: dict = Depends(requires(Permission.CASES_REASSIGN)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """D94: the audited exception to automatic routing.

    A reason is required, the version the caller read is checked, and the
    case's own age is untouched — moving it between people does not make it
    younger, and the SLA it is already late against still applies.
    """
    case = _fetch_case(conn, case_id, user)
    if body.expected_case_version is not None and int(case["version"]) != body.expected_case_version:
        raise HTTPException(409, f"the case has moved on: it is at version {case['version']}, "
                                 f"you read version {body.expected_case_version}")
    if body.assignee_id is not None:
        holder = _rows(conn, "SELECT id, role::text AS role, active FROM users WHERE id = %s", (body.assignee_id,))
        if not holder or not holder[0]["active"]:
            raise HTTPException(422, "that account cannot hold a case: unknown or deactivated")
        if holder[0]["role"] not in ("ANALYST", "FRAUD_OPS_LEAD", "INFOSEC_ANALYST"):
            raise HTTPException(422, f"a {holder[0]['role']} does not work cases (D12b)")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cases SET assignee_id = %s, assigned_at = now(), assignment_reason = %s, "
            "version = version + 1 WHERE id = %s",
            (body.assignee_id, f"reassigned by hand: {body.reason}", case_id),
        )
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_REASSIGNED",
        object_type="case",
        object_id=case_id,
        from_state=str(case["assignee_id"]),
        to_state=str(body.assignee_id),
        payload={"reason": body.reason, "was_automatic": (case["assignment_reason"] or "").startswith("automatic")},
    )
    if body.assignee_id:
        publish(conn, "case_assigned", {"case_id": case_id, "assignee_id": body.assignee_id,
                                        "reason": "reassigned by a lead"})
    return _fetch_case(conn, case_id, user)


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
    from ...cases import submissions as subs

    case = _fetch_case(conn, case_id, user)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "case is already closed")
    # D93 / plan §4.3: closing is objective. Every condition is named, and the
    # answer says which one is missing rather than "cannot close".
    blockers = subs.closure_blockers(conn, case_id)
    if blockers:
        raise HTTPException(409, {"detail": "this case cannot close yet", "blockers": blockers})

    promoted: list[str] = []
    if case["outcome"] == "CONFIRMED_FRAUD":
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO beneficiary_lists (kind, token, note, added_by)
                SELECT DISTINCT 'KNOWN_MULE'::beneficiary_list_kind, t.beneficiary_token,
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

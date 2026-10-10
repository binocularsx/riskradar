"""Completed cases, and each one's decision record (for leads).

A lead answers for what the desk decided. Two questions come back weeks later,
from an auditor, the regulator, a customer complaint or the support team: "what
did we decide on this case?" and "who decided it, on what evidence, and what
happened next?". This answers both from what is already recorded:

* ``GET /v1/completed-cases`` - closed cases, newest first, with the outcome,
  who proposed it, who approved it and who closed it.
* ``GET /v1/completed-cases/{case_id}/record`` - one case's decision record:
  every proposal and its decision (D93), the actions asked of support and what
  support answered (D97, D109b), the messages sent to support (D109c), the
  regulatory milestones (D71), and the case's own entries in the hash-chained
  audit log (D12c), which is the authority when the rest disagrees.

Nothing here is new data and nothing is writable. Leads only (cases:close):
an analyst's own work is on their cases, and an administrator sees no case
content at all (D12b).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...security.rbac import Permission
from ..deps import get_conn, requires

router = APIRouter(prefix="/v1/completed-cases", tags=["completed cases"])

OUTCOMES = ("CONFIRMED_FRAUD", "FALSE_POSITIVE", "INCONCLUSIVE")


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@router.get("")
def completed_cases(
    outcome: str | None = Query(None),
    q: str | None = Query(None, max_length=128, description="case number or support ticket"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(requires(Permission.CASES_CLOSE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    where = ["c.state = 'CLOSED'"]
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    if outcome:
        if outcome not in OUTCOMES:
            raise HTTPException(422, f"outcome must be one of {', '.join(OUTCOMES)}")
        where.append("c.outcome = %(outcome)s")
        params["outcome"] = outcome
    if q:
        term = q.strip().upper().removeprefix("CASE-")
        where.append("(c.id::text = %(q)s OR c.support_ticket_ref ILIKE %(qlike)s)")
        params.update(q=term, qlike=f"%{q.strip()}%")
    rows = _rows(
        conn,
        f"""
        SELECT c.id, c.outcome::text AS outcome, c.risk_level::text AS risk_level,
               c.opened_at, c.closed_at, c.alert_count, c.support_ticket_ref,
               (c.first_reported_at IS NOT NULL) AS reported_by_support,
               closer.display_name AS closed_by,
               s.submitted_at, proposer.display_name AS proposed_by,
               approver.display_name AS approved_by, s.decided_at AS approved_at,
               direct.recorded_by,
               (SELECT coalesce(sum(t.amount_minor) FILTER (WHERE t.auth_result = 'APPROVED'), 0)
                  FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id)::bigint AS exposure_minor
          FROM cases c
          LEFT JOIN users closer ON closer.id = c.closed_by
          LEFT JOIN LATERAL (
                SELECT * FROM fraud_submissions fs
                 WHERE fs.case_id = c.id AND fs.state = 'APPROVED'
                 ORDER BY fs.decided_at DESC LIMIT 1) s ON true
          LEFT JOIN users proposer ON proposer.id = s.submitted_by
          LEFT JOIN users approver ON approver.id = s.decided_by
          -- Before the maker-checker (D93) the outcome was recorded in one
          -- step; the audit log names who recorded it.
          LEFT JOIN LATERAL (
                SELECT u.display_name AS recorded_by FROM audit_log l JOIN users u ON u.id = l.actor_user_id
                 WHERE l.object_type = 'case' AND l.object_id = c.id::text AND l.action = 'CASE_OUTCOME_SET'
                 ORDER BY l.id DESC LIMIT 1) direct ON s.id IS NULL
         WHERE {' AND '.join(where)}
         ORDER BY c.closed_at DESC NULLS LAST, c.id DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    totals = _rows(
        conn,
        "SELECT outcome::text AS outcome, count(*) AS n FROM cases WHERE state = 'CLOSED' GROUP BY 1",
    )
    return {
        "items": rows[:limit],
        "has_more": len(rows) > limit,
        "limit": limit,
        "offset": offset,
        "totals": {r["outcome"] or "NONE": int(r["n"]) for r in totals},
    }


@router.get("/{case_id}/record")
def decision_record(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_CLOSE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _rows(
        conn,
        """
        SELECT c.id, c.state::text AS state, c.outcome::text AS outcome, c.risk_level::text AS risk_level,
               c.opened_at, c.closed_at, c.alert_count, c.handling::text AS handling,
               c.support_ticket_ref, c.first_reported_at, c.report_channel, c.counterparty_institution,
               c.acknowledged_at, c.counterparty_notified_at, c.investigation_concluded_at, c.reimbursed_at,
               c.escalated_at, c.escalation_reason, c.escalated_to::text AS escalated_to,
               closer.display_name AS closed_by, assignee.display_name AS assignee,
               (SELECT coalesce(sum(t.amount_minor) FILTER (WHERE t.auth_result = 'APPROVED'), 0)
                  FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id)::bigint AS exposure_minor,
               (SELECT count(*) FROM alerts a WHERE a.case_id = c.id AND a.source = 'CUSTOMER_REPORT')
                   AS missed_by_detector
          FROM cases c
          LEFT JOIN users closer   ON closer.id = c.closed_by
          LEFT JOIN users assignee ON assignee.id = c.assignee_id
         WHERE c.id = %s
        """,
        (case_id,),
    )
    if not case:
        raise HTTPException(404, "case not found")
    case = case[0]
    if case["state"] != "CLOSED":
        raise HTTPException(409, "this case is still open; its record is on the case page until it closes")

    proposals = _rows(
        conn,
        """
        SELECT s.id, s.proposed_outcome::text AS proposed_outcome, s.rationale, s.state::text AS state,
               s.submitted_at, s.decided_at, s.decision_reason, s.restrictions,
               p.display_name AS proposed_by, d.display_name AS decided_by
          FROM fraud_submissions s
          JOIN users p ON p.id = s.submitted_by
          LEFT JOIN users d ON d.id = s.decided_by
         WHERE s.case_id = %s
         ORDER BY s.submitted_at
        """,
        (case_id,),
    )
    actions = _rows(
        conn,
        """
        SELECT o.restriction_ref, o.action::text AS action, o.kind, o.transaction_ref, o.reason,
               o.issued_at, o.first_delivered_at, o.acknowledged_at, o.ack_outcome::text AS ack_outcome,
               o.ack_reason, a.display_name AS approved_by
          FROM restriction_orders o
          LEFT JOIN users a ON a.id = o.approved_by
         WHERE o.case_id = %s
         ORDER BY o.issued_at
        """,
        (case_id,),
    )
    messages = _rows(
        conn,
        """
        SELECT report_ref, kind, status, created_at, sent_at, payload->>'outcome' AS outcome
          FROM support_reports WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    )
    # The hash-chained audit log is the authority (D12c). Case opens are left
    # out: who looked at a case is an access record, not part of the decision.
    trail = _rows(
        conn,
        """
        SELECT l.occurred_at, l.action, l.from_state, l.to_state, l.payload,
               u.display_name AS actor, u.role::text AS actor_role
          FROM audit_log l
          LEFT JOIN users u ON u.id = l.actor_user_id
         WHERE l.object_type = 'case' AND l.object_id = %s AND l.action <> 'CASE_VIEWED'
         ORDER BY l.id
        """,
        (str(case_id),),
    )
    # Before the maker-checker (D93) an outcome was recorded in one step. For
    # those cases the audit entry is the decision, so it is named as such.
    direct = next((e for e in reversed(trail) if e["action"] == "CASE_OUTCOME_SET"), None)
    approved = next((p for p in reversed(proposals) if p["state"] == "APPROVED"), None)
    decision = (
        {"how": "APPROVED_PROPOSAL", "outcome": approved["proposed_outcome"],
         "proposed_by": approved["proposed_by"], "proposed_at": approved["submitted_at"],
         "rationale": approved["rationale"], "approved_by": approved["decided_by"],
         "approved_at": approved["decided_at"], "approval_reason": approved["decision_reason"]}
        if approved else
        {"how": "RECORDED_DIRECTLY", "outcome": case["outcome"],
         "recorded_by": direct["actor"] if direct else None,
         "recorded_at": direct["occurred_at"] if direct else None,
         "rationale": ((direct or {}).get("payload") or {}).get("rationale")
                      or ((direct or {}).get("payload") or {}).get("reason")}
    )
    return {"case": case, "decision": decision, "proposals": proposals, "actions": actions,
            "messages": messages, "trail": trail}

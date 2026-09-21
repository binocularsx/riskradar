"""Propose a fraud finding, and decide someone else's (D93).

* ``POST /v1/cases/{id}/submissions`` — the analyst proposes an outcome, with
  the reason, the evidence they read, and any account restriction they are
  asking the bank for. It decides nothing.
* ``GET /v1/cases/{id}/submissions`` — every proposal this case has had, in
  order, with who decided what and why. Nothing is ever edited away.
* ``GET /v1/approvals`` — the lead's queue: proposals waiting, oldest and most
  urgent first, never including the reader's own.
* ``POST /v1/submissions/{id}/decision`` — approve, reject or return, with a
  reason. Only an approval writes the outcome onto the case.
* ``GET /v1/submissions/catalog`` — the restriction actions a submission may
  ask for, so the console offers a list rather than a free-text box.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...cases import submissions
from ...security import visibility
from ...security.rbac import Permission
from ..deps import get_conn, requires
from ..schemas import FraudDecisionIn, FraudSubmissionIn

router = APIRouter(prefix="/v1", tags=["fraud approval"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@router.get("/submissions/catalog")
def catalog(user: dict = Depends(requires(Permission.CASES_READ))) -> dict[str, Any]:
    return {
        "restriction_actions": submissions.RESTRICTION_ACTIONS,
        "outcomes": ["CONFIRMED_FRAUD", "FALSE_POSITIVE", "INCONCLUSIVE"],
        "decisions": {"APPROVE": "the finding stands and any restriction is authorised",
                      "REJECT": "the finding does not stand; the case stays open for the desk",
                      "RETURN": "send it back to the analyst with what is missing"},
        "note": "An analyst proposes; a different lead decides. Only an approval writes the outcome.",
    }


@router.post("/cases/{case_id}/submissions")
def submit(
    case_id: int,
    body: FraudSubmissionIn,
    user: dict = Depends(requires(Permission.CASES_SUBMIT_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    try:
        out = submissions.submit(
            conn, case_id=case_id, user=user, proposed_outcome=body.proposed_outcome,
            rationale=body.rationale, restrictions=[r.model_dump() for r in body.restrictions],
            expected_version=body.expected_case_version,
        )
    except submissions.SubmissionError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"submission": out, "waiting_for": "a Fraud Ops Lead who did not write it"}


@router.get("/cases/{case_id}/submissions")
def history(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    if not visibility.can_read(conn, user, case_id):
        raise HTTPException(404, "case not found")
    items = _rows(
        conn,
        """
        SELECT s.*, u.display_name AS submitted_by_name, d.display_name AS decided_by_name
          FROM fraud_submissions s
          JOIN users u ON u.id = s.submitted_by
          LEFT JOIN users d ON d.id = s.decided_by
         WHERE s.case_id = %s
         ORDER BY s.submitted_at DESC
        """,
        (case_id,),
    )
    return {"items": items, "pending": next((i for i in items if i["state"] == "PENDING"), None)}


@router.get("/approvals")
def queue(
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(requires(Permission.CASES_APPROVE_FRAUD)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Waiting proposals, never the reader's own: a lead's queue is other people's work."""
    items = _rows(
        conn,
        """
        SELECT s.id, s.case_id, s.proposed_outcome, s.rationale, s.restrictions, s.submitted_at,
               s.case_version, u.display_name AS submitted_by_name, s.submitted_by,
               c.risk_level::text AS risk_level, c.state::text AS case_state, c.first_reported_at,
               extract(epoch FROM now() - s.submitted_at)::int AS waiting_seconds,
               (SELECT count(*) FROM alerts a WHERE a.case_id = c.id) AS alert_count,
               (SELECT coalesce(sum(t.amount_minor), 0)::bigint FROM alerts a
                  JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id AND t.auth_result = 'APPROVED') AS exposure_minor
          FROM fraud_submissions s
          JOIN cases c ON c.id = s.case_id
          JOIN users u ON u.id = s.submitted_by
         WHERE s.state = 'PENDING' AND s.submitted_by <> %s
         ORDER BY c.risk_level DESC, s.submitted_at
         LIMIT %s
        """,
        (user["id"], limit),
    )
    mine = _rows(
        conn,
        "SELECT count(*) AS n FROM fraud_submissions WHERE state = 'PENDING' AND submitted_by = %s",
        (user["id"],),
    )[0]["n"]
    return {
        "items": items,
        "summary": {"waiting": len(items), "oldest_seconds": max((i["waiting_seconds"] for i in items), default=0),
                    "with_restrictions": sum(1 for i in items if i["restrictions"]),
                    "mine_awaiting_someone_else": int(mine)},
    }


@router.post("/submissions/{submission_id}/decision")
def decide(
    submission_id: int,
    body: FraudDecisionIn,
    user: dict = Depends(requires(Permission.CASES_APPROVE_FRAUD)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    try:
        return submissions.decide(conn, submission_id=submission_id, user=user,
                                  decision=body.decision, reason=body.reason)
    except submissions.SubmissionError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

"""Delivering approved restrictions to the bank, and reading them back (D97).

Plain English
-------------
Two audiences, as with directives (D74):

* the **bank's systems** (API key, like ingestion): poll the recommendations
  addressed to them, read one, and acknowledge what they did about it.
* the **fraud desk** (a session): see the restrictions on a case, scoped by the
  same visibility rule as every other case read (D94), and the delivery health.

Risk Radar restricts nothing (D7): these routes carry a recommendation to the
bank and record the bank's answer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ... import governance
from ... import restrictions as delivery
from ...security import visibility
from ...security.rbac import Permission
from ..deps import current_user, get_conn, require_api_key, requires
from ..schemas import ConfigDecisionIn, DeliveryPauseIn, ReleaseRequestIn, RestrictionAckIn

router = APIRouter(prefix="/v1/restrictions", tags=["restrictions"])
staff_router = APIRouter(prefix="/v1", tags=["restrictions"])


def _contract(order: dict[str, Any]) -> dict[str, Any]:
    """The bank-facing view of an order: the ask, the target tokens, and status."""
    if order["acknowledged_at"] is not None:
        state = order["ack_outcome"]
    elif order["first_delivered_at"] is not None:
        state = "DELIVERED"
    else:
        state = "RECOMMENDED"
    return {
        "restriction_ref": str(order["restriction_ref"]),
        "action": order["action"],
        "account_token": order["account_token"],
        "beneficiary_token": order["beneficiary_token"],
        "channel": order["channel"],
        "reason": order["reason"],
        "issued_at": order["issued_at"],
        "status": state,
        "acknowledged_at": order["acknowledged_at"],
        "ack_outcome": order["ack_outcome"],
    }


# ---------------------------------------------------------------------------
# Bank-facing (API key)
# ---------------------------------------------------------------------------


@router.get("")
def restriction_feed(
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    """Restrictions issued after ``after_id``, oldest first. Fetching records
    that the bank was told. Poll with the last ``id``."""
    rows = delivery.feed(conn, after_id=after_id, limit=limit)
    delivery.mark_delivered(conn, [r["id"] for r in rows])
    return {
        "items": [{"id": r["id"], **_contract(r)} for r in rows],
        "next_after_id": rows[-1]["id"] if rows else after_id,
    }


@router.get("/{restriction_ref}")
def get_restriction(
    restriction_ref: UUID,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    order = delivery.by_ref(conn, str(restriction_ref))
    if not order:
        raise HTTPException(404, "unknown restriction_ref")
    delivery.mark_delivered(conn, [order["id"]])
    return _contract(order)


@router.post("/{restriction_ref}/ack")
def acknowledge_restriction(
    restriction_ref: UUID,
    body: RestrictionAckIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    """The bank reports the outcome. Once; a repeat with the same answer is a
    no-op, a different answer is a 409."""
    try:
        result = delivery.acknowledge(
            conn, restriction_ref=str(restriction_ref), outcome=body.outcome,
            reason=body.reason, taken_at=body.taken_at,
        )
    except delivery.RestrictionError as exc:
        raise HTTPException(exc.status, exc.detail)
    if result["status"] == "duplicate":
        response.status_code = status.HTTP_200_OK
    return result


# ---------------------------------------------------------------------------
# The fraud desk (session)
# ---------------------------------------------------------------------------


@staff_router.get("/cases/{case_id}/restrictions")
def case_restrictions(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    # 404, not 403, outside the caller's scope (D94).
    if not visibility.can_read(conn, user, case_id):
        raise HTTPException(404, "case not found")
    return {"items": [_contract(o) for o in delivery.for_case(conn, case_id)]}


@staff_router.post("/restrictions/{restriction_ref}/release-request")
def request_release(
    restriction_ref: UUID,
    body: ReleaseRequestIn,
    user: dict = Depends(requires(Permission.CASES_SUBMIT_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Ask for a restriction to be lifted (D103).

    Anyone who may work a case may ask; a Fraud Ops lead who did not ask
    decides. Unblocking a customer is as consequential as blocking one, so it
    takes the same two signatures — and the original order is never rewritten,
    the lift is its own order beside it.
    """
    order = delivery.order_by_ref(conn, str(restriction_ref))
    if order is None or not visibility.can_read(conn, user, order["case_id"]):
        raise HTTPException(404, "restriction not found")
    blockers = delivery.release_blockers(conn, order)
    if blockers:
        raise HTTPException(409, {"detail": "this restriction cannot be lifted", "blockers": blockers})
    try:
        return governance.propose(
            conn, user,
            change_type="RESTRICTION_RELEASE",
            target=str(restriction_ref),
            summary=(f"lift {order['action'].replace('_', ' ').lower()} on case {order['case_id']}"),
            payload={"restriction_ref": str(restriction_ref), "reason": body.reason,
                     "case_id": order["case_id"]},
            before_snapshot={"action": order["action"], "ack_outcome": order["ack_outcome"],
                             "acknowledged_at": order["acknowledged_at"]},
            rationale=body.reason,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


@staff_router.get("/restrictions/release-requests")
def list_release_requests(
    state: str = Query("PENDING", pattern="^(PENDING|APPROVED|REJECTED|RETURNED|SUPERSEDED)$"),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Lifts waiting for a decision, and what happened to the ones decided (D103).

    Scoped like every other case read: a request names a case, and a person who
    may not see the case does not see the request to unblock its customer.
    """
    items = [r for r in governance.list_requests(conn, states=(state,))
             if r["change_type"] == "RESTRICTION_RELEASE"]
    case_ids = [int((r["payload"] or {}).get("case_id") or 0) for r in items]
    allowed = visibility.visible_case_ids(conn, user, [c for c in case_ids if c])
    mine = [r for r in items
            if visibility.sees_everything(user) or int((r["payload"] or {}).get("case_id") or 0) in allowed]
    return {"items": mine,
            "summary": {"waiting": sum(1 for r in mine if r["state"] == "PENDING"),
                        "yours_waiting_for_someone_else":
                            sum(1 for r in mine if r["state"] == "PENDING" and r["proposed_by_id"] == user["id"])}}


@staff_router.post("/restrictions/release-requests/{request_id}/decision")
def decide_release_request(
    request_id: int,
    body: ConfigDecisionIn,
    user: dict = Depends(requires(Permission.CASES_APPROVE_FRAUD)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """A lead decides somebody else's request to lift a restriction (D103).

    The desk's own door to the same mechanism the administrators use, because a
    lead should not have to walk through /v1/admin to answer for a case they
    own. `governance.decide` still checks the authority and the separation.
    """
    try:
        return governance.decide(conn, user, request_id=request_id, action=body.action, reason=body.reason)
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


@staff_router.get("/metrics/restrictions/reconciliation")
def reconciliation(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """What the bank owes an answer on, and what has outlived its reason (D103)."""
    return delivery.reconcile(conn)


@staff_router.post("/restrictions/delivery/pause")
def pause_delivery(
    body: DeliveryPauseIn,
    user: dict = Depends(requires(Permission.CASES_CLOSE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Stop or resume sending restrictions to the bank (D103, plan §5.3).

    The desk's own stop button, held by the Fraud Ops lead who carries the
    operational risk rather than by an administrator who cannot see a case
    (D12b). Detection, investigation and approval carry on; only the outbound
    messages wait, and they wait in a durable outbox rather than being lost.
    """
    return delivery.set_paused(conn, on=body.paused, reason=body.reason, actor=user)


@staff_router.get("/metrics/restrictions")
def restriction_status(
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Delivery health and what the bank has said (FR-305 for restrictions)."""
    return delivery.status(conn)

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

from ... import restrictions as delivery
from ...security import visibility
from ...security.rbac import Permission
from ..deps import current_user, get_conn, require_api_key, requires
from ..schemas import RestrictionAckIn

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


@staff_router.get("/metrics/restrictions")
def restriction_status(
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Delivery health and what the bank has said (FR-305 for restrictions)."""
    return delivery.status(conn)

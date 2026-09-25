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
from ... import reporting as account_manager_reporting
from ... import restrictions as delivery
from ...security import visibility
from ...security.rbac import Permission
from ..deps import current_user, get_conn, require_api_key, requires
from ..schemas import ConfigDecisionIn, DeliveryPauseIn, ReleaseRequestIn, RestrictionAckIn

# A submission carries at most twenty restrictions; reversals must not crowd
# out the account-level actions on a busy case.
REVERSAL_SUGGESTION_CAP = 8

# A submission and a decision each accept at most twenty restrictions, so a
# longer suggestion list is not merely untidy: checking all of it produces a
# 422 at the moment the lead approves, which is the worst possible time to
# find out. Truncated here, in the order below, and the caller is told.
MAX_SUGGESTIONS = 20

# D108: the rules that mean somebody else is holding the customer's login.
TAKEOVER_SIGNALS = frozenset({"ACCOUNT_TAKEOVER_SEQUENCE", "SIM_SWAP_TRANSFER"})


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


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
        "transaction_ref": order.get("transaction_ref"),
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


@staff_router.get("/cases/{case_id}/restriction-suggestions")
def restriction_suggestions(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_SUBMIT_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """What to block, derived from the case's own alerted transactions (D106).

    Confirming fraud used to leave the restriction list empty unless somebody
    remembered to fill it in, so the commonest outcome of a confirmed fraud was
    that nothing was blocked at all. This proposes the obvious set from the
    evidence already on the case, and the analyst edits it before submitting —
    automatic by default, still a human's decision (D7, D93).

    Every suggestion carries the reason it is suggested, because a restriction
    reaches a real customer and "the system proposed it" is not a reason.
    """
    if not visibility.can_read(conn, user, case_id):
        raise HTTPException(404, "case not found")

    rows = _rows(
        conn,
        """
        SELECT t.account_token, t.beneficiary_token, t.instrument::text AS instrument,
               t.transaction_ref, t.amount_minor, t.auth_result::text AS auth_result
          FROM alerts a JOIN transactions t ON t.id = a.transaction_id
         WHERE a.case_id = %s
         ORDER BY t.amount_minor DESC
        """,
        (case_id,),
    )

    accounts = {r["account_token"] for r in rows if r["account_token"]}
    beneficiaries = {r["beneficiary_token"] for r in rows if r["beneficiary_token"]}
    carded = {r["account_token"] for r in rows if r["account_token"] and r["instrument"] == "CARD"}

    items: list[dict[str, Any]] = []
    for token in sorted(accounts):
        items.append({
            "action": "DEBIT_RESTRICTION", "account_token": token,
            "beneficiary_token": None, "channel": None, "transaction_ref": None,
            "reason": f"confirmed fraud on case {case_id}",
            "why": "this account sent the payments the case was raised on",
        })
    for token in sorted(carded):
        items.append({
            "action": "CARD_FREEZE", "account_token": token,
            "beneficiary_token": None, "channel": None, "transaction_ref": None,
            "reason": f"confirmed fraud on case {case_id}",
            "why": "at least one alerted payment on this account was made by card",
        })
    # D108: containment for a compromised login, proposed only when the case
    # actually looks like one. Offering a credential reset on a card-testing
    # case would be noise, and noise is how a control stops being read.
    signals = {r["code"] for r in _rows(
        conn,
        """
        SELECT DISTINCT s->>'code' AS code
          FROM alerts a
          JOIN decisions d ON d.id = a.decision_id,
               jsonb_array_elements(d.signals) s
         WHERE a.case_id = %s
        """,
        (case_id,),
    ) if r["code"]}
    compromised = signals & TAKEOVER_SIGNALS
    if compromised:
        why = ("this case fired " + ", ".join(sorted(c.replace("_", " ").lower()
                                                     for c in compromised)))
        for token in sorted(accounts):
            for action, purpose in (
                ("SESSION_TERMINATION", "sign the attacker out of any live session"),
                ("CREDENTIAL_RESET", "the password should be assumed known"),
                ("MFA_REENROLMENT", "the second factor may be on the attacker's device"),
            ):
                items.append({
                    "action": action, "account_token": token,
                    "beneficiary_token": None, "channel": None, "transaction_ref": None,
                    "reason": f"suspected account takeover on case {case_id}",
                    "why": f"{why} - {purpose}",
                })

    for token in sorted(beneficiaries):
        items.append({
            "action": "BENEFICIARY_RESTRICTION", "account_token": None,
            "beneficiary_token": token, "channel": None, "transaction_ref": None,
            "reason": f"confirmed fraud on case {case_id}",
            "why": "money on this case went to this destination",
        })

    # D107: the money that already moved. Only approved payments — a declined
    # one moved nothing and asking to reverse it is noise the bank has to
    # triage. Largest first, and capped: the submission takes twenty
    # restrictions in total, and a case with forty payments would otherwise
    # bury the account-level actions under a wall of reversals.
    reversible = [r for r in rows if r["auth_result"] == "APPROVED" and r["transaction_ref"]]
    for r in reversible[:REVERSAL_SUGGESTION_CAP]:
        items.append({
            "action": "TRANSACTION_REVERSAL", "account_token": r["account_token"],
            "beneficiary_token": r["beneficiary_token"], "channel": None,
            "transaction_ref": r["transaction_ref"],
            "reason": f"confirmed fraud on case {case_id}",
            "why": f"approved payment of {r['amount_minor'] / 100:,.2f} on this case",
        })

    omitted = max(0, len(items) - MAX_SUGGESTIONS)
    items = items[:MAX_SUGGESTIONS]
    return {"items": items,
            "omitted": omitted,
            "reversible_total": len(reversible),
            "note": ("Suggested from this case's alerted transactions. Edit or remove any of "
                     "them before submitting; a lead sets what is finally asked of the bank "
                     "when they approve."
                     + (f" Showing the {REVERSAL_SUGGESTION_CAP} largest of {len(reversible)} "
                        f"reversible payments." if len(reversible) > REVERSAL_SUGGESTION_CAP else "")
                     + (f" {omitted} further suggestion(s) were left out: a finding carries at "
                        f"most {MAX_SUGGESTIONS}." if omitted else ""))}


@staff_router.get("/cases/{case_id}/account-manager-reports")
def case_account_manager_reports(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Whether the customer's account manager has actually been told (D106)."""
    if not visibility.can_read(conn, user, case_id):
        raise HTTPException(404, "case not found")
    return {"items": account_manager_reporting.for_case(conn, case_id)}


@staff_router.get("/metrics/account-manager-reports")
def account_manager_report_status(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Delivery health for the reports (FR-305, as for restrictions). A report
    nobody received is worse than none, because the desk believes it landed."""
    return account_manager_reporting.status(conn)


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

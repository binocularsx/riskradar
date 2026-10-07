"""The support team's side of the desk (D109).

The fraud desk is not customer-facing. The bank's support team is, and it talks
to Risk Radar as an external system with an API key (D109a), never a login:

* ``POST /v1/support/reports`` — a customer told support they were defrauded.
  Support forwards it with the matching customer record; it opens or joins the
  customer's case exactly as D90 did, and the desk reviews it.
* ``POST /v1/support/watchlist/{case_id}/contact`` — support reached a customer
  the desk placed on the 24-hour watch (D109f).

What the desk sends back (reports, heads-ups, contact requests) leaves through
the support outbox (``riskradar.reporting``); the actions support is asked to
take are acknowledged on the existing restriction routes (D97, D109b).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...audit import chain
from ...cases import reports
from ...clocks import sweep as clock_sweep
from ...clocks import watchlist
from ...security.tokens import subject_token
from ...worker.scoring import system_user_id
from ..deps import get_conn, require_api_key
from ..schemas import SupportContactIn, SupportReportIn

router = APIRouter(prefix="/v1/support", tags=["support"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@router.post("/reports")
def support_report(
    body: SupportReportIn,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    """A customer's fraud report, forwarded by support with the customer record.

    The record has to match: every payment named must belong to the customer
    named, or the report is refused and the mismatch is named. A report filed
    against the wrong customer would put the wrong person's account in front
    of the desk, and in front of the regulator's clock.
    """
    subject = subject_token(body.customer_id)
    owners = _rows(conn, "SELECT transaction_ref, subject_token FROM transactions WHERE transaction_ref = ANY(%s)",
                   (body.transaction_refs,))
    mismatched = [r["transaction_ref"] for r in owners if r["subject_token"] != subject]
    if mismatched:
        raise HTTPException(422, "these payments do not belong to the customer named in the report: "
                                 + ", ".join(mismatched[:5]))

    sys_uid = system_user_id(conn)
    actor = {"id": sys_uid}
    note = body.customer_statement and f"Customer's statement, via support ticket {body.support_ticket_ref}: " \
                                        f"{body.customer_statement}"
    try:
        done = reports.report_payments(
            conn, user=actor, sys_uid=sys_uid, transaction_refs=body.transaction_refs,
            reported_at=body.reported_at, channel=body.channel,
            counterparty_institution=body.counterparty_institution, note=note,
        )
    except reports.ReportError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    with conn.cursor() as cur:
        # The first ticket is the one the report back quotes; a second ticket on
        # the same case is recorded in the audit, not written over the first.
        cur.execute("UPDATE cases SET support_ticket_ref = COALESCE(support_ticket_ref, %s) "
                    "WHERE id = ANY(%s)", (body.support_ticket_ref, done["cases"]))
    for case_id in done["cases"]:
        chain.append(conn, actor_user_id=sys_uid, action="SUPPORT_REPORT_RECEIVED", object_type="case",
                     object_id=case_id, payload={"support_ticket_ref": body.support_ticket_ref,
                                                 "api_key": api_key["name"],
                                                 "transaction_refs": body.transaction_refs})
    done["support_ticket_ref"] = body.support_ticket_ref
    done["clocks"] = {cid: clock_sweep.clocks_for_case(conn, cid) for cid in done["cases"]}
    return done


@router.post("/watchlist/{case_id}/contact")
def support_contact(
    case_id: int,
    body: SupportContactIn,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    """Support reached the customer the desk asked them to contact (D109f)."""
    sys_uid = system_user_id(conn)
    try:
        flag = watchlist.record_contact_for_case(conn, case_id=case_id, user_id=sys_uid, outcome=body.outcome)
    except watchlist.WatchlistError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if body.note:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                        (case_id, sys_uid, f"From support: {body.note}"))
    return {"flag": flag}

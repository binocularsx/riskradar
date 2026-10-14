"""Ingestion — the front door (FR-001 to FR-007).

Plain English
-------------
The front door. Every transaction the system ever sees comes through here.

It does four jobs before anything else happens: check the caller is allowed in,
check the transaction is properly formed (and if not, keep the broken one on
file rather than quietly fixing it), scramble the account numbers, and save it
together with a note telling the scoring engine there is work to do.

That last part is one database operation, not two. If it were two, a crash in
between would leave a transaction saved but never scored — and nobody would
notice, because it would look exactly like a transaction that had been
scored and found boring.

One versioned, authenticated endpoint. The simulator is a pure external client of
it (D8): it queues at the same door a bank would, which is what makes it
deletable without touching the product, and what gets the door tested constantly
for five weeks.

The boundary does three things nothing downstream may repeat:

1. **Tokenises.** Raw identifiers never reach the database (FR-004).
2. **Stamps point-in-time account context** (D22), so the interior never joins a
   mutable table and every decision stays reproducible.
3. **Persists and enqueues in one transaction** (FR-005). A crash between those
   two writes would lose work silently, which is the worst kind.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ...identity import boundary as identity
from ...security.tokens import (
    account_token,
    beneficiary_token,
    device_token,
    msisdn_token,
    subject_token,
)
from .. import flow
from ..deps import get_conn, require_api_key
from ..schemas import (
    BatchAccepted,
    BatchIn,
    EventAccepted,
    EventBatchAccepted,
    EventBatchIn,
    EventIn,
    PaymentEventIn,
    TransactionAccepted,
    TransactionIn,
)

router = APIRouter(prefix="/v1/transactions", tags=["ingestion"])
# WP-01 (D72): the general door. /v1/transactions stays as the payment-only
# door every existing caller, the simulator included, already uses.
events_router = APIRouter(prefix="/v1/events", tags=["ingestion"])


class EventRefConflict(Exception):
    """An idempotency reference already used by an event of a different type."""


INSERT_SQL = """
    INSERT INTO transactions (
        transaction_ref, occurred_at, amount_minor, currency,
        channel, instrument, rail,
        subject_token, account_token, beneficiary_token,
        device_token, ip_region, merchant_category,
        auth_result, decline_reason, display_name,
        account_opened_at, last_activity_at, product_type, origin_sol_id,
        is_replay, raise_alerts, bvn_token, direction, remitter_token, remitter_bank_code
    ) VALUES (
        %(transaction_ref)s, %(occurred_at)s, %(amount_minor)s, %(currency)s,
        %(channel)s, %(instrument)s, %(rail)s,
        %(subject_token)s, %(account_token)s, %(beneficiary_token)s,
        %(device_token)s, %(ip_region)s, %(merchant_category)s,
        %(auth_result)s, %(decline_reason)s, %(display_name)s,
        %(account_opened_at)s, %(last_activity_at)s, %(product_type)s, %(origin_sol_id)s,
        %(is_replay)s, %(raise_alerts)s, %(bvn_token)s, %(direction)s, %(remitter_token)s,
        %(remitter_bank_code)s
    )
    ON CONFLICT (transaction_ref) DO NOTHING
    RETURNING id
"""

EVENT_INSERT_SQL = """
    INSERT INTO events (
        event_ref, event_type, occurred_at, subject_token, account_token,
        device_token, ip_region, channel, transaction_id, detail, is_replay, bvn_token
    ) VALUES (
        %(event_ref)s, %(event_type)s, %(occurred_at)s, %(subject_token)s, %(account_token)s,
        %(device_token)s, %(ip_region)s, %(channel)s, %(transaction_id)s, %(detail)s, %(is_replay)s,
        %(bvn_token)s
    )
    ON CONFLICT (event_ref) DO NOTHING
    RETURNING id
"""


def _existing_event_type(conn: Any, ref: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT event_type::text AS t FROM events WHERE event_ref = %s", (ref,))
        row = cur.fetchone()
    return None if row is None else (row["t"] if isinstance(row, dict) else row[0])


def _stamp_account_context(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    payload = _stamp_account_context_inner(conn, payload)
    return _no_staler_than_observed(conn, payload)


def _no_staler_than_observed(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Last activity is never older than a payment we have already seen (D22a).

    A caller's account context can be stale: a core-banking snapshot from last
    month, or a simulator that rebuilt its bank as it was on day one. Trusted as
    sent, a stale ``last_activity_at`` makes a busy account look dormant, and
    dormancy is a fraud signal: on 18 Sep a stream stamped 30.9 days of
    inactivity on every account and one payment in eight alerted. So the stamp
    is the later of what the caller says and this account's own most recent
    earlier transaction here. Only earlier ones: the value stays point-in-time,
    whatever order a replay arrives in.
    """
    if not payload.get("account_token"):
        return payload
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(occurred_at) AS seen FROM transactions WHERE account_token = %s AND occurred_at < %s",
            (payload["account_token"], payload["occurred_at"]),
        )
        row = cur.fetchone()
    seen = row["seen"] if isinstance(row, dict) else row[0]
    supplied = payload.get("last_activity_at")
    if seen is not None and (supplied is None or seen > supplied):
        payload["last_activity_at"] = seen
    return payload


def _stamp_account_context_inner(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve missing account context from the dimension, once, here (D22).

    If the caller supplied context we trust it and refresh the dimension from it;
    if not, we read the dimension *at ingestion time* and stamp the result onto
    the transaction. Either way the values are frozen at the moment of the event,
    which is what makes ``account_age_days`` and dormancy point-in-time correct
    without an as-of join anywhere in the feature package.
    """
    token = payload["account_token"]
    supplied = payload.get("account_opened_at") is not None

    if supplied:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (account_token, subject_token, product_type,
                                      origin_sol_id, account_opened_at, last_activity_at,
                                      display_name, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (account_token) DO UPDATE
                   SET last_activity_at = GREATEST(
                           accounts.last_activity_at, EXCLUDED.last_activity_at),
                       display_name = COALESCE(EXCLUDED.display_name, accounts.display_name),
                       updated_at = now()
                """,
                (
                    token,
                    payload["subject_token"],
                    payload.get("product_type") or "SAVINGS",
                    payload.get("origin_sol_id") or "UNKNOWN",
                    payload["account_opened_at"],
                    payload.get("last_activity_at"),
                    payload.get("display_name"),
                ),
            )
        return payload

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT product_type, origin_sol_id, account_opened_at, last_activity_at
              FROM accounts WHERE account_token = %s
            """,
            (token,),
        )
        row = cur.fetchone()
    if row:
        row = dict(row) if not isinstance(row, dict) else row
        payload.update(
            {
                "product_type": payload.get("product_type") or row["product_type"],
                "origin_sol_id": payload.get("origin_sol_id") or row["origin_sol_id"],
                "account_opened_at": row["account_opened_at"],
                "last_activity_at": row["last_activity_at"],
            }
        )
    # An unknown account is left unstamped rather than defaulted. The feature
    # package reports NOT_APPLICABLE (-1) for age and dormancy, which is honest: we do not
    # know when this account opened, and inventing "today" would make every
    # unknown account look brand new and therefore suspicious.
    return payload


def _to_row(tx: TransactionIn, *, is_replay: bool, raise_alerts: bool) -> dict[str, Any]:
    return {
        "transaction_ref": tx.transaction_ref,
        "occurred_at": tx.occurred_at,
        "amount_minor": tx.amount_minor,
        "currency": tx.currency,
        "channel": tx.channel,
        "instrument": tx.instrument,
        "rail": tx.rail,
        # FR-004 happens exactly here and nowhere else.
        "subject_token": subject_token(tx.customer_id),
        "account_token": account_token(tx.account_id),
        "beneficiary_token": (
            beneficiary_token(tx.beneficiary_account_id) if tx.beneficiary_account_id else None
        ),
        "device_token": device_token(tx.device_fingerprint) if tx.device_fingerprint else None,
        "ip_region": tx.ip_region,
        "merchant_category": tx.merchant_category,
        "auth_result": tx.auth_result,
        "decline_reason": tx.decline_reason,
        "display_name": tx.display_name,
        "account_opened_at": tx.account_opened_at,
        "last_activity_at": tx.last_activity_at,
        "product_type": tx.product_type,
        "origin_sol_id": tx.origin_sol_id,
        "is_replay": is_replay,
        "raise_alerts": raise_alerts,
        # D78: the sender of a credit shares the account namespace, so a
        # remitter today and a destination tomorrow are the same token.
        "direction": tx.direction,
        "remitter_token": account_token(tx.remitter_account_id) if tx.remitter_account_id else None,
        "remitter_bank_code": tx.remitter_bank_code,
        # D75: raw identity rides along only as far as _persist, which stamps
        # the BVN token and drops these before anything is written.
        "_customer_id": tx.customer_id,
        "_bvn": tx.bvn,
        "_nin": tx.nin,
    }


def _persist(conn: Any, row: dict[str, Any]) -> TransactionAccepted:
    # One reference names one event. A payment may not reuse a login's.
    existing_type = _existing_event_type(conn, row["transaction_ref"])
    if existing_type not in (None, "PAYMENT", "CREDIT"):
        raise EventRefConflict(f"event_ref already used by a {existing_type} event")
    row = _stamp_account_context(conn, row)
    row["bvn_token"] = identity.stamp(conn, customer_id=row.pop("_customer_id"), subject_token=row["subject_token"],
                                      bvn=row.pop("_bvn"), nin=row.pop("_nin"))
    with conn.cursor() as cur:
        cur.execute(INSERT_SQL, row)
        inserted = cur.fetchone()

        if inserted is None:
            # FR-002: a repeat submission is a no-op returning the original
            # result. Not an error — the caller retrying after a timeout is
            # behaving correctly, and punishing that produces duplicate money.
            cur.execute(
                "SELECT id FROM transactions WHERE transaction_ref = %s",
                (row["transaction_ref"],),
            )
            existing = cur.fetchone()
            tx_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
            return TransactionAccepted(
                transaction_ref=row["transaction_ref"],
                transaction_id=tx_id,
                status="duplicate",
                queued=False,
            )

        tx_id = int(inserted["id"] if isinstance(inserted, dict) else inserted[0])
        # FR-005: same transaction. The queue row and the transaction row commit
        # together or not at all.
        # D86: replayed history queues behind live traffic (priority 1).
        cur.execute("INSERT INTO scoring_queue (transaction_id, priority) VALUES (%s, %s)",
                    (tx_id, 1 if row["is_replay"] else 0))
        # D72: and its envelope, in the same transaction, so every payment is
        # also an event from the moment it exists.
        cur.execute(EVENT_INSERT_SQL, {
            "event_ref": row["transaction_ref"],
            "event_type": "CREDIT" if row["direction"] == "INBOUND" else "PAYMENT",
            "occurred_at": row["occurred_at"],
            "subject_token": row["subject_token"],
            "account_token": row["account_token"],
            "device_token": row["device_token"],
            "ip_region": row["ip_region"],
            "channel": row["channel"],
            "transaction_id": tx_id,
            "detail": "{}",
            "is_replay": row["is_replay"],
            "bvn_token": row["bvn_token"],
        })

    return TransactionAccepted(
        transaction_ref=row["transaction_ref"],
        transaction_id=tx_id,
        status="accepted",
        queued=True,
    )


@router.post("", response_model=TransactionAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_one(
    tx: TransactionIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> TransactionAccepted:
    flow.guard(conn, response, api_key, cost=1, replay=False)
    try:
        result = _persist(conn, _to_row(tx, is_replay=False, raise_alerts=True))
    except EventRefConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if result.status == "duplicate":
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/batch", response_model=BatchAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_batch(
    body: BatchIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> BatchAccepted:
    """FR-006. A naive loop, deliberately (D16: batch/replay is contract-first).

    The contract matters more than throughput here: what a caller must be able to
    rely on is that replay is idempotent and silent, and that is true of a loop.
    """
    flow.guard(conn, response, api_key, cost=len(body.transactions), replay=body.is_replay)
    results: list[TransactionAccepted] = []
    errors: list[dict] = []
    # D87: each item commits on its own. A batch was never all-or-nothing (the
    # answer lists each item accepted or rejected), and holding every item's
    # account-row lock until the whole batch committed made concurrent batches
    # for overlapping customers queue behind each other: with three API
    # processes one 200-item batch took over a minute. The request's own work
    # (key check, flow guard) commits first.
    conn.commit()
    for index, tx in enumerate(body.transactions):
        try:
            with conn.transaction():
                results.append(
                    _persist(conn, _to_row(tx, is_replay=body.is_replay, raise_alerts=body.raise_alerts))
                )
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": index, "transaction_ref": tx.transaction_ref, "error": str(exc)})

    return BatchAccepted(
        accepted=sum(1 for r in results if r.status == "accepted"),
        duplicates=sum(1 for r in results if r.status == "duplicate"),
        rejected=len(errors),
        results=results,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Events (WP-01, D72)
# ---------------------------------------------------------------------------


def _detail(ev: Any) -> dict[str, Any]:
    """The type-specific facts, tokenised exactly as a payment's identifiers are."""
    detail = ev.detail.model_dump()
    if ev.event_type == "PAYEE_ADDED":
        detail["beneficiary_token"] = beneficiary_token(detail.pop("beneficiary_account_id"))
    elif ev.event_type == "SIM_CHANGED":
        detail["msisdn_token"] = msisdn_token(detail.pop("msisdn"))
    return detail


def _persist_event(conn: Any, ev: Any, *, is_replay: bool, raise_alerts: bool) -> EventAccepted:
    if isinstance(ev, PaymentEventIn):
        # The payment path, unchanged: tokenise, stamp, persist, enqueue, envelope.
        tx = _persist(conn, _to_row(ev.payment, is_replay=is_replay, raise_alerts=raise_alerts))
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM events WHERE transaction_id = %s", (tx.transaction_id,))
            row = cur.fetchone()
        return EventAccepted(
            event_ref=tx.transaction_ref, event_type="CREDIT" if ev.payment.direction == "INBOUND" else "PAYMENT",
            event_id=int(row["id"] if isinstance(row, dict) else row[0]),
            status=tx.status, queued=tx.queued, transaction_id=tx.transaction_id,
        )

    subject = subject_token(ev.customer_id)
    bvn = identity.stamp(conn, customer_id=ev.customer_id, subject_token=subject, bvn=ev.bvn, nin=ev.nin,
                         event_type=ev.event_type)
    with conn.cursor() as cur:
        cur.execute(EVENT_INSERT_SQL, {
            "event_ref": ev.event_ref,
            "event_type": ev.event_type,
            "occurred_at": ev.occurred_at,
            # FR-004 for every type: raw identifiers stop here.
            "subject_token": subject,
            "bvn_token": bvn,
            "account_token": account_token(ev.account_id) if ev.account_id else None,
            "device_token": device_token(ev.device_fingerprint) if ev.device_fingerprint else None,
            "ip_region": ev.ip_region,
            "channel": ev.channel,
            "transaction_id": None,
            "detail": json.dumps(_detail(ev)),
            "is_replay": is_replay,
        })
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute("SELECT id, event_type::text AS t FROM events WHERE event_ref = %s", (ev.event_ref,))
            existing = cur.fetchone()
            if existing["t"] != ev.event_type:
                raise EventRefConflict(f"event_ref already used by a {existing['t']} event")
            return EventAccepted(event_ref=ev.event_ref, event_type=ev.event_type,
                                 event_id=int(existing["id"]), status="duplicate", queued=False)
    return EventAccepted(event_ref=ev.event_ref, event_type=ev.event_type,
                         event_id=int(inserted["id"]), status="accepted", queued=False)


@events_router.post("", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_event(
    event: EventIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> EventAccepted:
    """One event of any type. Payments are scored; the other types are stored
    for the detectors WP-02 adds, and nothing is scored on them yet."""
    flow.guard(conn, response, api_key, cost=1, replay=False)
    try:
        result = _persist_event(conn, event, is_replay=False, raise_alerts=True)
    except EventRefConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if result.status == "duplicate":
        response.status_code = status.HTTP_200_OK
    return result


@events_router.post("/batch", response_model=EventBatchAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_event_batch(
    body: EventBatchIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> EventBatchAccepted:
    """FR-006 for every type: replay is idempotent and silent by default (D8d)."""
    flow.guard(conn, response, api_key, cost=len(body.events), replay=body.is_replay)
    results: list[EventAccepted] = []
    errors: list[dict] = []
    conn.commit()  # D87: per-item commits, as for payments
    for index, ev in enumerate(body.events):
        ref = ev.payment.transaction_ref if isinstance(ev, PaymentEventIn) else ev.event_ref
        try:
            with conn.transaction():
                results.append(_persist_event(conn, ev, is_replay=body.is_replay, raise_alerts=body.raise_alerts))
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": index, "event_ref": ref, "error": str(exc)})
    return EventBatchAccepted(
        accepted=sum(1 for r in results if r.status == "accepted"),
        duplicates=sum(1 for r in results if r.status == "duplicate"),
        rejected=len(errors),
        results=results,
        errors=errors,
    )


def write_dead_letter(conn: Any, raw_body: bytes, error: str, api_key_id: int | None) -> None:
    """FR-003: never silently coerced.

    A malformed payload keeps its raw body and its validation error, so the
    caller can be shown exactly what they sent and exactly why it stopped.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dead_letter (api_key_id, raw_body, validation_error) VALUES (%s, %s, %s)",
            (api_key_id, raw_body.decode("utf-8", errors="replace")[:100_000], error[:4000]),
        )

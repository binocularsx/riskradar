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

from fastapi import APIRouter, Depends, Request, Response, status

from ...security.tokens import account_token, beneficiary_token, device_token, subject_token
from ..deps import get_conn, require_api_key
from ..schemas import BatchAccepted, BatchIn, TransactionAccepted, TransactionIn

router = APIRouter(prefix="/v1/transactions", tags=["ingestion"])


INSERT_SQL = """
    INSERT INTO transactions (
        transaction_ref, occurred_at, amount_minor, currency,
        channel, instrument, rail,
        subject_token, account_token, beneficiary_token,
        device_token, ip_region, merchant_category,
        auth_result, decline_reason, display_name,
        account_opened_at, last_activity_at, product_type, origin_sol_id,
        is_replay, raise_alerts
    ) VALUES (
        %(transaction_ref)s, %(occurred_at)s, %(amount_minor)s, %(currency)s,
        %(channel)s, %(instrument)s, %(rail)s,
        %(subject_token)s, %(account_token)s, %(beneficiary_token)s,
        %(device_token)s, %(ip_region)s, %(merchant_category)s,
        %(auth_result)s, %(decline_reason)s, %(display_name)s,
        %(account_opened_at)s, %(last_activity_at)s, %(product_type)s, %(origin_sol_id)s,
        %(is_replay)s, %(raise_alerts)s
    )
    ON CONFLICT (transaction_ref) DO NOTHING
    RETURNING id
"""


def _stamp_account_context(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
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
    # package reports NEVER_SEEN for age and dormancy, which is honest: we do not
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
    }


def _persist(conn: Any, row: dict[str, Any]) -> TransactionAccepted:
    row = _stamp_account_context(conn, row)
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
        cur.execute("INSERT INTO scoring_queue (transaction_id) VALUES (%s)", (tx_id,))

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
    result = _persist(conn, _to_row(tx, is_replay=False, raise_alerts=True))
    if result.status == "duplicate":
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/batch", response_model=BatchAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_batch(
    body: BatchIn,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> BatchAccepted:
    """FR-006. A naive loop, deliberately (D16: batch/replay is contract-first).

    The contract matters more than throughput here: what a caller must be able to
    rely on is that replay is idempotent and silent, and that is true of a loop.
    """
    results: list[TransactionAccepted] = []
    errors: list[dict] = []
    for index, tx in enumerate(body.transactions):
        try:
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

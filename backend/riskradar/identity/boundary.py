"""Stamping identity at the ingestion boundary (D75, REG-NG-03).

The boundary is the only place a raw customer number, BVN or NIN exists (D9c).
So this is where a BVN is learned, tokenised and remembered:

1. The payload carried a BVN: use it.
2. Otherwise, this customer's BVN is already known: use the stored token. This
   is the common case, and costs one indexed read, not a call to the core.
3. Otherwise ask the core by customer number. Unconnected core: no BVN, and the
   payment carries on exactly as before.

A BVN seen for the first time, or seen on a high-risk event (a changed PIN, a
SIM swap, a newly bound device), is checked with the identity registry: the
base PRD's "at onboarding and on high-risk events", with first sighting standing
in for onboarding because Risk Radar receives no onboarding events.

Nothing here can fail a payment. An adapter error is logged and the payment is
stored without a BVN.
"""

from __future__ import annotations

import logging
from typing import Any

from ..security.tokens import bvn_token as make_bvn_token
from ..security.tokens import nin_token as make_nin_token
from . import adapters

log = logging.getLogger("riskradar.identity")


def stamp(conn: Any, *, customer_id: str, subject_token: str, bvn: str | None, nin: str | None,
          event_type: str = "PAYMENT") -> str | None:
    """Return the customer's BVN token, learning it if needed. Never raises."""
    try:
        # A savepoint, so a failed identity write cannot abort the payment's transaction.
        with conn.transaction():
            return _stamp(conn, customer_id=customer_id, subject_token=subject_token, bvn=bvn, nin=nin,
                          event_type=event_type)
    except Exception:  # noqa: BLE001 - identity must never stop a payment
        log.exception("identity stamping failed; continuing without a BVN")
        return None


def _stamp(conn: Any, *, customer_id: str, subject_token: str, bvn: str | None, nin: str | None,
           event_type: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT bvn_token FROM customer_identities WHERE subject_token = %s", (subject_token,))
        row = cur.fetchone()
    known = (row["bvn_token"] if isinstance(row, dict) else row[0]) if row else None

    source = "BOUNDARY"
    if bvn is None and known is None:
        try:
            found = adapters.core_resolver().resolve(customer_id)
        except adapters.NotConnected as exc:
            log.debug("core resolver: %s", exc)
            found = None
        if found is None:
            return None
        bvn, nin, source = found.bvn, found.nin, "CORE_LOOKUP"

    if bvn is None:
        # Known already, nothing new arrived. Touch last_seen only on a
        # high-risk event, so ordinary payments stay one read.
        if event_type in adapters.HIGH_RISK_EVENT_TYPES:
            with conn.cursor() as cur:
                cur.execute("UPDATE customer_identities SET last_seen_at = now() WHERE subject_token = %s",
                            (subject_token,))
        return known

    token = make_bvn_token(bvn)
    first_sighting = known != token
    verify = first_sighting or event_type in adapters.HIGH_RISK_EVENT_TYPES
    check = adapters.identity_registry().verify(bvn) if verify else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO customer_identities (subject_token, bvn_token, nin_token, source,
                                             verification_status, verified_at, verification_source)
            VALUES (%(subject)s, %(bvn)s, %(nin)s, %(source)s,
                    COALESCE(%(status)s::text, 'UNVERIFIED'),
                    CASE WHEN %(status)s::text IS NULL THEN NULL ELSE now() END, %(vsource)s::text)
            ON CONFLICT (subject_token) DO UPDATE SET
                bvn_token = EXCLUDED.bvn_token,
                nin_token = COALESCE(EXCLUDED.nin_token, customer_identities.nin_token),
                last_seen_at = now(),
                verification_status = COALESCE(%(status)s::text, customer_identities.verification_status),
                verified_at = CASE WHEN %(status)s::text IS NULL THEN customer_identities.verified_at ELSE now() END,
                verification_source = COALESCE(%(vsource)s::text, customer_identities.verification_source)
            """,
            {"subject": subject_token, "bvn": token, "nin": make_nin_token(nin) if nin else None,
             "source": source, "status": check.status if check else None,
             "vsource": check.source if check else None},
        )
    if known and known != token:
        # A customer record whose BVN changed is worth a human look, not a
        # silent overwrite; it is logged, and the new token applies from now.
        log.warning("customer record %s changed BVN token", subject_token[:12])
    return token

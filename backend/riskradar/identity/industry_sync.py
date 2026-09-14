"""Moving flags to and from the industry watch-list (D75, REG-NG-04, FR-702).

Outbound is a transactional outbox: a flag's place, lift and expiry are written
to ``watchlist_outbox`` in the same transaction as the flag itself, so a flag
can never exist without its message, and a crash can never send a message for
a flag that rolled back. This dispatcher then hands due messages to the
connector:

* sent: marked SENT with the hub's reference;
* connector not configured: stays PENDING, the reason recorded, retried in five
  minutes. This is "pending connection", visibly, not silently;
* any other failure: retried with backoff, FAILED after ten attempts;
* no BVN known: NOT_SHAREABLE from the start (the hub only understands BVNs).

Inbound, flags other institutions placed are stored by BVN token. They arrive
either from the connector's ``pull`` or through the inbound API a bank-side
connector posts to. The raw BVN is tokenised on arrival and never stored.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..security.tokens import bvn_token
from . import adapters

log = logging.getLogger("riskradar.identity")

MAX_ATTEMPTS = 10
NOT_CONNECTED_RETRY = timedelta(minutes=5)


def _fetch(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def dispatch(conn: Any, *, connector: adapters.IndustryConnector | None = None,
             now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    connector = connector or adapters.industry_connector()
    now = now or datetime.now(timezone.utc)
    due = _fetch(
        conn,
        "SELECT id, payload, attempts FROM watchlist_outbox WHERE status = 'PENDING' AND next_attempt_at <= %s "
        "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
        (now, limit),
    )
    counts = {"sent": 0, "waiting": 0, "failed": 0}
    for row in due:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        attempts = row["attempts"] + 1
        try:
            ref = connector.publish(payload)
        except adapters.NotConnected as exc:
            _update(conn, row["id"], status="PENDING", attempts=attempts, error=str(exc),
                    next_at=now + NOT_CONNECTED_RETRY)
            counts["waiting"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - a hub outage must not stop the sweep
            final = attempts >= MAX_ATTEMPTS
            _update(conn, row["id"], status="FAILED" if final else "PENDING", attempts=attempts,
                    error=f"{type(exc).__name__}: {exc}", next_at=now + timedelta(seconds=30 * 2 ** min(attempts, 8)))
            counts["failed" if final else "waiting"] += 1
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE watchlist_outbox SET status = 'SENT', attempts = %s, sent_at = %s, external_ref = %s, "
                "last_error = NULL WHERE id = %s",
                (attempts, now, ref, row["id"]),
            )
        counts["sent"] += 1
    return counts


def _update(conn: Any, outbox_id: int, *, status: str, attempts: int, error: str, next_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE watchlist_outbox SET status = %s, attempts = %s, last_error = %s, next_attempt_at = %s "
            "WHERE id = %s",
            (status, attempts, error[:500], next_at, outbox_id),
        )


def receive(conn: Any, entries: list[dict[str, Any]], *, source: str) -> dict[str, int]:
    """Store flags from other institutions. Idempotent on ``external_ref``.

    Entries carry a raw BVN; it is tokenised here and dropped. Our own flags
    echoed back by the hub are ignored: they are already on the case.
    """
    ours = adapters.institution_code()
    counts = {"stored": 0, "updated": 0, "ignored_own": 0}
    for e in entries:
        if e["institution_code"] == ours:
            counts["ignored_own"] += 1
            continue
        rows = _fetch(
            conn,
            """
            INSERT INTO industry_watchlist (external_ref, bvn_token, institution_code, reason_code,
                                            flagged_at, expires_at, lifted_at, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (external_ref) DO UPDATE
               SET lifted_at = COALESCE(industry_watchlist.lifted_at, EXCLUDED.lifted_at)
            RETURNING (xmax = 0) AS inserted
            """,
            (e["external_ref"], bvn_token(e["bvn"]), e["institution_code"], e["reason_code"],
             e["flagged_at"], e["expires_at"], e.get("lifted_at"), source),
        )
        counts["stored" if rows[0]["inserted"] else "updated"] += 1
    return counts


def pull(conn: Any, *, connector: adapters.IndustryConnector | None = None) -> dict[str, int]:
    connector = connector or adapters.industry_connector()
    try:
        entries = connector.pull()
    except adapters.NotConnected as exc:
        log.debug("industry pull: %s", exc)
        return {"stored": 0, "updated": 0, "ignored_own": 0}
    return receive(conn, entries, source="CONNECTOR_PULL")


def status(conn: Any) -> dict[str, Any]:
    """What an operator needs to see: which adapters are in use, and what is waiting."""
    outbox = _fetch(conn, "SELECT status, count(*) AS n, max(last_error) FILTER (WHERE last_error IS NOT NULL) "
                          "AS last_error, min(created_at) AS oldest FROM watchlist_outbox GROUP BY status ORDER BY status")
    coverage = _fetch(conn, """
        SELECT count(DISTINCT t.subject_token) AS customers,
               count(DISTINCT t.subject_token) FILTER (WHERE ci.bvn_token IS NOT NULL) AS with_bvn
          FROM transactions t LEFT JOIN customer_identities ci ON ci.subject_token = t.subject_token
         WHERE t.occurred_at > now() - interval '30 days'
    """)[0]
    verification = _fetch(conn, "SELECT verification_status AS status, count(*) AS n FROM customer_identities "
                                "GROUP BY 1 ORDER BY 1")
    inbound = _fetch(conn, "SELECT count(*) FILTER (WHERE lifted_at IS NULL AND expires_at > now()) AS active, "
                           "count(*) AS total, max(received_at) AS last_received FROM industry_watchlist")[0]
    return {
        "adapters": {
            "core_resolver": adapters.core_resolver().name,
            "identity_registry": adapters.identity_registry().name,
            "industry_connector": adapters.industry_connector().name,
            "institution_code": adapters.institution_code(),
        },
        "outbox": outbox,
        "bvn_coverage_30d": coverage,
        "verification": verification,
        "inbound": inbound,
    }

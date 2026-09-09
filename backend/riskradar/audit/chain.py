"""Hash-chained audit log (D12c).

Every row stores ``prev_hash`` and ``SHA256(prev_hash || canonical_payload)``.
Tampering with any historical row breaks every hash after it, and the break is
found by a query rather than by trusting that nobody edited the table.

The database-level control is the one that matters — the application role holds
``INSERT`` and ``SELECT`` on ``audit_log`` and nothing else (see 0002_grants.sql),
so the process writing this chain cannot rewrite it. This module supplies the
evidence; the grant supplies the guarantee.

**Actor is never null.** "Analyst cleared this case" means nothing if anyone can
assert they are any analyst, and non-repudiation is the entire value of the
feature. System-generated actions use the reserved SYSTEM principal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

GENESIS = "GENESIS"

# Constant key for the transaction-scoped advisory lock that serialises appends.
#
# The obvious approach — SELECT ... FOR UPDATE on the tail row — is unavailable
# to us *because* the control works: FOR UPDATE requires UPDATE privilege, and
# the application role deliberately has none. An advisory lock needs no table
# privilege at all, so the chain stays serialised without weakening the grant.
_LOCK_KEY = 0x21B3_C0DE


def canonical_payload(
    occurred_at: datetime,
    actor_user_id: int,
    action: str,
    object_type: str,
    object_id: str | None,
    from_state: str | None,
    to_state: str | None,
    payload: dict[str, Any],
) -> str:
    """Stable serialisation.

    Sorted keys and no incidental whitespace: the hash must not depend on dict
    ordering or on a Python version's JSON formatting, or verification breaks on
    an upgrade and looks like tampering.
    """
    return json.dumps(
        {
            "occurred_at": occurred_at.isoformat(),
            "actor_user_id": actor_user_id,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "from_state": from_state,
            "to_state": to_state,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _chain_hash(prev_hash: str, body: str) -> str:
    return hashlib.sha256(f"{prev_hash}{body}".encode("utf-8")).hexdigest()


def append(
    conn: Any,
    *,
    actor_user_id: int,
    action: str,
    object_type: str,
    object_id: str | int | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one row. Must run inside the caller's transaction.

    Sharing the caller's transaction is deliberate: an audit row that commits
    when the action it describes rolled back is worse than no audit row, because
    it is confidently wrong.
    """
    payload = payload or {}
    object_id_str = None if object_id is None else str(object_id)

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        cur.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = (row["hash"] if isinstance(row, dict) else row[0]) if row else GENESIS

        cur.execute("SELECT now() AS ts")
        ts_row = cur.fetchone()
        occurred_at = ts_row["ts"] if isinstance(ts_row, dict) else ts_row[0]

        body = canonical_payload(
            occurred_at,
            actor_user_id,
            action,
            object_type,
            object_id_str,
            from_state,
            to_state,
            payload,
        )
        digest = _chain_hash(prev_hash, body)

        cur.execute(
            """
            INSERT INTO audit_log
                (occurred_at, actor_user_id, action, object_type, object_id,
                 from_state, to_state, payload, prev_hash, hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, occurred_at, hash
            """,
            (
                occurred_at,
                actor_user_id,
                action,
                object_type,
                object_id_str,
                from_state,
                to_state,
                json.dumps(payload, default=str),
                prev_hash,
                digest,
            ),
        )
        result = cur.fetchone()

    return dict(result) if not isinstance(result, dict) else result


def verify(conn: Any, limit: int | None = None) -> dict[str, Any]:
    """Walk the chain and report the first break.

    Returns ``{"ok": bool, "checked": int, "broken_at_id": int | None, "reason": str}``.
    Exposed through the admin API so tamper-evidence is demonstrable rather than
    merely claimed.
    """
    sql = """
        SELECT id, occurred_at, actor_user_id, action, object_type, object_id,
               from_state, to_state, payload, prev_hash, hash
          FROM audit_log
         ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    prev = GENESIS
    checked = 0
    with conn.cursor() as cur:
        cur.execute(sql)
        for row in cur:
            r = dict(row) if not isinstance(row, dict) else row
            if r["prev_hash"] != prev:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at_id": r["id"],
                    "reason": "prev_hash does not match the preceding row's hash",
                }
            body = canonical_payload(
                r["occurred_at"],
                r["actor_user_id"],
                r["action"],
                r["object_type"],
                r["object_id"],
                r["from_state"],
                r["to_state"],
                r["payload"],
            )
            if _chain_hash(r["prev_hash"], body) != r["hash"]:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at_id": r["id"],
                    "reason": "row content does not match its stored hash",
                }
            prev = r["hash"]
            checked += 1

    return {"ok": True, "checked": checked, "broken_at_id": None, "reason": ""}

"""Server-side sessions (D12).

Stored in Postgres, delivered as an ``httpOnly`` + ``Secure`` + ``SameSite=Lax``
cookie. **Revocation is a DELETE.**

Explicitly not a JWT in ``localStorage``: readable by any XSS, and unrevocable
before expiry. For a console displaying financial data and taking case decisions,
"we cannot log this session out until it expires" is not an acceptable sentence.

Two expiries, because they answer different questions (D27):

* ``idle_expires_at``     — 12h. An abandoned desk should not stay authenticated.
* ``absolute_expires_at`` — 24h. A stolen cookie has a hard ceiling regardless of
  how attentively the thief keeps it warm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from .passwords import new_session_token
from .tokens import hash_session_id


def create(
    conn: Any,
    user_id: int,
    *,
    mfa_satisfied: bool,
    user_agent: str | None = None,
    ip_region: str | None = None,
) -> str:
    """Create a session and return the **raw** token for the cookie.

    Only the hash is persisted, so a database read cannot be replayed as a login.
    """
    s = settings()
    raw = new_session_token()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions
                (id, user_id, idle_expires_at, absolute_expires_at,
                 mfa_satisfied, user_agent, ip_region)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                hash_session_id(raw),
                user_id,
                now + timedelta(hours=s.session_idle_hours),
                now + timedelta(hours=s.session_absolute_hours),
                mfa_satisfied,
                (user_agent or "")[:400] or None,
                ip_region,
            ),
        )
    return raw


def resolve(conn: Any, raw_token: str) -> dict[str, Any] | None:
    """Return the session's user, or None. Slides the idle window on success."""
    s = settings()
    session_id = hash_session_id(raw_token)
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.user_id, s.mfa_satisfied, s.idle_expires_at,
                   s.absolute_expires_at,
                   u.email, u.display_name, u.role, u.active, u.is_system
              FROM sessions s
              JOIN users u ON u.id = s.user_id
             WHERE s.id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        row = dict(row) if not isinstance(row, dict) else row

        if row["idle_expires_at"] <= now or row["absolute_expires_at"] <= now:
            cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            return None
        if not row["active"] or row["is_system"]:
            cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            return None

        cur.execute(
            """
            UPDATE sessions
               SET last_seen_at = %s,
                   idle_expires_at = LEAST(%s, absolute_expires_at)
             WHERE id = %s
            """,
            (now, now + timedelta(hours=s.session_idle_hours), session_id),
        )
    return row


def revoke(conn: Any, raw_token: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE id = %s", (hash_session_id(raw_token),))


def revoke_all_for_user(conn: Any, user_id: int) -> int:
    """Used when a user is deactivated or a role changes.

    A role change with live sessions would otherwise leave the old permission set
    in play until expiry, which is the exact failure JWTs have and this design
    was chosen to avoid.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        return cur.rowcount

"""Server-side sessions (D12), with rotation and CSRF (D95).

Stored in Postgres, delivered as an ``httpOnly`` + ``Secure`` + ``SameSite=Lax``
cookie. **Revocation is a DELETE.**

Explicitly not a JWT in ``localStorage``: readable by any XSS, and unrevocable
before expiry. For a console displaying financial data and taking case decisions,
"we cannot log this session out until it expires" is not an acceptable sentence.

Three expiries/limits, because they answer different questions (D27, D95):

* ``idle_expires_at``     — 12h. An abandoned desk should not stay authenticated.
* ``absolute_expires_at`` — 24h. A stolen cookie has a hard ceiling regardless of
  how attentively the thief keeps it warm.
* rotation (``rotated_at``) — every 15 min the identifier is replaced, so a cookie
  captured at rest is replayable for the rotation interval, not the whole 24h.

Rotation replaces the random cookie *value* (and therefore its stored hash, the
primary key) while the session itself lives on. The just-superseded identifier
stays valid for a short grace window, so the several requests a dashboard fires
at once do not race one another into a spurious logout.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from .passwords import new_csrf_token, new_session_token
from .tokens import hash_session_id


def create(
    conn: Any,
    user_id: int,
    *,
    mfa_satisfied: bool,
    user_agent: str | None = None,
    ip_region: str | None = None,
) -> tuple[str, str]:
    """Create a session and return ``(raw_session_token, csrf_token)``.

    Only the session hash is persisted, so a database read cannot be replayed as
    a login. The CSRF token is stored in plaintext — it is not a credential (see
    the module docstring).
    """
    s = settings()
    raw = new_session_token()
    csrf = new_csrf_token()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions
                (id, user_id, idle_expires_at, absolute_expires_at,
                 mfa_satisfied, user_agent, ip_region, csrf_token, rotated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                hash_session_id(raw),
                user_id,
                now + timedelta(hours=s.session_idle_hours),
                now + timedelta(hours=s.session_absolute_hours),
                mfa_satisfied,
                (user_agent or "")[:400] or None,
                ip_region,
                csrf,
                now,
            ),
        )
    return raw, csrf


def resolve(conn: Any, raw_token: str, *, rotate: bool = True) -> dict[str, Any] | None:
    """Return the session's user, or None. Slides the idle window on success.

    When ``rotate`` is true and the identifier is older than the rotation
    interval, the identifier is replaced: the returned dict then carries
    ``rotated_token`` (the new raw cookie value) for the caller to re-set. A
    session that predates D95 (no CSRF token) has one minted here on first sight.
    ``rotate`` is false on the streaming path, which cannot re-set a cookie mid
    stream and whose GETs never mutate anything anyway.
    """
    s = settings()
    session_id = hash_session_id(raw_token)
    now = datetime.now(timezone.utc)
    grace = timedelta(seconds=s.session_rotate_grace_seconds)

    with conn.cursor() as cur:
        # Match the current identifier, or the just-superseded one within its
        # grace window — a request still carrying the old cookie resolves.
        cur.execute(
            """
            SELECT s.id, s.user_id, s.mfa_satisfied, s.idle_expires_at,
                   s.absolute_expires_at, s.csrf_token, s.rotated_at,
                   (s.id = %(sid)s) AS matched_current,
                   u.email, u.display_name, u.role, u.active, u.is_system,
                   u.totp_enabled
              FROM sessions s
              JOIN users u ON u.id = s.user_id
             WHERE s.id = %(sid)s
                OR (s.previous_id = %(sid)s AND s.rotated_at > %(grace_floor)s)
            """,
            {"sid": session_id, "grace_floor": now - grace},
        )
        row = cur.fetchone()
        if not row:
            return None
        row = dict(row) if not isinstance(row, dict) else dict(row)
        current_id = row["id"]

        if row["idle_expires_at"] <= now or row["absolute_expires_at"] <= now:
            cur.execute("DELETE FROM sessions WHERE id = %s", (current_id,))
            return None
        if not row["active"] or row["is_system"]:
            cur.execute("DELETE FROM sessions WHERE id = %s", (current_id,))
            return None

        row["rotated_token"] = None

        # Backfill a CSRF token for any session created before D95.
        if not row["csrf_token"]:
            row["csrf_token"] = new_csrf_token()
            cur.execute(
                "UPDATE sessions SET csrf_token = %s WHERE id = %s",
                (row["csrf_token"], current_id),
            )

        due = row["rotated_at"] <= now - timedelta(minutes=s.session_rotate_minutes)
        if rotate and row["matched_current"] and due:
            new_raw = new_session_token()
            # Optimistic swap: only the request that still sees the old id as
            # current performs the rotation; a concurrent one updates zero rows
            # and simply proceeds on the grace window.
            cur.execute(
                """
                UPDATE sessions
                   SET previous_id = id,
                       id = %s,
                       rotated_at = %s,
                       last_seen_at = %s,
                       idle_expires_at = LEAST(%s, absolute_expires_at)
                 WHERE id = %s AND rotated_at = %s
                """,
                (
                    hash_session_id(new_raw),
                    now,
                    now,
                    now + timedelta(hours=s.session_idle_hours),
                    current_id,
                    row["rotated_at"],
                ),
            )
            if cur.rowcount == 1:
                row["rotated_token"] = new_raw
        else:
            cur.execute(
                """
                UPDATE sessions
                   SET last_seen_at = %s,
                       idle_expires_at = LEAST(%s, absolute_expires_at)
                 WHERE id = %s
                """,
                (now, now + timedelta(hours=s.session_idle_hours), current_id),
            )
    return row


def verify_csrf(session: dict[str, Any], presented: str | None) -> bool:
    """Constant-time double-submit check. The header must equal the session's
    CSRF token (D95). Empty or missing fails closed."""
    expected = session.get("csrf_token")
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(expected), str(presented))


def revoke(conn: Any, raw_token: str) -> None:
    session_id = hash_session_id(raw_token)
    with conn.cursor() as cur:
        # Cover the grace window too: a logout with a just-rotated cookie still
        # ends the right session.
        cur.execute(
            "DELETE FROM sessions WHERE id = %s OR previous_id = %s",
            (session_id, session_id),
        )


def revoke_all_for_user(conn: Any, user_id: int) -> int:
    """Used when a user is deactivated or a role changes.

    A role change with live sessions would otherwise leave the old permission set
    in play until expiry, which is the exact failure JWTs have and this design
    was chosen to avoid.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        return cur.rowcount

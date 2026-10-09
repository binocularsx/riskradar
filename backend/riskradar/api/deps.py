"""Request-scoped dependencies: database handles, identity, permissions."""

from __future__ import annotations

from typing import Any, Iterator

from fastapi import Depends, HTTPException, Request, Response, status

from ..config import settings
from ..db import pool
from ..security import cookies, sessions
from ..security.rbac import Permission, permissions_for
from ..security.tokens import hash_api_key

# Requests that change state carry a CSRF token (D95). The safe methods do not.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _enforce_csrf(request: Request, session: dict[str, Any]) -> None:
    """D95: double-submit check on every unsafe, cookie-authenticated request.

    The session cookie proves who you are; the header proves the request came
    from our own page, which is the one thing a forged cross-site request cannot
    supply. Machine callers use ``X-API-Key`` and never travel through here.
    """
    if request.method not in _UNSAFE_METHODS:
        return
    presented = request.headers.get(settings().csrf_header)
    if not sessions.verify_csrf(session, presented):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing or invalid CSRF token",
        )


def get_conn() -> Iterator[Any]:
    """One pooled connection per request, in a transaction.

    Committed by the pool on clean exit, rolled back on exception — so a handler
    that raises cannot leave a half-written case behind.

    Note the timing: FastAPI runs this teardown *after* the response has been
    handed to the client, so the commit lands slightly after the client can see
    the answer. Harmless for a handler whose answer merely describes what it
    wrote; not harmless for one whose answer is a credential the client uses
    immediately. Such a handler calls `conn.commit()` itself as its last act —
    see `routers/auth.py:login`.
    """
    with pool().connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Machine authentication — ingestion (FR-001)
# ---------------------------------------------------------------------------


_SCOPE_NAME = {"BANK": "the bank's systems", "SUPPORT": "the support team"}


def _api_key(request: Request, conn: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    raw = request.headers.get("x-api-key")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, scope FROM api_keys WHERE key_hash = %s AND active",
            (hash_api_key(raw),),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key"
        )
    row = dict(row) if not isinstance(row, dict) else row
    if row["scope"] not in allowed:
        # D109g: a valid key, for somebody else's door. 403, not 401: the key
        # is genuine, it is the endpoint that is not theirs.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this API key belongs to {_SCOPE_NAME[row['scope']]} and cannot call this endpoint",
        )
    with conn.cursor() as cur:
        cur.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row["id"],))
    return row


def require_api_key(request: Request, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """A bank key: ingestion, events, directives, the industry watch-list."""
    return _api_key(request, conn, ("BANK",))


def require_support_key(request: Request, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """D109g: the support team's key, for the support team's endpoints only."""
    return _api_key(request, conn, ("SUPPORT",))


def require_action_feed_key(request: Request, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """The actions a lead approved (D97). Support carries them out (D109b); a
    bank core connector may also read them, so both scopes are accepted."""
    return _api_key(request, conn, ("SUPPORT", "BANK"))


# ---------------------------------------------------------------------------
# Human authentication — the console
# ---------------------------------------------------------------------------


def current_user(
    request: Request,
    response: Response,
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    # D112: the cookie carries every session this browser holds for the host;
    # the tab names the one it is using. No name, no session — which is how a
    # newly opened tab reaches the login page instead of someone else's desk.
    tokens = cookies.read_session_tokens(request)
    raw = sessions.token_for_tab(conn, tokens, request.headers.get(settings().tab_header))
    if not raw:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = sessions.resolve(conn, raw)
    if not session:
        raise HTTPException(status_code=401, detail="session expired or revoked")

    role = session["role"]
    # D12a/D95/D105: a session that has not satisfied MFA is not a session for a
    # user who is required to hold one. The switch is now the user's own
    # `totp_enabled`, which an administrator can change under maker-checker, and
    # it is checked on every request rather than only at login — so turning the
    # requirement *on* takes effect immediately for anyone already signed in.
    if session["totp_enabled"] and not session["mfa_satisfied"]:
        raise HTTPException(status_code=401, detail="multi-factor authentication required")

    # D95: CSRF on unsafe methods, and re-set the cookie if the identifier just
    # rotated, so the browser carries the new value on its next request.
    _enforce_csrf(request, session)
    if session.get("rotated_token"):
        # Replace only this tab's identifier; the others in the cookie belong to
        # other tabs and must survive (D112).
        rotated = [session["rotated_token"] if t == raw else t for t in tokens]
        cookies.set_session_cookie(response, rotated)

    return {
        "id": session["user_id"],
        "email": session["email"],
        "display_name": session["display_name"],
        "role": role,
        "mfa_satisfied": session["mfa_satisfied"],
        "permissions": sorted(str(p) for p in permissions_for(role)),
    }


def current_user_short(request: Request) -> dict[str, Any]:
    """Identify the caller **without** holding a pooled connection.

    ``get_conn`` yields its connection for the whole lifetime of the response,
    which is correct for ordinary request/response routes and catastrophic for a
    streaming one: an open Server-Sent Events stream would pin a pooled
    connection until the browser disconnected. A handful of dashboard tabs
    reconnecting was enough to exhaust the pool and stall every other request in
    the process — observed as ``PoolTimeout`` and a dead API.

    So streaming routes authenticate through this instead. It borrows a
    connection, resolves the session, and gives it straight back.
    """
    # D112: an EventSource cannot send a header, so the stream takes the tab key
    # as `?tab=`. It is a selector and not a credential — it names a session but
    # proves nothing; the httpOnly cookie is still what authenticates. It does
    # reach the access log, which is why it is a key and never the token.
    tokens = cookies.read_session_tokens(request)
    tab_key = request.query_params.get("tab") or request.headers.get(settings().tab_header)

    # rotate=False: a streaming response cannot re-set a cookie mid stream, and
    # its GET mutates nothing (D95).
    with pool().connection() as conn:
        raw = sessions.token_for_tab(conn, tokens, tab_key)
        if not raw:
            raise HTTPException(status_code=401, detail="not authenticated")
        session = sessions.resolve(conn, raw, rotate=False)

    if not session:
        raise HTTPException(status_code=401, detail="session expired or revoked")

    role = session["role"]
    if session["totp_enabled"] and not session["mfa_satisfied"]:
        raise HTTPException(status_code=401, detail="multi-factor authentication required")

    _enforce_csrf(request, session)

    return {
        "id": session["user_id"],
        "email": session["email"],
        "display_name": session["display_name"],
        "role": role,
        "mfa_satisfied": session["mfa_satisfied"],
        "permissions": sorted(str(p) for p in permissions_for(role)),
    }


def requires_streaming(permission: Permission):
    """Permission guard for long-lived responses. See ``current_user_short``."""

    def guard(user: dict[str, Any] = Depends(current_user_short)) -> dict[str, Any]:
        if str(permission) not in user["permissions"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"role {user['role']} does not hold {permission}")
        return user

    return guard


def requires(permission: Permission):
    """Dependency factory enforcing one permission.

    Separation of duties (D12b) is enforced here, in code, on every route — not
    by hiding a button in the frontend. ADMIN holds no case permission, so an
    administrator calling the case API directly gets a 403 exactly like anyone
    else without it.
    """

    def guard(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if str(permission) not in user["permissions"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role {user['role']} does not hold {permission}. "
                    "This is separation of duties, not a bug."
                ),
            )
        return user

    return guard

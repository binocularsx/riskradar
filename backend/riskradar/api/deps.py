"""Request-scoped dependencies: database handles, identity, permissions."""

from __future__ import annotations

from typing import Any, Iterator

from fastapi import Depends, HTTPException, Request, status

from ..config import settings
from ..db import pool
from ..security import sessions
from ..security.rbac import Permission, mfa_required, permissions_for
from ..security.tokens import hash_api_key


def get_conn() -> Iterator[Any]:
    """One pooled connection per request, in a transaction.

    Committed by the pool on clean exit, rolled back on exception — so a handler
    that raises cannot leave a half-written case behind.
    """
    with pool().connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Machine authentication — ingestion (FR-001)
# ---------------------------------------------------------------------------


def require_api_key(request: Request, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    raw = request.headers.get("x-api-key")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM api_keys WHERE key_hash = %s AND active",
            (hash_api_key(raw),),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key"
        )
    row = dict(row) if not isinstance(row, dict) else row
    with conn.cursor() as cur:
        cur.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row["id"],))
    return row


# ---------------------------------------------------------------------------
# Human authentication — the console
# ---------------------------------------------------------------------------


def current_user(request: Request, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    raw = request.cookies.get(settings().session_cookie)
    if not raw:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = sessions.resolve(conn, raw)
    if not session:
        raise HTTPException(status_code=401, detail="session expired or revoked")

    role = session["role"]
    # D12a: a session that has not satisfied MFA is not a session for a role that
    # requires it. Checked on every request, not only at login, so enabling the
    # requirement takes effect immediately for everyone already signed in.
    if mfa_required(role) and not session["mfa_satisfied"]:
        raise HTTPException(status_code=401, detail="multi-factor authentication required")

    return {
        "id": session["user_id"],
        "email": session["email"],
        "display_name": session["display_name"],
        "role": role,
        "mfa_satisfied": session["mfa_satisfied"],
        "permissions": sorted(str(p) for p in permissions_for(role)),
    }


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

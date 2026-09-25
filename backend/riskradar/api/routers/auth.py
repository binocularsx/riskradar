"""Authentication routes.

The audit trail is worth nothing without authenticated identity — "analyst
cleared this case" means nothing if anyone can assert they are any analyst — so
this is not a peripheral feature, it is what makes §13 true.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...audit import chain
from ...config import settings
from ...security import cookies, sessions
from ...security.passwords import verify_password, verify_totp
from ...security.rbac import permissions_for
from ..deps import current_user, get_conn
from ..schemas import LoginIn, LoginOut, MeOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut)
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    conn: Any = Depends(get_conn),
) -> LoginOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, email, display_name, password_hash, role,
                   totp_secret, totp_enabled, active, is_system
              FROM users WHERE lower(email) = lower(%s)
            """,
            (body.email,),
        )
        row = cur.fetchone()
    user = dict(row) if row and not isinstance(row, dict) else row

    # One message for every failure mode. Distinguishing "no such user" from
    # "wrong password" hands an attacker a user-enumeration oracle for free.
    invalid = HTTPException(status_code=401, detail="invalid credentials")

    if not user or not user["active"] or user["is_system"]:
        verify_password(None, body.password)  # equalise timing
        raise invalid
    if not verify_password(user["password_hash"], body.password):
        raise invalid

    role = user["role"]
    # D105: the user's own switch decides, and an administrator can change it
    # under maker-checker. Before this the role forced it (D95) and the column
    # was inert, so an administrator could appear to turn MFA off and not.
    needs_mfa = bool(user["totp_enabled"])

    if needs_mfa:
        if not body.totp_code:
            # Not an error: the client should now collect a code. The password
            # was correct, but no session exists yet, so nothing is granted.
            return LoginOut(status="mfa_required")
        if not verify_totp(user["totp_secret"], body.totp_code):
            raise invalid

    raw, csrf = sessions.create(
        conn,
        user["id"],
        mfa_satisfied=bool(needs_mfa),
        user_agent=request.headers.get("user-agent"),
    )
    cookies.set_session_cookie(response, raw)
    cookies.set_csrf_cookie(response, csrf)

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="LOGIN",
        object_type="user",
        object_id=user["id"],
        payload={"role": role, "mfa": bool(needs_mfa)},
    )

    return LoginOut(
        status="ok",
        user={
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "role": role,
            "permissions": sorted(str(p) for p in permissions_for(role)),
        },
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    conn: Any = Depends(get_conn),
) -> dict[str, str]:
    """Revocation is a DELETE (D12). That sentence is the whole argument against
    a JWT here."""
    raw = request.cookies.get(settings().session_cookie)
    if raw:
        session = sessions.resolve(conn, raw, rotate=False)
        if session:
            # D95: logout changes state, so it too carries the CSRF token — a
            # forged cross-site logout is only a nuisance, but the control is
            # uniform rather than case-by-case.
            presented = request.headers.get(settings().csrf_header)
            if not sessions.verify_csrf(session, presented):
                raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
            chain.append(
                conn,
                actor_user_id=session["user_id"],
                action="LOGOUT",
                object_type="user",
                object_id=session["user_id"],
            )
        sessions.revoke(conn, raw)
    cookies.clear_session_cookies(response)
    return {"status": "ok"}


@router.get("/me", response_model=MeOut)
def me(user: dict = Depends(current_user)) -> MeOut:
    return MeOut(**user)

"""D95: session and MFA hardening (implementation plan §3).

Three properties made checkable rather than asserted:

* MFA is mandatory for *every* human role, not only leads and admins.
* an unsafe, cookie-authenticated request without a valid CSRF token is refused.
* the session identifier rotates, the session survives the rotation, and the
  just-superseded identifier stays valid only for its grace window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.security import sessions
from riskradar.security.rbac import MFA_REQUIRED_ROLES, mfa_required
from riskradar.security.tokens import hash_session_id

from conftest import login


# ---------------------------------------------------------------------------
# MFA for every case user (D95, was D12a)
# ---------------------------------------------------------------------------


def test_mfa_is_mandatory_for_every_human_role():
    for role in ("ANALYST", "FRAUD_OPS_LEAD", "ADMIN"):
        assert mfa_required(role), f"{role} works cases or tunes detection; MFA is not optional"
    # The one principal that never logs in holds no factor and is not required to.
    assert not mfa_required("SYSTEM")
    assert "SYSTEM" not in MFA_REQUIRED_ROLES


def test_analyst_password_alone_yields_no_session(client):
    """An analyst is a case user; a correct password without a code is not a
    session (this is the case D12a left as merely default-on)."""
    r = client.post(
        "/v1/auth/login",
        json={"email": "analyst@riskradar.local", "password": "Analyst#2026"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "mfa_required"
    assert client.get("/v1/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# CSRF double-submit (D95)
# ---------------------------------------------------------------------------


def _login_without_default_csrf_header(client):
    """Log in so the client holds the session and CSRF cookies, but do NOT set
    the CSRF header — so a following POST is exactly a request that omits it.

    The tab key *is* set: without it the request is not authenticated at all
    (D112), and these tests are about CSRF, not about authentication."""
    import pyotp
    import psycopg

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        secret = c.execute(
            "SELECT totp_secret FROM users WHERE email = %s",
            ("analyst@riskradar.local",),
        ).fetchone()["totp_secret"]
    r = client.post(
        "/v1/auth/login",
        json={
            "email": "analyst@riskradar.local",
            "password": "Analyst#2026",
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert r.json()["status"] == "ok"
    client.headers[settings().tab_header] = r.json()["tab_key"]


def test_unsafe_request_without_csrf_token_is_refused(client):
    _login_without_default_csrf_header(client)
    # Authenticated (the session cookie is sent automatically)...
    assert client.get("/v1/auth/me").status_code == 200
    # ...but a state-changing call with no CSRF header is refused.
    r = client.post("/v1/worklist/next")
    assert r.status_code == 403
    assert "csrf" in r.json()["detail"].lower()


def test_unsafe_request_with_csrf_token_is_allowed(client):
    _login_without_default_csrf_header(client)
    csrf = client.cookies.get(settings().csrf_cookie)
    assert csrf, "login must set the readable CSRF cookie"
    r = client.post("/v1/worklist/next", headers={settings().csrf_header: csrf})
    assert r.status_code != 403, r.text


def test_a_forged_csrf_token_is_refused(client):
    _login_without_default_csrf_header(client)
    r = client.post("/v1/worklist/next", headers={settings().csrf_header: "not-the-token"})
    assert r.status_code == 403


def test_safe_requests_need_no_csrf_token(client):
    _login_without_default_csrf_header(client)
    assert client.get("/v1/cases?scope=mine").status_code == 200


# ---------------------------------------------------------------------------
# Session rotation with a grace window (D95)
# ---------------------------------------------------------------------------


def _analyst_id(conn) -> int:
    return conn.execute(
        "SELECT id FROM users WHERE email = %s", ("analyst@riskradar.local",)
    ).fetchone()["id"]


def _backdate_rotation(conn, raw: str, minutes: int) -> None:
    conn.execute(
        "UPDATE sessions SET rotated_at = %s WHERE id = %s",
        (datetime.now(timezone.utc) - timedelta(minutes=minutes), hash_session_id(raw)),
    )


def test_a_fresh_session_does_not_rotate(conn):
    raw, csrf, _tab = sessions.create(conn, _analyst_id(conn), mfa_satisfied=True)
    resolved = sessions.resolve(conn, raw)
    assert resolved is not None
    assert resolved["rotated_token"] is None
    assert resolved["csrf_token"] == csrf


def test_session_rotates_past_the_interval_and_survives(conn):
    uid = _analyst_id(conn)
    raw, _, _tab = sessions.create(conn, uid, mfa_satisfied=True)
    _backdate_rotation(conn, raw, settings().session_rotate_minutes + 5)

    rotated = sessions.resolve(conn, raw)
    assert rotated is not None
    new_raw = rotated["rotated_token"]
    assert new_raw and new_raw != raw, "a due session must hand back a new identifier"
    assert rotated["user_id"] == uid, "rotation replaces the cookie, not the session"

    # The new identifier resolves as the same session, and does not re-rotate.
    again = sessions.resolve(conn, new_raw)
    assert again is not None and again["user_id"] == uid
    assert again["rotated_token"] is None


def test_the_superseded_identifier_works_within_grace_then_dies(conn):
    uid = _analyst_id(conn)
    raw, _, _tab = sessions.create(conn, uid, mfa_satisfied=True)
    _backdate_rotation(conn, raw, settings().session_rotate_minutes + 5)
    rotated = sessions.resolve(conn, raw)
    assert rotated["rotated_token"], "precondition: it rotated"

    # The old cookie, still in flight from a parallel request, resolves in grace.
    old_still = sessions.resolve(conn, raw)
    assert old_still is not None and old_still["user_id"] == uid
    assert old_still["rotated_token"] is None, "grace match must not re-rotate"

    # Push the rotation past the grace window: the old identifier is now dead.
    conn.execute(
        "UPDATE sessions SET rotated_at = %s WHERE previous_id = %s",
        (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings().session_rotate_grace_seconds + 30),
            hash_session_id(raw),
        ),
    )
    assert sessions.resolve(conn, raw) is None


def test_logout_still_ends_a_rotated_session(conn):
    uid = _analyst_id(conn)
    raw, _, _tab = sessions.create(conn, uid, mfa_satisfied=True)
    _backdate_rotation(conn, raw, settings().session_rotate_minutes + 5)
    rotated = sessions.resolve(conn, raw)
    new_raw = rotated["rotated_token"]

    # Revoking with the *old* cookie must still delete the session.
    sessions.revoke(conn, raw)
    assert sessions.resolve(conn, new_raw) is None


# ---------------------------------------------------------------------------
# The session is committed before the login answers
# ---------------------------------------------------------------------------


def _session_ids_for(dsn: str, email: str) -> set[str]:
    """Which sessions exist for this user, seen from outside any request."""
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as other:
        rows = other.execute(
            """
            SELECT s.id FROM sessions s
              JOIN users u ON u.id = s.user_id
             WHERE lower(u.email) = lower(%s)
            """,
            (email,),
        ).fetchall()
    return {r["id"] for r in rows}


def test_login_commits_the_session_before_it_answers(client, dsn):
    """A session the client has been told about must already exist to everybody.

    ``get_conn`` commits at dependency teardown, and FastAPI runs that teardown
    after the response has been handed off — so a login that left its commit to
    the pool could deliver a working tab key microseconds before the ``sessions``
    row was visible to any other connection. The request the console fires next
    found no session and answered 401; measured at roughly one immediate
    follow-up call in ten.

    The probe below is not timing-sensitive. It runs after the handler has
    returned and *while still inside* the pool's block, so the pool has not
    committed anything yet. A row visible to an independent connection at that
    point can only have been committed by the handler itself, before it answered.
    """
    from riskradar.api.app import app
    from riskradar.api.deps import get_conn
    from riskradar.db import pool

    email = "analyst@riskradar.local"
    before = _session_ids_for(dsn, email)
    seen: list[set[str]] = []

    def probing_conn():
        with pool().connection() as conn:
            yield conn
            seen.append(_session_ids_for(dsn, email))

    app.dependency_overrides[get_conn] = probing_conn
    try:
        login(client, email, "Analyst#2026")
    finally:
        app.dependency_overrides.pop(get_conn, None)

    assert seen, "the probe never ran, so this test proves nothing"
    created = seen[-1] - before
    assert created, (
        "the login response was ready while its session row was still "
        "uncommitted — the next request would have been refused"
    )

    # This one did commit, so clear it up rather than leaving it to expire.
    with psycopg.connect(dsn) as other:
        other.execute(
            "DELETE FROM sessions WHERE id = ANY(%s)", (list(created),)
        )
        other.commit()

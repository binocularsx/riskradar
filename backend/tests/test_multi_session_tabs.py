"""D112: one browser, one session per tab.

The session cookie used to carry a single identifier, so a browser held one
session per host. That produced three behaviours the desk reported:

* signing in as a second role replaced the first;
* signing out of one tab signed every tab out, because they shared the cookie;
* opening the console in a new tab landed in whatever session was already there.

One ``TestClient`` is one cookie jar — that is, one browser. A "tab" is a tab
key sent with a request, so these tests put several tabs in one browser exactly
as a person would.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from riskradar.config import settings

PASSWORDS = {
    "analyst@riskradar.local": "Analyst#2026",
    "lead@riskradar.local": "OpsLead#2026",
}


@pytest.fixture
def browser():
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def open_tab(browser: TestClient, email: str) -> dict:
    """Sign in in a new tab of this browser. Returns the tab's own headers."""
    import pyotp

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        secret = c.execute(
            "SELECT totp_secret FROM users WHERE email = %s", (email,)
        ).fetchone()["totp_secret"]
    r = browser.post("/v1/auth/login", json={
        "email": email, "password": PASSWORDS[email], "totp_code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    return {settings().tab_header: body["tab_key"],
            settings().csrf_header: body["csrf_token"]}


def whoami(browser: TestClient, tab: dict) -> int:
    r = browser.get("/v1/auth/me", headers=tab)
    assert r.status_code == 200, r.text
    return r.json()["email"]


def test_two_roles_are_signed_in_at_once_in_one_browser(browser):
    analyst = open_tab(browser, "analyst@riskradar.local")
    lead = open_tab(browser, "lead@riskradar.local")

    # The second login did not replace the first: both tabs are live, each as
    # the person who signed into it.
    assert whoami(browser, analyst) == "analyst@riskradar.local"
    assert whoami(browser, lead) == "lead@riskradar.local"
    assert analyst[settings().tab_header] != lead[settings().tab_header]


def test_a_new_tab_starts_signed_out_even_though_the_browser_is_signed_in(browser):
    open_tab(browser, "analyst@riskradar.local")
    # A new tab shares the cookie but has no key of its own: sessionStorage is
    # not shared between tabs. It must be asked to sign in.
    assert browser.get("/v1/auth/me").status_code == 401
    # And a key that names no session gets nothing either.
    assert browser.get("/v1/auth/me",
                       headers={settings().tab_header: "not-a-real-key"}).status_code == 401


def test_signing_out_of_one_tab_leaves_the_others_signed_in(browser):
    analyst = open_tab(browser, "analyst@riskradar.local")
    lead = open_tab(browser, "lead@riskradar.local")

    assert browser.post("/v1/auth/logout", headers=analyst).status_code == 200

    # That tab is done...
    assert browser.get("/v1/auth/me", headers=analyst).status_code == 401
    # ...and the other one never noticed.
    assert whoami(browser, lead) == "lead@riskradar.local"


def test_a_tab_cannot_use_a_session_this_browser_does_not_hold(browser):
    """The key selects; the cookie proves."""
    from riskradar.api.app import app

    other = TestClient(app)
    other.__enter__()
    stolen = open_tab(other, "lead@riskradar.local")  # another browser's session

    open_tab(browser, "analyst@riskradar.local")
    # This browser's cookie does not carry that session, so naming it is useless.
    assert browser.get("/v1/auth/me", headers=stolen).status_code == 401
    other.__exit__(None, None, None)

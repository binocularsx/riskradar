"""Cookie helpers shared by the login handler and the request dependencies.

Two cookies carry the human session (D12, D95):

* the **session** cookie — ``httpOnly``, so no script can read it and an XSS
  cannot lift the session. Its value is a random token; only the hash is stored.
* the **CSRF** cookie — deliberately *not* ``httpOnly``, because the double-submit
  defence needs the page's own script to read it and echo it in a header. It is
  not a credential: on its own it grants nothing, and a cross-origin page can
  neither read it nor set the matching header.

Both are ``Secure`` in production and ``SameSite=Lax``. Kept in one place so the
login path and the rotation path set them identically — a rotated session that
came back with a subtly different cookie attribute would silently stop working.

D112: the session cookie carries **several** identifiers, up to
``MAX_SESSIONS``, separated by ``~`` (never produced by ``token_urlsafe``). One
browser can therefore hold one session per tab instead of one in total, which is
what lets two roles be signed in at once, lets signing out of one tab leave the
others alone, and lets a new tab start with no session of its own. Which of them
a request is using is named by the tab, not by the cookie — see
``sessions.token_for_tab``.
"""

from __future__ import annotations

from typing import Any

from ..config import settings


SEPARATOR = "~"
MAX_SESSIONS = 4


def read_session_tokens(request: Any) -> list[str]:
    """Every session identifier the browser is carrying for this host."""
    raw = request.cookies.get(settings().session_cookie) or ""
    return [t for t in raw.split(SEPARATOR) if t]


def add_session_token(existing: list[str], raw_token: str) -> list[str]:
    """This tab's new session, alongside the ones other tabs are using.

    Oldest first, so the cap drops the least recently added. A browser past the
    cap is a demo desk with a lot of tabs, not an attack, so the oldest simply
    falls out of the cookie — its row stays until it expires or is revoked.
    """
    kept = [t for t in existing if t != raw_token]
    return (kept + [raw_token])[-MAX_SESSIONS:]


def set_session_cookie(response: Any, raw_tokens: str | list[str]) -> None:
    s = settings()
    tokens = [raw_tokens] if isinstance(raw_tokens, str) else list(raw_tokens)
    response.set_cookie(
        key=s.session_cookie,
        value=SEPARATOR.join(tokens),
        httponly=True,           # unreadable by any script, so XSS cannot lift it
        secure=s.is_production,  # http is only tolerable on a local demo machine
        samesite="lax",          # blocks cross-site POSTs while keeping normal navigation
        max_age=s.session_absolute_hours * 3600,
        path="/",
    )


def set_csrf_cookie(response: Any, csrf_token: str) -> None:
    s = settings()
    response.set_cookie(
        key=s.csrf_cookie,
        value=csrf_token,
        httponly=False,          # the page must read this one and echo it in a header
        secure=s.is_production,
        samesite="lax",
        max_age=s.session_absolute_hours * 3600,
        path="/",
    )


def clear_session_cookies(response: Any) -> None:
    s = settings()
    response.delete_cookie(s.session_cookie, path="/")
    response.delete_cookie(s.csrf_cookie, path="/")


def write_remaining(response: Any, remaining: list[str]) -> None:
    """After a logout: keep the other tabs signed in, or clear the cookie.

    Signing out of one tab used to delete the cookie, which signed out every
    other tab on the host with it (D112).
    """
    if remaining:
        set_session_cookie(response, remaining)
    else:
        clear_session_cookies(response)

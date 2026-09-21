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
"""

from __future__ import annotations

from typing import Any

from ..config import settings


def set_session_cookie(response: Any, raw_token: str) -> None:
    s = settings()
    response.set_cookie(
        key=s.session_cookie,
        value=raw_token,
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

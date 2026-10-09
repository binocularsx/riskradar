"""Live MFA codes for the seeded accounts. `python scripts/codes.py`

A **development convenience only**, and a sibling of ``totp.py``: that one
prints a single code and exits, which means asking for a new one every thirty
seconds. This keeps a window open and reprints the whole desk each time the
codes roll, so signing in never needs a round trip to anybody.

It has no privileged access. It reads the secrets from the database exactly as
an operator with database access could, and the server verifies the codes it
prints exactly as it would verify a code from a phone.

    python scripts/codes.py             # live ticker, Ctrl+C to stop
    python scripts/codes.py --enroll    # one-time setup for an authenticator app
    python scripts/codes.py --qr        # the same, as QR codes a phone can scan

``--enroll`` prints the otpauth:// URI and the base32 secret for each account.
Add them to Google Authenticator, Authy, 1Password or similar once and the
phone generates the same codes this script does — no terminal at all. Note that
``demo_reset.py --stage schema`` reseeds the users, which mints **new** secrets;
re-run ``--enroll`` after a rebuild or the phone's codes will be rejected.
"""
import io
import sys
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg
import pyotp

from riskradar.config import settings
from riskradar.security.passwords import totp_uri
from riskradar.security.rbac import mfa_required

STEP = 30  # seconds; pyotp's default and what the server verifies against


def accounts() -> list[dict]:
    """The accounts a code can actually sign in to.

    Deactivated accounts are left out deliberately. Their secrets still work
    arithmetically, so enrolling one gives a six-digit code that looks right
    and is refused every time — and the leftovers of crashed test runs, plus
    the InfoSec account D111 retired, outnumber the real desk here.
    """
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            """SELECT email, role, totp_secret, totp_enabled
                 FROM users
                WHERE NOT is_system AND active
             ORDER BY role, email"""
        ).fetchall()


def wants_code(row: dict) -> bool:
    """Mirror auth.py: the role can require a code the user row does not."""
    return bool(row["totp_enabled"] or mfa_required(row["role"]))


def enroll(rows: list[dict]) -> None:
    print("\n  Add these to an authenticator app once, then you never need this script.\n")
    for r in rows:
        print(f"  {r['role']}  {r['email']}")
        if not r["totp_secret"]:
            print("      no secret on this account\n")
            continue
        if not wants_code(r):
            print("      MFA is OFF for this account - password only, no code needed")
        print(f"      secret  {r['totp_secret']}")
        print(f"      uri     {totp_uri(r['totp_secret'], r['email'])}\n")


def ticker(rows: list[dict]) -> None:
    live = [r for r in rows if r["totp_secret"] and wants_code(r)]
    off = [r for r in rows if not wants_code(r)]
    if not live:
        print("  no account currently requires a code")
    width = max((len(r["email"]) for r in live), default=0)

    shown = None
    try:
        while True:
            remaining = STEP - int(time.time()) % STEP
            codes = tuple(pyotp.TOTP(r["totp_secret"]).now() for r in live)
            if codes != shown:
                shown = codes
                print("\n  Risk Radar - MFA codes            (Ctrl+C to stop)")
                for r, code in zip(live, codes):
                    print(f"    {r['email']:<{width}}  {code[:3]} {code[3:]}   {r['role']}")
                for r in off:
                    print(f"    {r['email']:<{width}}  no code needed - MFA off")
            print(f"    rolls in {remaining:2d}s ", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n")


def qr(rows: list[dict]) -> None:
    """Write the enrolment URIs as scannable QR codes.

    The file lands in the repo's gitignored ``.local/`` directory, because it
    holds the TOTP secrets in full: anything that can read it can mint codes for
    these accounts. They are seeded development accounts on a synthetic
    database, but the file still has no business in version control.

    Rendering happens locally with segno. Handing an authenticator secret to an
    online QR generator would post the credential to a third party, which is
    exactly the thing the second factor exists to prevent.
    """
    import base64

    import segno

    out = REPO_ROOT / ".local" / "mfa-enrolment.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    cards = []
    for r in rows:
        if not r["totp_secret"] or not wants_code(r):
            continue
        # A short label keeps the QR a version smaller, so its modules are
        # bigger and a phone camera locks on far more easily. Label and issuer
        # are cosmetic: only the secret decides the codes.
        account = r["email"].split("@")[0]
        uri = (f"otpauth://totp/RiskRadar:{account}"
               f"?secret={r['totp_secret']}&issuer=RiskRadar")
        buf = io.BytesIO()
        # PNG, not SVG. An SVG QR is anti-aliased when the browser scales it and
        # the soft module edges defeat some scanners; a raster at 10px a module
        # with the default 4-module quiet zone stays sharp.
        segno.make(uri, error="m").save(buf, kind="png", scale=10, border=4)
        png = base64.b64encode(buf.getvalue()).decode()
        cards.append(
            f'<figure><img alt="QR code for {r["email"]}" src="data:image/png;base64,{png}">'
            f"<figcaption><b>{r['email']}</b><span>{r['role']}</span>"
            f"<code>{r['totp_secret']}</code></figcaption></figure>"
        )

    out.write_text(
        "<!doctype html><meta charset=utf-8><title>Risk Radar MFA enrolment</title>"
        "<style>body{font:14px system-ui;margin:32px;background:#fff;color:#111827}"
        "h1{font-size:19px;margin:0 0 4px}p.note{color:#4b5563;margin:0 0 24px;max-width:62ch}"
        "main{display:flex;flex-wrap:wrap;gap:28px}"
        "figure{margin:0;width:340px}"
        "img{width:340px;height:340px;display:block;image-rendering:pixelated;"
        "border:1px solid #e5e7eb;border-radius:6px}"
        "figcaption{margin-top:10px;display:flex;flex-direction:column;gap:3px}"
        "figcaption span{color:#4b5563;font-size:12px}"
        "code{font-size:11px;color:#4b5563;word-break:break-all}</style>"
        "<h1>Risk Radar &mdash; authenticator enrolment</h1>"
        "<p class=note>Scan each code, or use your app's <b>enter a setup key</b> option and "
        "type the string under it &mdash; both give identical codes. Seeded development "
        "accounts on a synthetic database, but these are the real TOTP secrets, so this file "
        "stays out of version control. <b>Rebuilding the database with demo_reset.py mints new "
        "secrets and invalidates every code below.</b></p><main>" + "".join(cards) + "</main>",
        encoding="utf-8",
    )
    print()
    print(f"  {len(cards)} QR code(s) written to {out}")
    print("  opening in your browser; scan with an authenticator app")
    print()
    webbrowser.open(out.as_uri())


if __name__ == "__main__":
    rows = accounts()
    if "--qr" in sys.argv:
        qr(rows)
    elif "--enroll" in sys.argv:
        enroll(rows)
    else:
        ticker(rows)

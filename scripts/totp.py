"""Print a current TOTP code for a seeded account.

A **development convenience only**. It exists because the demo runs on one
laptop and typing a code from a phone during a rehearsal loop is friction; the
server verifies this code exactly as it would verify one from an authenticator
app, and this script has no privileged access — it reads the secret from the
database as an operator with database access could.

    python scripts/totp.py analyst@riskradar.local
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg
import pyotp

from riskradar.config import settings

email = sys.argv[1] if len(sys.argv) > 1 else "analyst@riskradar.local"
with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
    row = conn.execute("SELECT totp_secret, role FROM users WHERE email = %s", (email,)).fetchone()

if not row or not row["totp_secret"]:
    raise SystemExit(f"no TOTP secret for {email}")
print(pyotp.TOTP(row["totp_secret"]).now())

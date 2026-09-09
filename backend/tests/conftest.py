"""Shared test fixtures.

Tests run against the real PostgreSQL instance, not a mock and not SQLite. Three
of the decisions under test — ``SKIP LOCKED`` claiming, ``LISTEN``/``NOTIFY``,
and the ``audit_log`` grant — have no meaning on any other engine, so a test
suite that avoided Postgres would be testing a different system than the one that
ships.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.config import settings  # noqa: E402

SIMULATOR_API_KEY = "rr_dev_simulator_key_do_not_use_in_production"


@pytest.fixture(scope="session")
def dsn() -> str:
    return settings().app_dsn


@pytest.fixture
def conn(dsn: str):
    """A connection whose transaction is rolled back after each test.

    Nothing a test writes survives it, so the suite can run repeatedly against a
    seeded demo database without polluting it.
    """
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as c:
        yield c
        c.rollback()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from riskradar.api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def api_headers() -> dict[str, str]:
    return {"X-API-Key": SIMULATOR_API_KEY}


def unique_ref(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@pytest.fixture
def sample_transaction():
    """A minimal, valid transaction payload."""

    def build(**overrides):
        now = datetime.now(timezone.utc)
        payload = {
            "transaction_ref": unique_ref(),
            "occurred_at": now.isoformat(),
            "amount_minor": 250_000,        # NGN 2,500.00 in kobo
            "currency": "NGN",
            "channel": "MOBILE_APP",
            "instrument": "ACCOUNT_TRANSFER",
            "rail": "NIP",
            "customer_id": f"CIF{uuid.uuid4().hex[:10]}",
            "account_id": f"ACC{uuid.uuid4().hex[:10]}",
            "beneficiary_account_id": f"BEN{uuid.uuid4().hex[:10]}",
            "device_fingerprint": f"dev-{uuid.uuid4().hex[:8]}",
            "ip_region": "NG-LA",
            "auth_result": "APPROVED",
            "display_name": "Test Customer",
            "account_opened_at": (now - timedelta(days=400)).isoformat(),
            "last_activity_at": (now - timedelta(days=1)).isoformat(),
            "product_type": "CURRENT",
            "origin_sol_id": "SOL001",
        }
        payload.update(overrides)
        return payload

    return build


def login(client, email: str, password: str) -> None:
    """Log in through the real flow, TOTP included.

    Computing the code from the seeded secret is a test convenience; the server
    verifies it exactly as it would a code from a phone.
    """
    import pyotp

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        row = c.execute(
            "SELECT totp_secret FROM users WHERE email = %s", (email,)
        ).fetchone()
    code = pyotp.TOTP(row["totp_secret"]).now() if row and row["totp_secret"] else None

    response = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password, "totp_code": code},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok", response.text

"""WP-01 / D72: one event, not one transaction.

Pins the envelope's promises: every payment is also an event, the other types
are validated per type and tokenised at the boundary, references are unique
across types, and nothing about payment scoring changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.security.tokens import account_token, beneficiary_token, msisdn_token, subject_token


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def ref(prefix="evt"):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def event(event_type, detail, **envelope):
    body = {
        "event_type": event_type,
        "event_ref": ref(event_type.lower()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": f"CIF{uuid.uuid4().hex[:10]}",
        "channel": "MOBILE_APP",
        "detail": detail,
    }
    body.update(envelope)
    return body


@pytest.fixture
def cleanup():
    refs: list[str] = []
    yield refs
    with _db() as c:
        tx_ids = [r["transaction_id"] for r in c.execute(
            "SELECT transaction_id FROM events WHERE event_ref = ANY(%s) AND transaction_id IS NOT NULL",
            (refs,)).fetchall()]
        c.execute("DELETE FROM events WHERE event_ref = ANY(%s) AND transaction_id IS NULL", (refs,))
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = ANY(%s)", (tx_ids,))
        c.execute("DELETE FROM decisions WHERE transaction_id = ANY(%s)", (tx_ids,))
        c.execute("DELETE FROM transactions WHERE id = ANY(%s)", (tx_ids,))
        c.commit()


def test_a_payment_through_the_old_door_is_also_an_event(client, api_headers, sample_transaction, cleanup):
    payload = sample_transaction()
    cleanup.append(payload["transaction_ref"])
    tx_id = client.post("/v1/transactions", json=payload, headers=api_headers).json()["transaction_id"]
    with _db() as c:
        row = c.execute("SELECT * FROM events WHERE transaction_id = %s", (tx_id,)).fetchone()
    assert row["event_type"] == "PAYMENT" and row["event_ref"] == payload["transaction_ref"]
    assert row["subject_token"] == subject_token(payload["customer_id"])


def test_a_payment_through_the_envelope_is_queued_like_any_payment(client, api_headers, sample_transaction, cleanup):
    payload = sample_transaction()
    cleanup.append(payload["transaction_ref"])
    r = client.post("/v1/events", json={"event_type": "PAYMENT", "payment": payload}, headers=api_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] is True and body["transaction_id"]
    with _db() as c:
        queued = c.execute("SELECT 1 FROM scoring_queue WHERE transaction_id = %s", (body["transaction_id"],)).fetchone()
        scored = c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (body["transaction_id"],)).fetchone()
    assert queued or scored

    again = client.post("/v1/events", json={"event_type": "PAYMENT", "payment": payload}, headers=api_headers)
    assert again.status_code == 200 and again.json()["status"] == "duplicate"


def test_non_payment_events_are_stored_tokenised_and_not_scored(client, api_headers, cleanup):
    login = event("LOGIN", {"result": "FAILED", "method": "PASSWORD", "failure_reason": "WRONG_CREDENTIAL"},
                  account_id="ACC-raw-1", device_fingerprint="dev-raw-1", ip_region="NG-LA")
    payee = event("PAYEE_ADDED", {"beneficiary_account_id": "BEN-raw-9", "beneficiary_bank_code": "058"},
                  account_id="ACC-raw-1")
    sim = event("SIM_CHANGED", {"msisdn": "+234 803 555 0101", "carrier": "MTN"})
    for e in (login, payee, sim):
        cleanup.append(e["event_ref"])
        r = client.post("/v1/events", json=e, headers=api_headers)
        assert r.status_code == 202, r.text
        assert r.json()["queued"] is False and r.json()["transaction_id"] is None

    with _db() as c:
        rows = {r["event_type"]: r for r in c.execute(
            "SELECT * FROM events WHERE event_ref = ANY(%s)", ([login["event_ref"], payee["event_ref"], sim["event_ref"]],)
        ).fetchall()}
    assert rows["LOGIN"]["account_token"] == account_token("ACC-raw-1")
    assert rows["LOGIN"]["detail"] == {"result": "FAILED", "method": "PASSWORD", "failure_reason": "WRONG_CREDENTIAL"}
    assert rows["PAYEE_ADDED"]["detail"]["beneficiary_token"] == beneficiary_token("BEN-raw-9")
    assert rows["SIM_CHANGED"]["detail"]["msisdn_token"] == msisdn_token("2348035550101")

    blob = " ".join(str(v) for r in rows.values() for v in r.values())
    for raw in ("ACC-raw-1", "dev-raw-1", "BEN-raw-9", "803 555", "8035550101", login["customer_id"]):
        assert raw not in blob


def test_detail_must_match_the_event_type(client, api_headers):
    wrong = event("LOGIN", {"msisdn": "+2348035550101"})
    assert client.post("/v1/events", json=wrong, headers=api_headers).status_code == 422
    unknown = event("TELEPORTED", {})
    assert client.post("/v1/events", json=unknown, headers=api_headers).status_code == 422


def test_types_that_need_an_identity_require_it(client, api_headers):
    no_device = event("DEVICE_BOUND", {"binding": "BOUND"})
    assert client.post("/v1/events", json=no_device, headers=api_headers).status_code == 422
    no_account = event("LIMIT_CHANGED", {"limit": "DAILY_TRANSFER", "from_minor": 1, "to_minor": 2})
    assert client.post("/v1/events", json=no_account, headers=api_headers).status_code == 422


def test_naive_timestamps_are_refused(client, api_headers):
    naive = event("LOGIN", {"result": "SUCCESS", "method": "PIN"}, occurred_at="2026-09-14T10:00:00")
    assert client.post("/v1/events", json=naive, headers=api_headers).status_code == 422


def test_a_reference_names_one_event_across_types(client, api_headers, sample_transaction, cleanup):
    login = event("LOGIN", {"result": "SUCCESS", "method": "BIOMETRIC"})
    cleanup.append(login["event_ref"])
    assert client.post("/v1/events", json=login, headers=api_headers).status_code == 202
    assert client.post("/v1/events", json=login, headers=api_headers).json()["status"] == "duplicate"

    clash = event("SIM_CHANGED", {"msisdn": "08035550101"}, event_ref=login["event_ref"])
    assert client.post("/v1/events", json=clash, headers=api_headers).status_code == 409
    payment = sample_transaction(transaction_ref=login["event_ref"])
    assert client.post("/v1/transactions", json=payment, headers=api_headers).status_code == 409


def test_batch_rejects_one_without_losing_the_rest(client, api_headers, cleanup):
    good = event("CREDENTIAL_CHANGED", {"credential": "PIN", "initiated_by": "CUSTOMER"})
    first = event("LOGIN", {"result": "SUCCESS", "method": "OTP"})
    clash = event("SIM_CHANGED", {"msisdn": "08035550101"}, event_ref=first["event_ref"])
    cleanup.extend([good["event_ref"], first["event_ref"]])
    body = client.post("/v1/events/batch", json={"events": [first, clash, good]}, headers=api_headers).json()
    assert body["accepted"] == 2 and body["rejected"] == 1
    assert body["errors"][0]["index"] == 1


def test_events_require_an_api_key(client):
    assert client.post("/v1/events", json=event("LOGIN", {"result": "SUCCESS", "method": "PIN"})).status_code == 401


def test_events_cannot_be_altered(cleanup, client, api_headers):
    e = event("LOGIN", {"result": "SUCCESS", "method": "PIN"})
    cleanup.append(e["event_ref"])
    client.post("/v1/events", json=e, headers=api_headers)
    # The grant is the control (the app role holds no UPDATE on events); the
    # row trigger stands behind it for any role that does.
    with _db() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE events SET ip_region = 'NG-AB' WHERE event_ref = %s", (e["event_ref"],))


def test_only_scored_types_are_queued():
    """D72: the feature package names what it scores, and every name is a real type."""
    from typing import get_args

    from riskradar.api.schemas import EventType
    from riskradar.features.spec import SCORED_EVENT_TYPES

    assert SCORED_EVENT_TYPES <= set(get_args(EventType))
    assert SCORED_EVENT_TYPES == {"PAYMENT"}

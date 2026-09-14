"""WP-07 / D74: a decision a bank can act on.

Pins the contract a switch would be built against: one directive per live
decision, fail open whenever it is late or in shadow, LIVE impossible without a
signature, and delivery and acknowledgement recorded once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.policy.directives import ACTION_FOR_DECISION, effective
from riskradar.worker.scoring import score_transaction, system_user_id

from conftest import login

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def d(mode="SHADOW", action="DECLINE", ttl=120):
    return {"mode": mode, "action": action, "fail_open_action": "APPROVE",
            "expires_at": T0 + timedelta(seconds=ttl)}


# ------------------------------------------------------------------ pure


def test_every_decision_maps_to_an_action():
    assert ACTION_FOR_DECISION == {"ALLOW": "APPROVE", "MONITOR": "APPROVE_AND_MONITOR",
                                   "REVIEW": "HOLD_FOR_REVIEW", "HOLD": "DECLINE"}


def test_shadow_never_enforces():
    e = effective(d("SHADOW"), T0 + timedelta(seconds=1))
    assert (e["enforce"], e["effective_action"]) == (False, "APPROVE")


def test_live_enforces_only_while_fresh():
    assert effective(d("LIVE"), T0 + timedelta(seconds=10))["effective_action"] == "DECLINE"
    stale = effective(d("LIVE"), T0 + timedelta(seconds=121))
    assert (stale["expired"], stale["enforce"], stale["effective_action"]) == (True, False, "APPROVE")


# -------------------------------------------------------------- database


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def test_live_is_refused_without_a_signature(conn):
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO enforcement_policies (version, mode, ttl_seconds, policy_text) "
                     "VALUES (999, 'LIVE', 60, 'unsigned live policy text')")


def test_fail_open_cannot_be_a_decline(conn):
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO enforcement_policies (version, mode, ttl_seconds, fail_open_action, policy_text) "
                     "VALUES (998, 'SHADOW', 60, 'DECLINE', 'fail closed is not fail open')")


@pytest.fixture
def scored(client, api_headers, sample_transaction):
    """A live payment, ingested and scored in this test, removed afterwards."""
    payload = sample_transaction()
    tx_id = client.post("/v1/transactions", json=payload, headers=api_headers).json()["transaction_id"]
    with _db() as c:
        if not c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone():
            score_transaction(c, tx_id, sys_uid=system_user_id(c))
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
        c.commit()
    yield payload["transaction_ref"], tx_id
    with _db() as c:
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
        c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        c.commit()


def test_a_live_decision_issues_one_shadow_directive_and_records_delivery(client, api_headers, scored):
    ref, tx_id = scored
    first = client.get(f"/v1/directives/{ref}", headers=api_headers).json()
    assert first["status"] == "ISSUED" and first["mode"] == "SHADOW" and first["enforce"] is False
    assert first["effective_action"] == "APPROVE" and first["first_delivered_at"]

    second = client.get(f"/v1/directives/{ref}", headers=api_headers).json()
    assert second["first_delivered_at"] == first["first_delivered_at"]
    with _db() as c:
        row = c.execute("SELECT delivery_count FROM directives WHERE transaction_id = %s", (tx_id,)).fetchone()
    assert row["delivery_count"] == 2


def test_acknowledgement_is_once_and_shadow_cannot_be_applied(client, api_headers, scored):
    ref, _ = scored
    directive = client.get(f"/v1/directives/{ref}", headers=api_headers).json()
    url = f"/v1/directives/{directive['directive_ref']}/ack"
    taken = datetime.now(timezone.utc).isoformat()

    assert client.post(url, json={"action_taken": "APPLIED", "taken_at": taken}, headers=api_headers).status_code == 400
    ok = client.post(url, json={"action_taken": "SHADOW_RECORDED", "taken_at": taken}, headers=api_headers)
    assert ok.status_code == 200 and ok.json()["status"] == "acknowledged"
    again = client.post(url, json={"action_taken": "SHADOW_RECORDED", "taken_at": taken}, headers=api_headers)
    assert again.json()["status"] == "duplicate"
    different = client.post(url, json={"action_taken": "NOT_APPLIED", "taken_at": taken}, headers=api_headers)
    assert different.status_code == 409


def test_an_issued_directive_cannot_be_rewritten(scored):
    _, tx_id = scored
    with _db() as c, pytest.raises(psycopg.errors.RaiseException):
        c.execute("UPDATE directives SET expires_at = expires_at + interval '1 hour' WHERE transaction_id = %s", (tx_id,))


def test_unscored_and_unknown_payments_fail_open(client, api_headers, sample_transaction):
    assert client.get("/v1/directives/no-such-ref", headers=api_headers).status_code == 404
    payload = sample_transaction()
    tx_id = client.post("/v1/transactions", json=payload, headers=api_headers).json()["transaction_id"]
    try:
        r = client.get(f"/v1/directives/{payload['transaction_ref']}", headers=api_headers)
        # Either a worker scored it already, or it is pending and fails open.
        assert r.json()["effective_action"] == "APPROVE"
        if r.json()["status"] == "PENDING":
            assert r.status_code == 202
    finally:
        with _db() as c:
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
            c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
            c.commit()


def test_directives_need_an_api_key(client):
    assert client.get("/v1/directives").status_code == 401


def test_staff_views_and_admin_only_publishing(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    assert client.get("/v1/admin/enforcement-policy").json()["active"]["mode"] == "SHADOW"
    assert "by_action" in client.get("/v1/metrics/directives").json()
    body = {"mode": "SHADOW", "ttl_seconds": 60, "policy_text": "a lead may not publish enforcement policy"}
    assert client.post("/v1/admin/enforcement-policy", json=body).status_code == 403


def test_admin_cannot_publish_live_unsigned(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    body = {"mode": "LIVE", "ttl_seconds": 60, "policy_text": "live without anyone signing for it"}
    assert client.post("/v1/admin/enforcement-policy", json=body).status_code == 400

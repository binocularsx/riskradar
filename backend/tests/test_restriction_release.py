"""D103: a restriction can be lifted, and delivery can be stopped.

The half of a customer-impacting control that matters when it is wrong. Pinned:
a lift takes two people like the restriction did; it is a new order beside the
original, never an edit; it cannot be asked for before the bank has applied
anything, nor twice; the emergency switch holds messages in the outbox instead
of losing them; and reconciliation names what the bank owes an answer on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar import governance, restrictions
from riskradar.config import settings

from conftest import login


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


@pytest.fixture
def applied_restriction():
    """A case with an approved, delivered and applied restriction on it."""
    now = datetime.now(timezone.utc)
    with _db() as c:
        analyst = c.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]
        lead = c.execute("SELECT id FROM users WHERE email = 'lead@riskradar.local'").fetchone()["id"]
        case_id = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, assignee_id, assigned_at, outcome) "
            "VALUES (%s, 'UNDER_REVIEW', 'CRITICAL', %s, %s, %s, %s, now(), 'CONFIRMED_FRAUD') RETURNING id",
            (f"sub_rel_{uuid.uuid4().hex[:12]}", now, now, now + timedelta(hours=24), analyst),
        ).fetchone()["id"]
        submission_id = c.execute(
            "INSERT INTO fraud_submissions (case_id, proposed_outcome, rationale, case_version, "
            "submitted_by, state, decided_by, decided_at) "
            "VALUES (%s, 'CONFIRMED_FRAUD', 'the customer denies every one of these payments', 1, %s, "
            "'APPROVED', %s, now()) RETURNING id",
            (case_id, analyst, lead),
        ).fetchone()["id"]
        order = c.execute(
            "INSERT INTO restriction_orders (submission_id, case_id, action, account_token, reason, "
            "approved_by, first_delivered_at, delivery_count) "
            "VALUES (%s, %s, 'DEBIT_RESTRICTION', %s, 'drain in progress', %s, now(), 1) "
            "RETURNING id, restriction_ref",
            (submission_id, case_id, f"acct_{uuid.uuid4().hex[:10]}", lead),
        ).fetchone()
        c.commit()
    yield {"case_id": case_id, "order_id": order["id"], "ref": str(order["restriction_ref"]),
           "submission_id": submission_id}
    with psycopg.connect(settings().migrate_dsn) as c:
        c.execute("DELETE FROM config_change_requests WHERE target = %s", (str(order["restriction_ref"]),))
        c.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        c.commit()


def _bank_applied(ref: str) -> None:
    """The bank says it applied the restriction, through the real path."""
    with _db() as c:
        restrictions.acknowledge(c, restriction_ref=ref, outcome="APPLIED", reason=None, taken_at=None)
        c.commit()


def test_lifting_a_restriction_takes_two_people_and_leaves_the_original(client, applied_restriction):
    ref = applied_restriction["ref"]
    _bank_applied(ref)
    login(client, "analyst@riskradar.local", "Analyst#2026")
    asked = client.post(f"/v1/restrictions/{ref}/release-request",
                        json={"reason": "the customer produced the receipts; this was a legitimate purchase"})
    assert asked.status_code == 200, asked.text
    request_id = asked.json()["request"]["id"]

    # Asking does not lift it.
    with _db() as c:
        assert c.execute("SELECT count(*) AS n FROM restriction_orders WHERE releases_order_id = %s",
                         (applied_restriction["order_id"],)).fetchone()["n"] == 0

    # The asker cannot approve their own request, even as a lead would.
    with _db() as c:
        analyst = c.execute("SELECT id, role::text AS role FROM users WHERE email = 'analyst@riskradar.local'"
                            ).fetchone()
        actor = {"id": analyst["id"], "permissions": ["cases:approve_fraud"], "role": analyst["role"]}
        with pytest.raises(governance.MakerCheckerError) as refused:
            governance.decide(c, actor, request_id=request_id, action="APPROVE")
        assert "cannot approve" in refused.value.detail
        c.rollback()

    # A lead decides, and the lift is a new order naming the original.
    login(client, "lead@riskradar.local", "OpsLead#2026")
    queue = client.get("/v1/restrictions/release-requests").json()
    assert request_id in [r["id"] for r in queue["items"]]
    approved = client.post(f"/v1/restrictions/release-requests/{request_id}/decision",
                           json={"action": "APPROVE", "reason": "receipts check out"})
    assert approved.status_code == 200, approved.text

    with _db() as c:
        pair = c.execute(
            "SELECT kind, releases_order_id, reason FROM restriction_orders WHERE case_id = %s ORDER BY id",
            (applied_restriction["case_id"],)).fetchall()
        assert [p["kind"] for p in pair] == ["RESTRICT", "RELEASE"]
        assert pair[1]["releases_order_id"] == applied_restriction["order_id"]
        # The original is untouched, and the lift has its own outbox message.
        original = c.execute("SELECT ack_outcome, acknowledged_at FROM restriction_orders WHERE id = %s",
                             (applied_restriction["order_id"],)).fetchone()
        assert original["ack_outcome"] == "APPLIED"
        queued = c.execute(
            "SELECT b.payload FROM restriction_outbox b JOIN restriction_orders o ON o.id = b.order_id "
            "WHERE o.releases_order_id = %s", (applied_restriction["order_id"],)).fetchone()
        assert queued["payload"]["kind"] == "RELEASE" and queued["payload"]["lifts"] == ref

    # A second lift is refused: one live release per restriction.
    login(client, "analyst@riskradar.local", "Analyst#2026")
    again = client.post(f"/v1/restrictions/{ref}/release-request",
                        json={"reason": "asking a second time for the very same restriction"})
    assert again.status_code == 409 and "already exists" in again.text


def test_a_restriction_the_bank_never_applied_cannot_be_lifted(client, applied_restriction):
    # The fixture leaves it delivered and unanswered, which is exactly the case.
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.post(f"/v1/restrictions/{applied_restriction['ref']}/release-request",
                    json={"reason": "the bank has not told us anything about this one yet"})
    assert r.status_code == 409 and "has not said what it did" in r.text


def test_the_emergency_switch_holds_messages_rather_than_losing_them(client, applied_restriction):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    on = client.post("/v1/restrictions/delivery/pause",
                     json={"paused": True, "reason": "suspected mass restriction from a bad rule"})
    assert on.status_code == 200 and on.json()["paused"] is True

    with _db() as c:
        assert restrictions.paused(c)["paused"] is True
        # A queued message stays queued, and the sweep says so.
        c.execute(
            "INSERT INTO restriction_outbox (order_id, payload) VALUES (%s, '{}'::jsonb) "
            "ON CONFLICT (order_id) DO NOTHING", (applied_restriction["order_id"],))
        out = restrictions.dispatch(c)
        assert out["sent"] == 0 and out.get("paused") == 1
        assert c.execute("SELECT status FROM restriction_outbox WHERE order_id = %s",
                         (applied_restriction["order_id"],)).fetchone()["status"] == "PENDING"
        c.rollback()

    off = client.post("/v1/restrictions/delivery/pause",
                      json={"paused": False, "reason": "the rule was retuned and the queue checked"})
    assert off.status_code == 200 and off.json()["paused"] is False
    with _db() as c:
        assert restrictions.paused(c)["paused"] is False
        trail = c.execute(
            "SELECT action FROM audit_log WHERE object_id = 'restriction_delivery' ORDER BY id DESC LIMIT 2"
        ).fetchall()
        assert {a["action"] for a in trail} == {"RESTRICTION_DELIVERY_PAUSED", "RESTRICTION_DELIVERY_RESUMED"}


def test_reconciliation_names_what_the_bank_owes_an_answer_on(client, applied_restriction):
    with _db() as c:
        c.execute("UPDATE restriction_orders SET first_delivered_at = now() - interval '48 hours' WHERE id = %s",
                  (applied_restriction["order_id"],))
        c.commit()
    login(client, "lead@riskradar.local", "OpsLead#2026")
    report = client.get("/v1/metrics/restrictions/reconciliation")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["clean"] is False
    assert applied_restriction["ref"] in [r["restriction_ref"] for r in body["delivered_not_acknowledged"]]


def test_an_expired_temporary_restriction_is_listed_for_lifting(applied_restriction):
    _bank_applied(applied_restriction["ref"])
    with _db() as c:
        c.execute("UPDATE restriction_orders SET expires_at = now() - interval '1 hour' WHERE id = %s",
                  (applied_restriction["order_id"],))
        report = restrictions.reconcile(c)
        assert applied_restriction["order_id"] in [o["id"] for o in report["expired_still_standing"]]
        # Expiry proposes the lift; it does not lift anything by itself.
        assert c.execute("SELECT count(*) AS n FROM restriction_orders WHERE releases_order_id = %s",
                         (applied_restriction["order_id"],)).fetchone()["n"] == 0
        c.rollback()

"""D97: the restriction delivery pipeline — outbox, execution, reconciliation.

The create → dispatch → acknowledge lifecycle runs on the rolled-back `conn`
fixture, so the suite never commits an order to the demo database or moves the
live dispatch state. The HTTP tests cover routing, machine auth and visibility
without needing committed orders.

Risk Radar restricts nothing (D7): these tests assert what was *recorded and
dispatched*, never that any account was actually restricted.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from riskradar import restrictions
from riskradar.identity import adapters

from conftest import login

WANTED = [
    {"action": "DEBIT_RESTRICTION", "account_token": "acct-tok-1",
     "beneficiary_token": None, "channel": None, "reason": "drain in progress"},
    {"action": "BENEFICIARY_RESTRICTION", "account_token": None,
     "beneficiary_token": "ben-tok-1", "channel": None, "reason": "mule destination"},
]


def _seed_submission(conn, restrictions_list=WANTED) -> dict:
    """An APPROVED confirmed-fraud submission carrying restrictions, on a real
    case, so the foreign keys hold. Rolled back with the test."""
    case = conn.execute("SELECT id, version FROM cases ORDER BY id LIMIT 1").fetchone()
    analyst = conn.execute(
        "SELECT id FROM users WHERE email = 'analyst@riskradar.local'"
    ).fetchone()["id"]
    lead = conn.execute(
        "SELECT id FROM users WHERE email = 'lead@riskradar.local'"
    ).fetchone()["id"]
    row = conn.execute(
        """
        INSERT INTO fraud_submissions
            (case_id, proposed_outcome, rationale, restrictions, case_version,
             submitted_by, state, decided_by, decided_at)
        VALUES (%s, 'CONFIRMED_FRAUD', 'confirmed drain across the customer accounts',
                %s, %s, %s, 'APPROVED', %s, now())
        RETURNING id, case_id
        """,
        (case["id"], json.dumps(restrictions_list), case["version"], analyst, lead),
    ).fetchone()
    return {"id": row["id"], "case_id": row["case_id"], "restrictions": restrictions_list, "lead": lead}


# ---------------------------------------------------------------------------
# 1. Outbox: orders and messages written on approval
# ---------------------------------------------------------------------------


def test_approved_restrictions_become_orders_and_outbox_messages(conn):
    sub = _seed_submission(conn)
    orders = restrictions.create_orders(conn, sub, sub["lead"])
    assert len(orders) == 2

    pending = conn.execute(
        """
        SELECT count(*) AS n FROM restriction_outbox o
          JOIN restriction_orders r ON r.id = o.order_id
         WHERE r.submission_id = %s AND o.status = 'PENDING'
        """,
        (sub["id"],),
    ).fetchone()["n"]
    assert pending == 2
    # The order records the lead who approved it — the human authority (D7).
    approver = conn.execute(
        "SELECT DISTINCT approved_by FROM restriction_orders WHERE submission_id = %s", (sub["id"],)
    ).fetchall()
    assert [r["approved_by"] for r in approver] == [sub["lead"]]


def test_creating_orders_is_idempotent(conn):
    sub = _seed_submission(conn)
    assert len(restrictions.create_orders(conn, sub, sub["lead"])) == 2
    # A replayed approval does not double the orders.
    assert restrictions.create_orders(conn, sub, sub["lead"]) == []
    total = conn.execute(
        "SELECT count(*) AS n FROM restriction_orders WHERE submission_id = %s", (sub["id"],)
    ).fetchone()["n"]
    assert total == 2


# ---------------------------------------------------------------------------
# 2. Execution: the dispatch sweep
# ---------------------------------------------------------------------------


def test_dispatch_to_a_connector_sends_and_records_delivery(conn):
    sub = _seed_submission(conn)
    restrictions.create_orders(conn, sub, sub["lead"])
    # dispatch works over every due message, so assert this submission's outcome
    # rather than a global count.
    counts = restrictions.dispatch(conn, connector=adapters.LoopbackRestrictionConnector())
    assert counts["sent"] >= 2 and counts["failed"] == 0

    delivered = conn.execute(
        "SELECT count(*) AS n FROM restriction_orders WHERE submission_id = %s AND first_delivered_at IS NOT NULL",
        (sub["id"],),
    ).fetchone()["n"]
    assert delivered == 2
    sent = conn.execute(
        """SELECT count(*) AS n FROM restriction_outbox o JOIN restriction_orders r ON r.id = o.order_id
            WHERE r.submission_id = %s AND o.status = 'SENT' AND o.external_ref IS NOT NULL""",
        (sub["id"],),
    ).fetchone()["n"]
    assert sent == 2


def test_dispatch_without_a_connector_waits_visibly(conn):
    sub = _seed_submission(conn)
    restrictions.create_orders(conn, sub, sub["lead"])
    counts = restrictions.dispatch(conn, connector=adapters.NoRestrictionConnector())
    assert counts["sent"] == 0 and counts["waiting"] >= 2

    row = conn.execute(
        """SELECT o.status, o.last_error FROM restriction_outbox o
             JOIN restriction_orders r ON r.id = o.order_id
            WHERE r.submission_id = %s LIMIT 1""",
        (sub["id"],),
    ).fetchone()
    assert row["status"] == "PENDING"
    assert "no restriction connector configured" in (row["last_error"] or "")
    # Not delivered: an unsent recommendation has not reached the bank.
    undelivered = conn.execute(
        "SELECT count(*) AS n FROM restriction_orders WHERE submission_id = %s AND first_delivered_at IS NULL",
        (sub["id"],),
    ).fetchone()["n"]
    assert undelivered == 2


# ---------------------------------------------------------------------------
# 3. Reconciliation: the bank acknowledges
# ---------------------------------------------------------------------------


def test_acknowledgement_is_recorded_once(conn):
    sub = _seed_submission(conn)
    orders = restrictions.create_orders(conn, sub, sub["lead"])
    ref = str(orders[0]["restriction_ref"])

    out = restrictions.acknowledge(conn, restriction_ref=ref, outcome="APPLIED", reason=None, taken_at=None)
    assert out["status"] == "acknowledged"
    row = conn.execute(
        "SELECT ack_outcome::text AS o, acknowledged_at FROM restriction_orders WHERE restriction_ref = %s", (ref,)
    ).fetchone()
    assert row["o"] == "APPLIED" and row["acknowledged_at"] is not None

    # Same answer again is a no-op...
    assert restrictions.acknowledge(conn, restriction_ref=ref, outcome="APPLIED",
                                    reason=None, taken_at=None)["status"] == "duplicate"
    # ...a different answer is a conflict.
    with pytest.raises(restrictions.RestrictionError) as exc:
        restrictions.acknowledge(conn, restriction_ref=ref, outcome="REJECTED", reason="x", taken_at=None)
    assert exc.value.status == 409


def test_an_issued_order_cannot_be_rewritten(conn):
    sub = _seed_submission(conn)
    orders = restrictions.create_orders(conn, sub, sub["lead"])
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE restriction_orders SET action = 'CARD_FREEZE' WHERE id = %s", (orders[0]["id"],))
    conn.rollback()


def test_status_summarises_the_pipeline(conn):
    sub = _seed_submission(conn)
    restrictions.create_orders(conn, sub, sub["lead"])
    st = restrictions.status(conn)
    assert st["connector"] == adapters.restriction_connector().name
    assert st["lifecycle"]["total"] >= 2


# ---------------------------------------------------------------------------
# The HTTP surface — routing, machine auth, visibility (no committed orders)
# ---------------------------------------------------------------------------


def test_the_bank_feed_needs_an_api_key(client, api_headers):
    assert client.get("/v1/restrictions").status_code == 401
    assert client.get("/v1/restrictions", headers=api_headers).status_code == 200


def test_acking_an_unknown_ref_is_404(client, api_headers):
    r = client.post(
        f"/v1/restrictions/{uuid.uuid4()}/ack",
        headers=api_headers,
        json={"outcome": "APPLIED"},
    )
    assert r.status_code == 404


def test_staff_status_needs_a_case_reader(client):
    assert client.get("/v1/metrics/restrictions").status_code == 401
    login(client, "lead@riskradar.local", "OpsLead#2026")
    ok = client.get("/v1/metrics/restrictions")
    assert ok.status_code == 200
    assert "lifecycle" in ok.json()
    # An administrator holds no case permission, so cannot read the desk's view.
    login(client, "admin@riskradar.local", "Admin#2026")
    assert client.get("/v1/metrics/restrictions").status_code == 403


def test_case_restrictions_are_scoped_like_every_case_read(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    # A case the analyst does not hold reads as 404, not 403 (D94).
    r = client.get("/v1/cases/999999999/restrictions")
    assert r.status_code == 404

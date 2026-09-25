"""D106: confirmed fraud tells the customer's account manager.

Everything runs on the rolled-back `conn` fixture, so no report is ever
committed to the demo database and the live dispatch state is untouched.

Risk Radar reports; it does not act on the customer (D7). These tests assert
what was *recorded and dispatched*, never that anybody was contacted.
"""

from __future__ import annotations

import json

import pytest

from riskradar import reporting
from riskradar.identity import adapters

from conftest import login

RESTRICTIONS = [
    {"action": "DEBIT_RESTRICTION", "account_token": "acct-tok-1",
     "beneficiary_token": None, "channel": None, "reason": "drain in progress"},
]


def _submission(conn, outcome="CONFIRMED_FRAUD"):
    case = conn.execute(
        "SELECT id, version, subject_token FROM cases ORDER BY id LIMIT 1").fetchone()
    analyst = conn.execute(
        "SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]
    lead = conn.execute(
        "SELECT id FROM users WHERE email = 'lead@riskradar.local'").fetchone()["id"]
    row = conn.execute(
        """
        INSERT INTO fraud_submissions
            (case_id, proposed_outcome, rationale, restrictions, case_version,
             submitted_by, state, decided_by, decided_at)
        VALUES (%s, %s, 'confirmed drain across the customer accounts',
                %s, %s, %s, 'APPROVED', %s, now())
        RETURNING id, case_id, proposed_outcome, rationale, restrictions
        """,
        (case["id"], outcome, json.dumps(RESTRICTIONS), case["version"], analyst, lead),
    ).fetchone()
    return dict(row), dict(case), lead


def test_approving_confirmed_fraud_raises_one_report(conn):
    submission, case, lead = _submission(conn)
    report = reporting.create_report(conn, submission, case, lead, orders=[])
    assert report is not None

    rows = conn.execute(
        "SELECT status, subject_token, payload FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["subject_token"] == case["subject_token"]

    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["outcome"] == "CONFIRMED_FRAUD"
    assert payload["case_id"] == case["id"]
    # The handle the recipient will quote back is in the message itself.
    assert payload["report_ref"]
    # D7 is stated in the message, not just in our documentation.
    assert "recommends and records" in payload["advisory"]


def test_a_replayed_approval_does_not_report_twice(conn):
    """The unique constraint is the guarantee, not remembering to check."""
    submission, case, lead = _submission(conn)
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is not None
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is None
    n = conn.execute(
        "SELECT count(*) AS n FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()["n"]
    assert n == 1


@pytest.mark.parametrize("outcome", ["FALSE_POSITIVE", "INCONCLUSIVE"])
def test_only_confirmed_fraud_is_reported(conn, outcome):
    """Reporting the others would train the account manager to ignore it."""
    submission, case, lead = _submission(conn, outcome=outcome)
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is None
    n = conn.execute(
        "SELECT count(*) AS n FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()["n"]
    assert n == 0


def test_dispatch_sends_through_the_connector(conn):
    submission, case, lead = _submission(conn)
    reporting.create_report(conn, submission, case, lead, orders=[])
    counts = reporting.dispatch(conn, connector=adapters.LoopbackAccountManagerConnector())
    assert counts["sent"] >= 1

    row = conn.execute(
        "SELECT status, sent_at, external_ref FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()
    assert row["status"] == "SENT"
    assert row["sent_at"] is not None
    assert row["external_ref"].startswith("loopback-am-")


def test_an_unconfigured_connector_waits_rather_than_failing(conn):
    """Nobody has wired a relationship system yet is not a delivery failure —
    the report must still be there to send once somebody does."""
    submission, case, lead = _submission(conn)
    reporting.create_report(conn, submission, case, lead, orders=[])
    counts = reporting.dispatch(conn, connector=adapters.NoAccountManagerConnector())
    assert counts["waiting"] >= 1
    assert counts["failed"] == 0

    row = conn.execute(
        "SELECT status, attempts, last_error FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()
    assert row["status"] == "PENDING"
    assert "no account manager connector configured" in row["last_error"]


def test_the_report_carries_the_restrictions_that_went_to_the_bank(conn):
    submission, case, lead = _submission(conn)
    orders = [{"action": "DEBIT_RESTRICTION", "account_token": "acct-tok-1",
               "beneficiary_token": None, "restriction_ref": "11111111-1111-1111-1111-111111111111"}]
    reporting.create_report(conn, submission, case, lead, orders=orders)
    payload = conn.execute(
        "SELECT payload FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["restrictions_recommended"][0]["action"] == "DEBIT_RESTRICTION"
    assert payload["restrictions_recommended"][0]["restriction_ref"].startswith("1111")


# ---------------------------------------------------------------------------
# What to block, proposed from the evidence
# ---------------------------------------------------------------------------


def test_suggestions_come_from_the_case_s_own_transactions(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    worklist = client.get("/v1/worklist").json()
    if not worklist.get("items"):
        pytest.skip("no case visible to the analyst on this database")
    case_id = worklist["items"][0]["id"]

    r = client.get(f"/v1/cases/{case_id}/restriction-suggestions")
    assert r.status_code == 200, r.text
    body = r.json()
    for item in body["items"]:
        assert item["action"] in {"DEBIT_RESTRICTION", "CARD_FREEZE",
                                  "BENEFICIARY_RESTRICTION", "TRANSACTION_REVERSAL"}
        # A restriction reaches a real customer, so each one says why it is here.
        assert item["why"]
        assert item["account_token"] or item["beneficiary_token"]
        # D107: a reversal that does not name its payment is unexecutable, and
        # the database refuses it — so it must never be suggested either.
        if item["action"] == "TRANSACTION_REVERSAL":
            assert item["transaction_ref"]

    reversals = [i for i in body["items"] if i["action"] == "TRANSACTION_REVERSAL"]
    # Capped, so reversals cannot crowd out the account-level actions.
    assert len(reversals) <= 8
    # D107: the whole list is capped at what a decision actually accepts. Longer
    # and a lead who checks everything gets a 422 at the moment they approve.
    assert len(body["items"]) <= 20, "a decision takes at most twenty restrictions"
    assert "omitted" in body
    # Truncation drops the least urgent first, so account-level actions survive.
    if body["omitted"]:
        assert any(i["action"] == "DEBIT_RESTRICTION" for i in body["items"])


def test_suggestions_are_scoped_like_every_other_case_read(client):
    """Outside the caller's scope is a 404, not a 403 (D94)."""
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.get("/v1/cases/99999999/restriction-suggestions")
    assert r.status_code == 404

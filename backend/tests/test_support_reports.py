"""D109: the fraud desk tells the support team (was D106's account manager).

Everything runs on the rolled-back `conn` fixture, so no message is ever
committed to the demo database and the live dispatch state is untouched.

Risk Radar recommends; support acts on the customer (D7, D109). These tests
assert what was *recorded and dispatched*, never that anybody was contacted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from riskradar import reporting
from riskradar.identity import adapters

from conftest import login

RESTRICTIONS = [
    {"action": "DEBIT_RESTRICTION", "account_token": "acct-tok-1",
     "beneficiary_token": None, "channel": None, "reason": "drain in progress"},
]


def _case(conn, *, reported=False, risk_level=None):
    case = dict(conn.execute(
        "SELECT id, version, subject_token, risk_level::text AS risk_level, state::text AS state, "
        "support_ticket_ref, first_reported_at FROM cases WHERE state <> 'CLOSED' ORDER BY id LIMIT 1").fetchone())
    if reported:
        case["first_reported_at"] = datetime.now(timezone.utc)
    if risk_level:
        case["risk_level"] = risk_level
    return case


def _submission(conn, outcome="CONFIRMED_FRAUD", *, reported=False):
    case = _case(conn, reported=reported)
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
    return dict(row), case, lead


def _payload(conn, submission_id):
    payload = conn.execute("SELECT payload FROM support_reports WHERE submission_id = %s",
                           (submission_id,)).fetchone()["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def _count(conn, submission_id):
    return conn.execute("SELECT count(*) AS n FROM support_reports WHERE submission_id = %s",
                        (submission_id,)).fetchone()["n"]


def test_approving_confirmed_fraud_raises_one_report(conn):
    submission, case, lead = _submission(conn)
    report = reporting.create_report(conn, submission, case, lead, orders=[])
    assert report is not None

    row = conn.execute(
        "SELECT kind, status, subject_token FROM support_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()
    assert (row["kind"], row["status"]) == ("REPORT", "PENDING")
    assert row["subject_token"] == case["subject_token"]

    payload = _payload(conn, submission["id"])
    assert payload["outcome"] == "CONFIRMED_FRAUD"
    assert payload["case_id"] == case["id"]
    # The handle the recipient will quote back is in the message itself.
    assert payload["report_ref"]
    # D7 and D109 are stated in the message, not just in our documentation.
    assert "support decides" in payload["advisory"]


def test_a_replayed_approval_does_not_report_twice(conn):
    """The unique constraint is the guarantee, not remembering to check."""
    submission, case, lead = _submission(conn)
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is not None
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is None
    assert _count(conn, submission["id"]) == 1


@pytest.mark.parametrize("outcome", ["FALSE_POSITIVE", "INCONCLUSIVE"])
def test_a_detector_case_cleared_by_the_desk_is_not_reported(conn, outcome):
    """D109c: support only hears about real issues."""
    submission, case, lead = _submission(conn, outcome=outcome)
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is None
    assert _count(conn, submission["id"]) == 0


@pytest.mark.parametrize("outcome", ["FALSE_POSITIVE", "INCONCLUSIVE", "CONFIRMED_FRAUD"])
def test_a_case_support_reported_is_always_answered(conn, outcome):
    """D109c: support asked, so support gets an answer whatever it is."""
    submission, case, lead = _submission(conn, outcome=outcome, reported=True)
    assert reporting.create_report(conn, submission, case, lead, orders=[]) is not None
    payload = _payload(conn, submission["id"])
    assert payload["outcome"] == outcome
    assert payload["reported_by_support"] is True


def test_dispatch_sends_through_the_connector(conn):
    submission, case, lead = _submission(conn)
    reporting.create_report(conn, submission, case, lead, orders=[])
    counts = reporting.dispatch(conn, connector=adapters.LoopbackSupportConnector())
    assert counts["sent"] >= 1

    row = conn.execute(
        "SELECT status, sent_at, external_ref FROM support_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()
    assert row["status"] == "SENT"
    assert row["sent_at"] is not None
    assert row["external_ref"].startswith("loopback-support-")


def test_an_unconfigured_connector_waits_rather_than_failing(conn):
    """Nobody has wired support's system yet is not a delivery failure — the
    report must still be there to send once somebody does."""
    submission, case, lead = _submission(conn)
    reporting.create_report(conn, submission, case, lead, orders=[])
    counts = reporting.dispatch(conn, connector=adapters.NoSupportConnector())
    assert counts["waiting"] >= 1
    assert counts["failed"] == 0

    row = conn.execute(
        "SELECT status, last_error FROM support_reports WHERE submission_id = %s",
        (submission["id"],),
    ).fetchone()
    assert row["status"] == "PENDING"
    assert "no support-team connector configured" in row["last_error"]


def test_the_report_carries_the_actions_recommended_to_support(conn):
    submission, case, lead = _submission(conn)
    orders = [{"action": "DEBIT_RESTRICTION", "account_token": "acct-tok-1",
               "beneficiary_token": None, "restriction_ref": "11111111-1111-1111-1111-111111111111"}]
    reporting.create_report(conn, submission, case, lead, orders=orders)
    asked = _payload(conn, submission["id"])["actions_recommended"]
    assert asked[0]["action"] == "DEBIT_RESTRICTION"
    assert asked[0]["restriction_ref"].startswith("1111")


def test_the_emailed_report_is_plain_english():
    """What support reads names the action in words, never the code."""
    text = adapters._report_text({
        "kind": "REPORT", "case_id": 7, "outcome": "CONFIRMED_FRAUD", "subject_token": "tok",
        "support_ticket_ref": "SUP-123", "exposure_minor": 150000, "transactions": 2,
        "actions_recommended": [{"action": "NOTIFY_RECEIVING_BANK", "restriction_ref": "abcdef12-0000"}],
        "advisory": reporting.ADVISORY,
    })
    assert "Fraud confirmed" in text
    assert "Ask the receiving bank to hold or return the money" in text
    assert "SUP-123" in text
    assert "NOTIFY_RECEIVING_BANK" not in text and "CONFIRMED_FRAUD" not in text


# ---------------------------------------------------------------------------
# D109d: the urgent heads-up
# ---------------------------------------------------------------------------


def test_a_heads_up_is_only_for_critical_cases(conn):
    case = _case(conn, risk_level="HIGH")
    with pytest.raises(reporting.SupportMessageError) as err:
        reporting.create_heads_up(conn, case, sent_by={"display_name": "A"}, message="money still leaving now")
    assert err.value.status == 409


def test_a_heads_up_goes_once_per_case(conn):
    row = conn.execute(
        "SELECT id, subject_token, risk_level::text AS risk_level, state::text AS state, support_ticket_ref "
        "FROM cases c WHERE state <> 'CLOSED' AND NOT EXISTS (SELECT 1 FROM support_reports s "
        "WHERE s.case_id = c.id AND s.kind = 'HEADS_UP') ORDER BY id LIMIT 1").fetchone()
    case = {**dict(row), "risk_level": "CRITICAL"}
    sent = reporting.create_heads_up(conn, case, sent_by={"display_name": "A"},
                                     message="money still leaving, please hold transfers")
    assert sent["kind"] == "HEADS_UP" and sent["status"] == "PENDING"
    with pytest.raises(reporting.SupportMessageError):
        reporting.create_heads_up(conn, case, sent_by={"display_name": "A"},
                                  message="a second heads-up is refused")


def test_a_contact_request_needs_no_finding(conn):
    """D109f: the watch flag's contact request is not tied to a submission."""
    case = _case(conn)
    sent = reporting.create_contact_request(conn, case, contact_by=datetime.now(timezone.utc) + timedelta(hours=24),
                                            reason="suspected takeover, confirm with the customer")
    assert sent["kind"] == "CONTACT_REQUEST"


# ---------------------------------------------------------------------------
# What to ask of support, proposed from the evidence
# ---------------------------------------------------------------------------

SUPPORT_ACTIONS = {"DEBIT_RESTRICTION", "CARD_FREEZE", "BENEFICIARY_RESTRICTION", "TRANSACTION_REVERSAL",
                   "SESSION_TERMINATION", "CREDENTIAL_RESET", "MFA_REENROLMENT",
                   "CONTACT_CUSTOMER", "VERIFY_IDENTITY", "NOTIFY_RECEIVING_BANK"}


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
        assert item["action"] in SUPPORT_ACTIONS
        # An action reaches a real customer, so each one says why it is here.
        assert item["why"]
        assert item["account_token"] or item["beneficiary_token"]
        # D107: a reversal that does not name its payment is unexecutable, and
        # the database refuses it — so it must never be suggested either.
        if item["action"] == "TRANSACTION_REVERSAL":
            assert item["transaction_ref"]
    # D109b: support is always asked to reach the customer.
    assert any(i["action"] == "CONTACT_CUSTOMER" for i in body["items"])

    reversals = [i for i in body["items"] if i["action"] == "TRANSACTION_REVERSAL"]
    # Capped, so reversals cannot crowd out the account-level actions.
    assert len(reversals) <= 8
    assert len(body["items"]) <= 20, "a decision takes at most twenty actions"
    assert "omitted" in body
    # Truncation drops the least urgent first, so account-level actions survive.
    if body["omitted"]:
        assert any(i["action"] == "DEBIT_RESTRICTION" for i in body["items"])


def test_suggestions_are_scoped_like_every_other_case_read(client):
    """Outside the caller's scope is a 404, not a 403 (D94)."""
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.get("/v1/cases/99999999/restriction-suggestions")
    assert r.status_code == 404


def test_the_heads_up_route_refuses_a_case_that_is_not_critical(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    items = client.get("/v1/worklist?scope=all&limit=200").json().get("items", [])
    calm = next((i for i in items if i.get("risk_level") != "CRITICAL"), None)
    if not calm:
        pytest.skip("no non-critical open case on this database")
    r = client.post(f"/v1/cases/{calm['id']}/heads-up", json={"message": "money may still be leaving now"})
    assert r.status_code == 409 and "Critical" in r.text
    assert client.post(f"/v1/cases/{calm['id']}/heads-up", json={"message": "too short"}).status_code == 422

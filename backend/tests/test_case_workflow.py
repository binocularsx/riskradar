"""D83: a case followed from alert to resolution, through escalation and back.

Pinned end to end through the API, as each role: an analyst takes a case and
records a step; an escalation needs a reason and leaves the analyst's queue for
the receiving team's; the team takes it, cannot be the one to hand back its own
escalation, hands it back with findings; the analyst records the outcome and
the case moves to "awaiting close"; a lead closes it. The stage the pipeline
reports follows every move, and the intake view answers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from riskradar.config import settings

from conftest import login

PASSWORDS = {
    "analyst@riskradar.local": "Analyst#2026",
    "infosec@riskradar.local": "InfoSec#2026",
    "lead@riskradar.local": "OpsLead#2026",
}


def as_user(email: str) -> TestClient:
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    login(c, email, PASSWORDS[email])
    return c


@pytest.fixture
def case_id():
    subject = f"sub_wf_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        row = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, assignee_id, assigned_at) "
            "VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s, "
            "(SELECT id FROM users WHERE email = 'analyst@riskradar.local'), now()) RETURNING id",
            (subject, now, now, now + timedelta(hours=24)),
        ).fetchone()
        c.commit()
    yield row["id"]
    with psycopg.connect(settings().migrate_dsn if hasattr(settings(), "migrate_dsn") else settings().app_dsn) as c:
        try:
            c.execute("DELETE FROM case_notes WHERE case_id = %s", (row["id"],))
            c.execute("DELETE FROM cases WHERE id = %s", (row["id"],))
            c.commit()
        except psycopg.Error:
            c.rollback()


def stage(client: TestClient, cid: int) -> str:
    r = client.get(f"/v1/cases/{cid}/workflow")
    assert r.status_code == 200, r.text
    return r.json()["stage"]


def test_a_case_goes_from_alert_through_escalation_to_closed(case_id):
    analyst, infosec, lead = (as_user(e) for e in PASSWORDS)
    me = analyst.get("/v1/auth/me").json()["id"]

    # D94: the case was routed to this analyst, so it is already theirs.
    assert stage(analyst, case_id) == "IN_REVIEW"
    assert analyst.post(f"/v1/cases/{case_id}/review").status_code == 200
    assert stage(analyst, case_id) == "IN_REVIEW"

    # A step, recorded with its result, marks the recommended step done.
    catalog = analyst.get("/v1/workflow/catalog").json()
    # D109: investigation steps only; contacting the customer is support's.
    assert "DESTINATIONS_CHECKED" in catalog["actions"]
    assert "CUSTOMER_CONTACTED" not in catalog["actions"]
    assert "CUSTOMER_CONTACTED" in catalog["retired_actions"]
    # D108: InfoSec is retired at the owner's direction. It is no longer offered
    # as a destination, but it is still labelled, because cases escalated there
    # before the change still name it and their history has to keep rendering —
    # which the rest of this test goes on to exercise.
    assert "FRAUD_OPS" in catalog["escalation"]
    assert "INFOSEC" not in catalog["escalation"]
    assert "INFOSEC" in catalog["retired_escalation"]
    retired = analyst.post(f"/v1/cases/{case_id}/actions",
                           json={"action_code": "CUSTOMER_CONTACTED", "result": "NOT_REACHED"})
    assert retired.status_code == 410 and "support" in retired.text
    bad = analyst.post(f"/v1/cases/{case_id}/actions", json={"action_code": "DESTINATIONS_CHECKED", "result": "MAYBE"})
    assert bad.status_code == 422
    ok = analyst.post(f"/v1/cases/{case_id}/actions",
                      json={"action_code": "DESTINATIONS_CHECKED", "result": "NOTHING_FOUND"})
    assert ok.status_code == 200, ok.text
    assert any(a["action_code"] == "DESTINATIONS_CHECKED" for a in ok.json()["workflow"]["actions"])
    # Somebody not working the case cannot record steps on it.
    assert (infosec.post(f"/v1/cases/{case_id}/actions",
                        json={"action_code": "TIMELINE_REVIEWED", "result": "UNUSUAL"}).status_code == 404,
            "D94: outside their scope InfoSec is not told the case exists")

    # D108: InfoSec is retired, so it can no longer be escalated to at all.
    refused = analyst.post(f"/v1/cases/{case_id}/escalate",
                           json={"target": "INFOSEC", "note": "a SIM change is recent"})
    assert refused.status_code == 400
    assert "retired" in refused.json()["detail"]

    # Escalation needs a reason, then leaves the analyst's queue for the lead's.
    assert analyst.post(f"/v1/cases/{case_id}/escalate", json={"target": "FRAUD_OPS"}).status_code == 400
    r = analyst.post(f"/v1/cases/{case_id}/escalate",
                     json={"target": "FRAUD_OPS", "note": "Customer unreachable while a SIM change is recent"})
    assert r.status_code == 200, r.text
    assert stage(analyst, case_id) == "ESCALATED"
    mine = analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]
    assert case_id not in [c["id"] for c in mine]
    sent = analyst.get("/v1/worklist", params={"scope": "escalated", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in sent]
    theirs = lead.get("/v1/worklist", params={"scope": "escalated", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in theirs]
    # D94 still holds for the retired role: it was never escalated to InfoSec,
    # so InfoSec is not told the case exists.
    assert infosec.get(f"/v1/cases/{case_id}").status_code == 404
    wf = analyst.get(f"/v1/cases/{case_id}/workflow").json()
    assert wf["escalation"]["by_id"] == me and "unreachable" in wf["escalation"]["reason"]

    # The lead takes it, and it becomes theirs; the analyst cannot hand back their own escalation.
    assert lead.post(f"/v1/cases/{case_id}/review").status_code == 200
    assert analyst.post(f"/v1/cases/{case_id}/return", json={"findings": "nothing"}).status_code == 400
    back = lead.post(f"/v1/cases/{case_id}/return", json={"findings": "SIM swap confirmed with the carrier"})
    assert back.status_code == 200, back.text
    # A lead still sees the case after handing it back, so they get the workflow
    # itself rather than the confirmation stub a role that loses sight gets (D94).
    assert back.json()["assignee_id"] == me
    assert stage(analyst, case_id) == "IN_REVIEW"
    # D94: InfoSec never saw it and still does not.
    assert infosec.get(f"/v1/cases/{case_id}").status_code == 404
    mine = analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in mine], "handed back to the analyst who escalated"

    # D93: the analyst proposes the outcome; a lead who did not write it decides.
    r = analyst.post(f"/v1/cases/{case_id}/disposition",
                     json={"outcome": "CONFIRMED_FRAUD", "close": False, "followed_recommendation": True,
                           "note": "SIM swap confirmed with the carrier and the customer denies the payments"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] is None and r.json()["proposed_outcome"] == "CONFIRMED_FRAUD"
    assert stage(analyst, case_id) == "AWAITING_APPROVAL"
    assert case_id not in [c["id"] for c in analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]]
    proposed = lead.get("/v1/worklist", params={"scope": "awaiting_approval", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in proposed]

    # A lead cannot close it before deciding, and deciding is what records the outcome.
    too_early = lead.post(f"/v1/cases/{case_id}/close")
    assert too_early.status_code == 409 and "waiting for a decision" in too_early.text
    sid = r.json()["submission"]["id"]
    assert lead.post(f"/v1/submissions/{sid}/decision",
                     json={"decision": "APPROVE", "reason": "carrier evidence and customer denial both hold"}
                     ).status_code == 200
    assert stage(analyst, case_id) == "AWAITING_CLOSE"
    waiting = lead.get("/v1/worklist", params={"scope": "awaiting_close", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in waiting]

    assert lead.post(f"/v1/cases/{case_id}/close").status_code == 200
    assert stage(lead, case_id) == "CLOSED"
    lifecycle = [m["key"] for m in lead.get(f"/v1/cases/{case_id}/workflow").json()["lifecycle"]]
    for key in ("CASE_REVIEW_STARTED", "CASE_ACTION_RECORDED", "CASE_ESCALATED", "CASE_RETURNED",
                "FRAUD_SUBMITTED", "FRAUD_APPROVED", "CASE_CLOSED"):
        assert key in lifecycle, key

    pipe = analyst.get("/v1/workflow/pipeline").json()
    closed = next(s for s in pipe["stages"] if s["key"] == "CLOSED")
    assert case_id in [c["id"] for c in closed["items"]] or closed["count"] >= 1


def test_intake_answers_for_an_analyst():
    analyst = as_user("analyst@riskradar.local")
    r = analyst.get("/v1/metrics/intake", params={"minutes": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["series"]) == 30
    for key in ("queue", "expected_next_hour", "budget", "pipeline", "recent", "by_risk_level"):
        assert key in body


def test_links_show_another_customer_paying_the_same_destination(case_id):
    """D84: a destination shared with another customer, and that customer's confirmed fraud, are visible."""
    now = datetime.now(timezone.utc)
    ben = f"ben_wf_{uuid.uuid4().hex[:12]}"
    other = f"sub_wf_other_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        subject = c.execute("SELECT subject_token FROM cases WHERE id = %s", (case_id,)).fetchone()["subject_token"]
        for who, minutes in ((subject, 30), (other, 20)):
            c.execute(
                "INSERT INTO transactions (transaction_ref, occurred_at, amount_minor, currency, channel, instrument, rail, "
                "subject_token, account_token, beneficiary_token, auth_result, display_name) "
                "VALUES (%s, %s, 5000000, 'NGN', 'MOBILE_APP', 'ACCOUNT_TRANSFER', 'NIP', %s, %s, %s, 'APPROVED', %s)",
                (uuid.uuid4().hex, now - timedelta(minutes=minutes), who, f"acc_{who}", ben, "Linked Person"))
        oc = c.execute("INSERT INTO cases (subject_token, state, outcome, risk_level, correlation_expires_at, closed_at) "
                       "VALUES (%s, 'CLOSED', 'CONFIRMED_FRAUD', 'HIGH', %s, now()) RETURNING id",
                       (other, now + timedelta(hours=24))).fetchone()["id"]
        c.commit()
    try:
        analyst = as_user("analyst@riskradar.local")
        body = analyst.get(f"/v1/cases/{case_id}/links").json()
        dest = next(n for n in body["nodes"] if n["kind"] == "destination" and n["token"] == ben)
        assert dest["other_customers"] == 1 and dest["other_customers_24h"] == 1
        linked = [n for n in body["nodes"] if n["kind"] == "other_customer"]
        assert any(n["case_id"] == oc and n["case_outcome"] == "CONFIRMED_FRAUD" and n["risky"] for n in linked)
        assert body["summary"]["linked_confirmed_fraud"] == 1
    finally:
        with psycopg.connect(settings().app_dsn) as c:
            try:
                c.execute("DELETE FROM transactions WHERE beneficiary_token = %s", (ben,))
                c.execute("DELETE FROM cases WHERE id = %s", (oc,))
                c.commit()
            except psycopg.Error:
                c.rollback()

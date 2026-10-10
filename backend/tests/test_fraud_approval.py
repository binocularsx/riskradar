"""D93: an analyst proposes fraud, a different lead decides it.

The plan's mandatory maker-checker suite: the submitter can never approve their
own proposal through any route, the outcome is not written until a lead
approves, a case cannot close before that, a stale proposal is refused, and a
second proposal supersedes the first rather than queueing twice.
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
    "lead@riskradar.local": "OpsLead#2026",
    "infosec@riskradar.local": "InfoSec#2026",
}
RATIONALE = "Customer confirmed by phone that they did not make these payments; device is new."


def as_user(email: str) -> TestClient:
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    login(c, email, PASSWORDS[email])
    return c


@pytest.fixture
def case_id():
    """A case with one alert on it, owned by nobody."""
    subject = f"sub_ap_{uuid.uuid4().hex[:14]}"
    now = datetime.now(timezone.utc)
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        row = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, assignee_id, assigned_at) VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s, "
            "(SELECT id FROM users WHERE email = 'analyst@riskradar.local'), now()) RETURNING id",
            (subject, now, now, now + timedelta(hours=24)),
        ).fetchone()
        c.commit()
    yield row["id"]
    with psycopg.connect(settings().migrate_dsn) as c:
        for table in ("fraud_submissions", "case_actions", "case_notes", "clock_breaches", "alerts"):
            c.execute(f"DELETE FROM {table} WHERE case_id = %s", (row["id"],))
        c.execute("DELETE FROM cases WHERE id = %s", (row["id"],))
        c.commit()


def _outcome(case_id: int):
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        return c.execute("SELECT outcome, state::text AS state, version FROM cases WHERE id = %s",
                         (case_id,)).fetchone()


def test_an_analyst_proposes_and_a_lead_decides(case_id):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    assert analyst.post(f"/v1/cases/{case_id}/review").status_code == 200

    r = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "CONFIRMED_FRAUD", "rationale": RATIONALE,
        "restrictions": [{"action": "DEBIT_RESTRICTION", "account_token": "acct-x", "reason": "drain in progress"}],
    })
    assert r.status_code == 200, r.text
    submission_id = r.json()["submission"]["id"]
    # Proposed is not decided: the outcome is still empty.
    assert _outcome(case_id)["outcome"] is None

    # The analyst cannot approve it, and neither can they close the case.
    assert analyst.post(f"/v1/submissions/{submission_id}/decision",
                        json={"decision": "APPROVE", "reason": "I am sure about this"}).status_code == 403
    assert analyst.post(f"/v1/cases/{case_id}/close").status_code == 403

    # It is in the lead's queue, and the lead decides.
    queue = lead.get("/v1/approvals").json()
    assert any(i["id"] == submission_id for i in queue["items"])
    ok = lead.post(f"/v1/submissions/{submission_id}/decision",
                   json={"decision": "APPROVE", "reason": "Evidence and customer contact both support it"})
    assert ok.status_code == 200, ok.text
    assert _outcome(case_id)["outcome"] == "CONFIRMED_FRAUD"

    # Deciding twice is refused, and the case may now close.
    again = lead.post(f"/v1/submissions/{submission_id}/decision",
                      json={"decision": "REJECT", "reason": "changed my mind about this one"})
    assert again.status_code == 409
    assert lead.post(f"/v1/cases/{case_id}/close").status_code == 200
    assert _outcome(case_id)["state"] == "CLOSED"


def test_a_lead_cannot_approve_their_own_proposal(case_id):
    lead, other = as_user("lead@riskradar.local"), as_user("analyst@riskradar.local")
    r = lead.post(f"/v1/cases/{case_id}/submissions",
                  json={"proposed_outcome": "FALSE_POSITIVE", "rationale": RATIONALE})
    assert r.status_code == 200, r.text
    sid = r.json()["submission"]["id"]
    # Holding both permissions changes nothing: the maker is never the checker.
    denied = lead.post(f"/v1/submissions/{sid}/decision", json={"decision": "APPROVE", "reason": "my own work"})
    assert denied.status_code == 403 and "cannot approve" in denied.text
    # And the database refuses it even if the check above were bypassed.
    with psycopg.connect(settings().app_dsn) as c:
        me = c.execute("SELECT id FROM users WHERE email = 'lead@riskradar.local'").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE fraud_submissions SET decided_by = %s, decided_at = now() WHERE id = %s", (me, sid))
    # It is not in their own queue either.
    assert all(i["id"] != sid for i in lead.get("/v1/approvals").json()["items"])
    assert other.get("/v1/auth/me").status_code == 200


def test_the_old_one_key_verdict_now_proposes(case_id):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    r = analyst.post(f"/v1/cases/{case_id}/disposition", json={
        "outcome": "CONFIRMED_FRAUD", "note": RATIONALE, "close": True, "followed_recommendation": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] is None and body["proposed_outcome"] == "CONFIRMED_FRAUD"
    assert body["closed"] is False and body["blockers"]
    assert _outcome(case_id)["outcome"] is None
    # A lead approving from the queue is what makes it real.
    sid = body["submission"]["id"]
    assert lead.post(f"/v1/submissions/{sid}/decision",
                     json={"decision": "APPROVE", "reason": "checked the timeline and the destinations"}
                     ).status_code == 200
    assert _outcome(case_id)["outcome"] == "CONFIRMED_FRAUD"


def test_a_stale_proposal_is_refused_and_a_new_one_supersedes(case_id):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    version = _outcome(case_id)["version"]
    stale = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "FALSE_POSITIVE", "rationale": RATIONALE, "expected_case_version": version - 1})
    assert stale.status_code == 409 and "has moved on" in stale.text

    first = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "FALSE_POSITIVE", "rationale": RATIONALE, "expected_case_version": version}).json()
    second = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "INCONCLUSIVE", "rationale": "Reviewed again: the destination is a known merchant."}).json()
    history = analyst.get(f"/v1/cases/{case_id}/submissions").json()
    states = {i["id"]: i["state"] for i in history["items"]}
    assert states[first["submission"]["id"]] == "SUPERSEDED"
    assert states[second["submission"]["id"]] == "PENDING"
    assert len([i for i in history["items"] if i["state"] == "PENDING"]) == 1
    # A returned proposal comes back to its author with the reason.
    back = lead.post(f"/v1/submissions/{second['submission']['id']}/decision",
                     json={"decision": "RETURN", "reason": "check whether the customer was contacted first"})
    assert back.status_code == 200 and back.json()["submission"]["state"] == "RETURNED"
    assert _outcome(case_id)["outcome"] is None


def test_restrictions_must_be_from_the_catalogue(case_id):
    analyst = as_user("analyst@riskradar.local")
    bad = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "CONFIRMED_FRAUD", "rationale": RATIONALE,
        "restrictions": [{"action": "BLOCK_EVERYTHING", "account_token": "acct-x"}]})
    assert bad.status_code == 422
    unscoped = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "CONFIRMED_FRAUD", "rationale": RATIONALE,
        "restrictions": [{"action": "CARD_FREEZE"}]})
    assert unscoped.status_code == 422 and "must name the account" in unscoped.text
    with_false_positive = analyst.post(f"/v1/cases/{case_id}/submissions", json={
        "proposed_outcome": "FALSE_POSITIVE", "rationale": RATIONALE,
        "restrictions": [{"action": "CARD_FREEZE", "account_token": "acct-x"}]})
    assert with_false_positive.status_code == 422

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
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, correlation_expires_at) "
            "VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s) RETURNING id",
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

    assert stage(analyst, case_id) == "NEW"
    assert analyst.post(f"/v1/cases/{case_id}/review").status_code == 200
    assert stage(analyst, case_id) == "IN_REVIEW"

    # A step, recorded with its result, marks the recommended step done.
    catalog = analyst.get("/v1/workflow/catalog").json()
    assert "CUSTOMER_CONTACTED" in catalog["actions"] and "INFOSEC" in catalog["escalation"]
    bad = analyst.post(f"/v1/cases/{case_id}/actions", json={"action_code": "CUSTOMER_CONTACTED", "result": "MAYBE"})
    assert bad.status_code == 422
    ok = analyst.post(f"/v1/cases/{case_id}/actions", json={"action_code": "CUSTOMER_CONTACTED", "result": "NOT_REACHED"})
    assert ok.status_code == 200, ok.text
    assert any(a["action_code"] == "CUSTOMER_CONTACTED" for a in ok.json()["workflow"]["actions"])
    # Somebody not working the case cannot record steps on it.
    assert infosec.post(f"/v1/cases/{case_id}/actions",
                        json={"action_code": "TIMELINE_REVIEWED", "result": "UNUSUAL"}).status_code == 403

    # Escalation needs a reason, then leaves the analyst's queue for InfoSec's.
    assert analyst.post(f"/v1/cases/{case_id}/escalate", json={"target": "INFOSEC"}).status_code == 400
    r = analyst.post(f"/v1/cases/{case_id}/escalate",
                     json={"target": "INFOSEC", "note": "Customer unreachable while a SIM change is recent"})
    assert r.status_code == 200, r.text
    assert stage(analyst, case_id) == "ESCALATED"
    mine = analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]
    assert case_id not in [c["id"] for c in mine]
    sent = analyst.get("/v1/worklist", params={"scope": "escalated", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in sent]
    theirs = infosec.get("/v1/worklist", params={"scope": "escalated", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in theirs]
    wf = analyst.get(f"/v1/cases/{case_id}/workflow").json()
    assert wf["escalation"]["by_id"] == me and "unreachable" in wf["escalation"]["reason"]

    # InfoSec takes it, and it becomes theirs; the analyst cannot hand back their own escalation.
    assert infosec.post(f"/v1/cases/{case_id}/review").status_code == 200
    assert analyst.post(f"/v1/cases/{case_id}/return", json={"findings": "nothing"}).status_code == 400
    back = infosec.post(f"/v1/cases/{case_id}/return", json={"findings": "SIM swap confirmed with the carrier"})
    assert back.status_code == 200, back.text
    assert back.json()["assignee"] and stage(analyst, case_id) == "IN_REVIEW"
    mine = analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in mine], "handed back to the analyst who escalated"

    # The analyst records the outcome; it waits for a lead.
    r = analyst.post(f"/v1/cases/{case_id}/disposition",
                     json={"outcome": "CONFIRMED_FRAUD", "close": False, "followed_recommendation": True})
    assert r.status_code == 200, r.text
    assert stage(analyst, case_id) == "AWAITING_CLOSE"
    assert case_id not in [c["id"] for c in analyst.get("/v1/worklist", params={"scope": "mine", "limit": 200}).json()["items"]]
    waiting = lead.get("/v1/worklist", params={"scope": "awaiting_close", "limit": 200}).json()["items"]
    assert case_id in [c["id"] for c in waiting]

    assert lead.post(f"/v1/cases/{case_id}/close").status_code == 200
    assert stage(lead, case_id) == "CLOSED"
    lifecycle = [m["key"] for m in lead.get(f"/v1/cases/{case_id}/workflow").json()["lifecycle"]]
    for key in ("CASE_REVIEW_STARTED", "CASE_ACTION_RECORDED", "CASE_ESCALATED", "CASE_RETURNED",
                "CASE_OUTCOME_SET", "CASE_CLOSED"):
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

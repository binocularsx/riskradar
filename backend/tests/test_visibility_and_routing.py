"""D94: work is routed to an analyst, and nobody reads outside their scope.

The plan's authorization suite: an analyst cannot reach another analyst's case
through the case endpoint, the list, the worklist, search, the workflow, the
connections view, the submission history or the pipeline; a lead can; InfoSec
sees escalations to InfoSec and nothing else. Plus the routing service: a new
case is assigned to the analyst carrying the least work, an unroutable case
waits visibly rather than vanishing, and reassignment stays a lead's power.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from riskradar.cases import assignment
from riskradar.config import settings

from conftest import login

PASSWORDS = {
    "analyst@riskradar.local": "Analyst#2026",
    "lead@riskradar.local": "OpsLead#2026",
}


def as_user(email: str) -> TestClient:
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    login(c, email, PASSWORDS[email])
    return c


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


@pytest.fixture
def somebody_elses_case():
    """A case owned by a second analyst, who is not the demo analyst."""
    now = datetime.now(timezone.utc)
    email = f"other-{uuid.uuid4().hex[:8]}@riskradar.local"
    with _db() as c:
        other = c.execute(
            "INSERT INTO users (email, display_name, role, active, password_hash) "
            "VALUES (%s, 'Other Analyst', 'ANALYST', true, 'x') RETURNING id", (email,)
        ).fetchone()["id"]
        case = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, assignee_id, assigned_at) "
            "VALUES (%s, 'UNDER_REVIEW', 'HIGH', %s, %s, %s, %s, now()) RETURNING id",
            (f"sub_vis_{uuid.uuid4().hex[:12]}", now, now, now + timedelta(hours=24), other),
        ).fetchone()["id"]
        c.commit()
    yield {"case_id": case, "user_id": other}
    with psycopg.connect(settings().migrate_dsn) as c:
        c.execute("DELETE FROM fraud_submissions WHERE case_id = %s", (case,))
        c.execute("DELETE FROM cases WHERE id = %s", (case,))
        c.execute("DELETE FROM users WHERE id = %s", (other,))
        c.commit()


def test_an_analyst_cannot_read_another_analysts_case(somebody_elses_case):
    case_id = somebody_elses_case["case_id"]
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")

    # Every read path, not only the buttons (plan §3.1).
    for path in (f"/v1/cases/{case_id}", f"/v1/cases/{case_id}/workflow", f"/v1/cases/{case_id}/links",
                 f"/v1/cases/{case_id}/submissions"):
        assert analyst.get(path).status_code == 404, path
        assert lead.get(path).status_code == 200, path

    # And not through any list either.
    # Newest first: the suite shares the demo database, where the live feed can
    # open more than a page of cases, and this one was opened just now.
    assert case_id not in [c["id"] for c in analyst.get("/v1/cases?limit=200&sort=opened").json()["items"]]
    assert case_id in [c["id"] for c in lead.get("/v1/cases?limit=200&sort=opened").json()["items"]]
    assert case_id not in [c["id"] for c in analyst.get("/v1/worklist?scope=all&limit=200").json()["items"]]
    lead_worklist = lead.get("/v1/worklist?scope=all&limit=200").json()["items"]
    if len(lead_worklist) < 200:  # a full page may simply rank this case below the cut
        assert case_id in [c["id"] for c in lead_worklist]
    stages = analyst.get("/v1/workflow/pipeline").json()["stages"]
    assert case_id not in [i["id"] for s in stages for i in s["items"]]

    # Writing is refused for the same reason.
    assert analyst.post(f"/v1/cases/{case_id}/notes", json={"body": "not mine"}).status_code == 404


def test_search_is_scoped_to_what_the_caller_holds(somebody_elses_case):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    lead_rows = lead.get("/v1/transactions/search?limit=50").json()["items"]
    assert lead_rows, "a lead searches the bank's traffic"

    mine = analyst.get("/v1/transactions/search?limit=50").json()["items"]
    refs = [i["transaction_ref"] for i in mine]
    if refs:
        with _db() as c:
            outside = c.execute(
                """
                SELECT count(*) AS n FROM transactions t
                 WHERE t.transaction_ref = ANY(%s)
                   AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.subject_token = t.subject_token
                                     AND c.assignee_id = (SELECT id FROM users
                                                           WHERE email = 'analyst@riskradar.local'))
                """,
                (refs,),
            ).fetchone()["n"]
        assert outside == 0, "every payment an analyst can search belongs to a customer they hold"
    # The other analyst's customer is not searchable by this one.
    with _db() as c:
        theirs = c.execute("SELECT subject_token FROM cases WHERE id = %s",
                           (somebody_elses_case["case_id"],)).fetchone()["subject_token"]
        ref = c.execute("SELECT transaction_ref FROM transactions WHERE subject_token = %s LIMIT 1",
                        (theirs,)).fetchone()
    if ref:
        found = analyst.get("/v1/transactions/search", params={"q": ref["transaction_ref"]}).json()["items"]
        assert found == []


def test_a_new_case_is_routed_to_the_least_loaded_analyst():
    now = datetime.now(timezone.utc)
    with _db() as c:
        case_id = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at) VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s) RETURNING id",
            (f"sub_route_{uuid.uuid4().hex[:12]}", now, now, now + timedelta(hours=24)),
        ).fetchone()["id"]
        sys_uid = c.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]
        loads = {u["id"]: u["open_cases"] for u in assignment.eligible(c)}
        out = assignment.route(c, case_id, sys_uid=sys_uid)
        assert out["assigned"] and out["assignee_id"] in loads
        assert loads[out["assignee_id"]] == min(loads.values()), "the least loaded analyst"
        # Routing twice does nothing; the case already has an owner.
        assert assignment.route(c, case_id, sys_uid=sys_uid)["why"] == "already assigned"
        row = c.execute("SELECT assignee_id, assigned_at, assignment_reason FROM cases WHERE id = %s",
                        (case_id,)).fetchone()
        assert row["assigned_at"] and "least open work" in row["assignment_reason"]
        trail = c.execute("SELECT action FROM audit_log WHERE object_type = 'case' AND object_id = %s",
                          (str(case_id),)).fetchall()
        assert any(a["action"] == "CASE_ASSIGNED_AUTOMATICALLY" for a in trail)
        c.rollback()


def test_a_machine_handled_case_is_not_routed():
    now = datetime.now(timezone.utc)
    with _db() as c:
        case_id = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, handling) VALUES (%s, 'OPEN', 'CRITICAL', %s, %s, %s, 'MACHINE') RETURNING id",
            (f"sub_mach_{uuid.uuid4().hex[:12]}", now, now, now + timedelta(hours=24)),
        ).fetchone()["id"]
        sys_uid = c.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]
        out = assignment.route(c, case_id, sys_uid=sys_uid)
        assert not out["assigned"] and "machine-handled" in out["why"]
        c.rollback()


def test_a_machine_case_that_becomes_human_work_is_routed(client, api_headers, sample_transaction):
    """D94 + D80: the alert that makes a machine-handled case human work must route it.

    Routing deliberately skips a case while the machine is handling it, so such
    a case has never had an owner. The alert that turns it into human work is
    therefore the first moment it needs one. Guarding that on "did this alert
    open the case?" misses it entirely: the case already existed, so nothing
    routed it, and D94 shows an analyst only their own cases — the work is not
    in anyone's queue to find.
    """
    from riskradar.policy import disposition as tiers
    from riskradar.security.tokens import subject_token
    from riskradar.worker import scoring

    payment = sample_transaction()
    # raise_alerts off: this leaves a scored payment with no alert and no case,
    # so the only case in play is the machine-handled one set up below.
    r = client.post("/v1/transactions/batch", headers=api_headers,
                    json={"transactions": [payment], "is_replay": False, "raise_alerts": False})
    assert r.status_code == 202, r.text
    tx_id = r.json()["results"][0]["transaction_id"]
    subject = subject_token(payment["customer_id"])
    now = datetime.now(timezone.utc)

    with _db() as c:
        try:
            sys_uid = scoring.system_user_id(c)
            if not c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone():
                scoring.score_transaction(c, tx_id, sys_uid=sys_uid)
            decision_id = c.execute("SELECT id FROM decisions WHERE transaction_id = %s",
                                    (tx_id,)).fetchone()["id"]
            # The customer already has an open case the machine is handling, and
            # so nobody owns it: exactly what D80 leaves behind.
            case_id = c.execute(
                "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
                "correlation_expires_at, handling) VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s, 'MACHINE') "
                "RETURNING id",
                (subject, now, now, now + timedelta(hours=24)),
            ).fetchone()["id"]

            _, joined = scoring.raise_alert(
                c, sys_uid, decision_id=decision_id, transaction_id=tx_id,
                transaction_ref=payment["transaction_ref"], subject_token=subject, occurred_at=now,
                risk_level="HIGH", score=80, signal_codes=["VELOCITY_SPIKE"],
                disposition=tiers.HUMAN_REVIEW, rule_only=False,
            )
            assert joined == case_id, "the alert joined the case that was already open"
            case = c.execute("SELECT handling, assignee_id, assignment_reason FROM cases WHERE id = %s",
                             (case_id,)).fetchone()
            assert case["handling"] == "HUMAN", "a review alert makes it human work (D80)"
            assert case["assignee_id"] is not None, "and human work has an owner (D94)"
            assert "least open work" in case["assignment_reason"]
        finally:
            c.rollback()
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
            c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
            c.commit()


def test_next_case_hands_back_your_own_work_not_someone_elses(somebody_elses_case):
    analyst = as_user("analyst@riskradar.local")
    r = analyst.post("/v1/worklist/next")
    assert r.status_code == 200
    body = r.json()
    if body["case"] is not None:
        with _db() as c:
            owner = c.execute("SELECT assignee_id FROM cases WHERE id = %s", (body["case"]["id"],)).fetchone()
            me = c.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]
        assert owner["assignee_id"] == me, "handed only what is already theirs"
    else:
        assert "queue is clear" in body["note"]
    # It never hands out the other analyst's case.
    assert body["case"] is None or body["case"]["id"] != somebody_elses_case["case_id"]


def test_the_live_stream_only_carries_events_a_subscriber_may_see(somebody_elses_case):
    """D94: an event about a case reaches only people who may see that case."""
    from riskradar.api.routers.stream import _visible

    other_case = somebody_elses_case["case_id"]
    with _db() as c:
        analyst = {"id": c.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"],
                   "role": "ANALYST"}
        lead = {"id": c.execute("SELECT id FROM users WHERE email = 'lead@riskradar.local'").fetchone()["id"],
                "role": "FRAUD_OPS_LEAD"}
        mine = c.execute("SELECT id FROM cases WHERE assignee_id = %s LIMIT 1", (analyst["id"],)).fetchone()
        rows = [
            {"id": 1, "event_type": "alert", "payload": {"case_id": other_case}},
            {"id": 2, "event_type": "alarm", "payload": {"code": "MODEL_UNAVAILABLE"}},
            {"id": 3, "event_type": "fraud_submitted", "payload": {"case_id": other_case}},
        ]
        if mine:
            rows.append({"id": 4, "event_type": "alert", "payload": {"case_id": mine["id"]}})

        seen = {r["id"] for r in _visible(c, analyst, rows)}
        assert other_case not in [r["payload"].get("case_id") for r in _visible(c, analyst, rows)]
        assert 2 in seen, "an alarm is the desk's weather and reaches everyone"
        if mine:
            assert 4 in seen, "their own case's alerts still arrive"
        # A lead sees all of it.
        assert {r["id"] for r in _visible(c, lead, rows)} == {r["id"] for r in rows}


def test_a_payment_check_is_scoped_like_search(client, api_headers, sample_transaction):
    """One payment's check (Payment lookup) follows the same rule as search:
    for a customer outside the analyst's cases it does not exist; a lead sees
    it, flagged or not."""
    body = sample_transaction()
    r = client.post("/v1/transactions", json=body, headers=api_headers)
    assert r.status_code in (200, 201, 202), r.text
    path = f"/v1/transactions/{body['transaction_ref']}/check"
    assert as_user("analyst@riskradar.local").get(path).status_code == 404
    seen = as_user("lead@riskradar.local").get(path)
    assert seen.status_code == 200, seen.text
    check = seen.json()
    assert check["transaction"]["transaction_ref"] == body["transaction_ref"]
    assert check["case"] is None or check["case"].get("not_on_this_payment")
    # Tokens identify customers; the check never hands them out.
    assert "subject_token" not in check["transaction"] and "beneficiary_token" not in check["transaction"]

"""D114: notifications go to the person concerned, and stop asking once the work is done.

* An analyst's proposal asks the leads, never the analyst who wrote it.
* A lead's decision clears the request for every lead and tells the analyst.
* Repeated news about the same case merges into one line.
* Nobody reads or marks another person's notifications.
* The stream rings a notification's doorbell only for the people it names.
* An administrator's change asks the other administrators, not its maker.
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
    "admin@riskradar.local": "Admin#2026",
    "admin2@riskradar.local": "Admin#2026",
}
RATIONALE = "Customer confirmed by phone that they did not make these payments; device is new."


def as_user(email: str) -> TestClient:
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    login(c, email, PASSWORDS[email])
    return c


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def _uid(email: str) -> int:
    with _db() as c:
        return c.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"]


def _mine(email: str, case_id: int, kind: str) -> list[dict]:
    with _db() as c:
        return c.execute("SELECT * FROM notifications WHERE recipient_id = %s AND case_id = %s AND kind = %s",
                         (_uid(email), case_id, kind)).fetchall()


@pytest.fixture
def case_id():
    """A case held by the demo analyst."""
    now = datetime.now(timezone.utc)
    with _db() as c:
        row = c.execute(
            "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, "
            "correlation_expires_at, assignee_id, assigned_at) VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s, "
            "(SELECT id FROM users WHERE email = 'analyst@riskradar.local'), now()) RETURNING id",
            (f"sub_nt_{uuid.uuid4().hex[:14]}", now, now, now + timedelta(hours=24)),
        ).fetchone()
        c.commit()
    yield row["id"]
    with psycopg.connect(settings().migrate_dsn) as c:
        c.execute("DELETE FROM notifications WHERE case_id = %s", (row["id"],))
        for table in ("fraud_submissions", "case_actions", "case_notes", "alerts"):
            c.execute(f"DELETE FROM {table} WHERE case_id = %s", (row["id"],))
        c.execute("DELETE FROM cases WHERE id = %s", (row["id"],))
        c.commit()


def test_a_proposal_asks_the_leads_and_their_decision_answers_the_analyst(case_id):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    r = analyst.post(f"/v1/cases/{case_id}/submissions",
                     json={"proposed_outcome": "CONFIRMED_FRAUD", "rationale": RATIONALE})
    assert r.status_code == 200, r.text
    sid = r.json()["submission"]["id"]

    waiting = _mine("lead@riskradar.local", case_id, "APPROVAL_WAITING")
    assert len(waiting) == 1 and waiting[0]["urgent"] and waiting[0]["resolved_at"] is None
    assert not _mine("analyst@riskradar.local", case_id, "APPROVAL_WAITING"), "never the person who did it"

    back = lead.post(f"/v1/submissions/{sid}/decision", json={"decision": "RETURN", "reason": "Call the customer again first."})
    assert back.status_code == 200, back.text
    with _db() as c:
        still_open = c.execute("SELECT count(*) AS n FROM notifications WHERE kind = 'APPROVAL_WAITING' "
                               "AND ref = %s AND resolved_at IS NULL", (str(sid),)).fetchone()["n"]
    assert still_open == 0, "no lead is left chasing a decision already made"
    told = _mine("analyst@riskradar.local", case_id, "OUTCOME_SENT_BACK")
    assert told and told[0]["urgent"] and "Call the customer" in told[0]["body"]

    # Proposing again settles the analyst's own "sent back" item.
    analyst.post(f"/v1/cases/{case_id}/submissions",
                 json={"proposed_outcome": "CONFIRMED_FRAUD", "rationale": RATIONALE + " Called again."})
    assert _mine("analyst@riskradar.local", case_id, "OUTCOME_SENT_BACK")[0]["resolved_at"] is not None


def test_repeated_news_about_one_case_is_one_line(case_id):
    lead = as_user("lead@riskradar.local")
    for text in ("First thing I noticed about this customer.", "And a second thing."):
        assert lead.post(f"/v1/cases/{case_id}/notes", json={"body": text}).status_code == 200
    notes = _mine("analyst@riskradar.local", case_id, "NOTE_ADDED")
    assert len(notes) == 1 and notes[0]["times"] == 2 and notes[0]["body"] == "And a second thing."
    assert not _mine("lead@riskradar.local", case_id, "NOTE_ADDED"), "your own note is not news"


def test_people_only_see_and_mark_their_own(case_id):
    analyst, lead = as_user("analyst@riskradar.local"), as_user("lead@riskradar.local")
    lead.post(f"/v1/cases/{case_id}/notes", json={"body": "Something for the analyst to read."})
    theirs = _mine("analyst@riskradar.local", case_id, "NOTE_ADDED")[0]

    assert lead.post(f"/v1/notifications/{theirs['id']}/read").status_code == 404
    assert theirs["id"] not in [i["id"] for i in lead.get("/v1/notifications?limit=200").json()["items"]]

    mine = analyst.get("/v1/notifications?limit=200").json()
    assert theirs["id"] in [i["id"] for i in mine["items"]]
    assert analyst.post(f"/v1/notifications/{theirs['id']}/read").status_code == 200
    assert _mine("analyst@riskradar.local", case_id, "NOTE_ADDED")[0]["read_at"] is not None


def test_the_stream_rings_only_for_the_people_named():
    from riskradar.api.routers.stream import _visible

    rows = [{"id": 1, "event_type": "notification", "payload": {"recipient_ids": [7]}},
            {"id": 2, "event_type": "alarm", "payload": {"code": "X"}}]
    lead = {"id": 9, "role": "FRAUD_OPS_LEAD"}
    assert [r["id"] for r in _visible(None, lead, rows)] == [2], "a lead sees everything, but not other people's bell"
    assert [r["id"] for r in _visible(None, {"id": 7, "role": "FRAUD_OPS_LEAD"}, rows)] == [1, 2]


def test_a_change_asks_the_other_administrators_not_its_maker(conn):
    """Run inside the conftest transaction, which is rolled back: nothing is proposed for real."""
    from riskradar import governance
    from riskradar.security.rbac import ROLE_PERMISSIONS

    maker = conn.execute("SELECT id, display_name FROM users WHERE email = 'admin@riskradar.local'").fetchone()
    actor = {**maker, "permissions": [str(p) for p in ROLE_PERMISSIONS["ADMIN"]]}
    governance.propose(conn, actor, change_type="LIST_ADD", target=f"test:{uuid.uuid4().hex[:8]}",
                       summary="Add a test token to the watch list", payload={}, before_snapshot={},
                       rationale="Testing who is told about a change waiting for approval.")
    told = {r["recipient_id"] for r in conn.execute(
        "SELECT recipient_id FROM notifications WHERE kind = 'CHANGE_WAITING' AND resolved_at IS NULL "
        "AND title LIKE %s", ("%watch list%",)).fetchall()}
    second = conn.execute("SELECT id FROM users WHERE email = 'admin2@riskradar.local'").fetchone()["id"]
    assert second in told
    assert maker["id"] not in told, "maker-checker: the maker is not asked to check"


def test_automatic_routing_tells_the_analyst_it_chose(conn):
    """Rolled back with the conftest transaction: no case is really routed."""
    from riskradar.cases import assignment

    now = datetime.now(timezone.utc)
    case_id = conn.execute(
        "INSERT INTO cases (subject_token, state, risk_level, opened_at, last_alert_at, correlation_expires_at) "
        "VALUES (%s, 'OPEN', 'HIGH', %s, %s, %s) RETURNING id",
        (f"sub_nt_{uuid.uuid4().hex[:14]}", now, now, now + timedelta(hours=24)),
    ).fetchone()["id"]
    sys_uid = conn.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]
    out = assignment.route(conn, case_id, sys_uid=sys_uid)
    assert out["assigned"], out
    told = conn.execute("SELECT recipient_id, urgent FROM notifications WHERE case_id = %s AND kind = 'CASE_ASSIGNED'",
                        (case_id,)).fetchall()
    assert [(t["recipient_id"], t["urgent"]) for t in told] == [(out["assignee_id"], True)]

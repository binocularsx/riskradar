"""WP-05 / D71: the regulator's clocks.

The calendar and the clock engine are pure, so most of this is plain arithmetic
pinned to dates a Nigerian desk would recognise. The database half runs inside
a rolled-back transaction, so no demo case is ever marked as reported.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from riskradar.api.routers import cases as cases_router
from riskradar.api.schemas import MilestoneIn, ReportIn
from riskradar.clocks import sweep as clock_sweep
from riskradar.clocks.calendar import LAGOS, Calendar, Holiday, easter_sunday, observed
from riskradar.clocks.engine import ClockDef, evaluate, most_urgent, parse_policy
from riskradar.worker.scoring import system_user_id

from conftest import login

EASTER_2026 = Calendar([Holiday(date(2026, 4, 3), "Good Friday"), Holiday(date(2026, 4, 6), "Easter Monday")])


def lagos(*args):
    return datetime(*args, tzinfo=LAGOS)


# ---------------------------------------------------------------- calendar


def test_easter():
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2027) == date(2027, 3, 28)


def test_weekend_holidays_move_to_the_next_free_weekday():
    # Christmas 2027 is a Saturday and Boxing Day a Sunday: Monday and Tuesday.
    moved = dict(observed([(date(2027, 12, 25), "Christmas Day"), (date(2027, 12, 26), "Boxing Day")]))
    assert moved == {date(2027, 12, 27): "Christmas Day (observed)", date(2027, 12, 28): "Boxing Day (observed)"}


def test_day_one_is_the_next_working_day_and_the_clock_ends_at_midnight():
    cal = Calendar([Holiday(date(2026, 1, 1), "New Year's Day")])
    # Reported Monday 14 Sep 2026 at 16:00: day one is Tuesday.
    assert cal.add_working_days(lagos(2026, 9, 14, 16), 1) == lagos(2026, 9, 16, 0)
    # Five working days from a Friday skip the weekend: ends end of next Friday.
    assert cal.add_working_days(lagos(2026, 9, 18, 9), 5) == lagos(2026, 9, 26, 0)


def test_fourteen_working_days_across_easter():
    # Reported Thursday 2 April 2026. Good Friday, the weekend and Easter Monday
    # are not working days, so day one is Tuesday 7 April and day 14 is Friday 24.
    assert EASTER_2026.add_working_days(lagos(2026, 4, 2, 11), 14) == lagos(2026, 4, 25, 0)


def test_utc_timestamps_are_counted_in_lagos_time():
    # 23:30 UTC on Friday is 00:30 Saturday in Lagos: day one is Monday.
    start = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    assert Calendar([Holiday(date(2026, 10, 1), "Independence Day")]).add_working_days(start, 1) == lagos(2026, 9, 22, 0)


def test_a_year_with_no_holidays_loaded_is_reported():
    assert EASTER_2026.covers(lagos(2026, 12, 20), lagos(2027, 1, 10)) is False
    assert EASTER_2026.covers(lagos(2026, 3, 1), lagos(2026, 5, 1)) is True


# ------------------------------------------------------------------ engine

POLICY = parse_policy([
    {"code": "CUSTOMER_REPORT_WINDOW", "owner": "CUSTOMER", "escalate": False, "obligation": "report",
     "starts": "fraud_first_at", "ends": "first_reported_at", "amount": 72, "unit": "HOURS"},
    {"code": "NOTIFY_COUNTERPARTY", "owner": "BANK", "obligation": "notify",
     "starts": "first_reported_at", "ends": "counterparty_notified_at", "amount": 30, "unit": "MINUTES"},
    {"code": "CONCLUDE_INVESTIGATION", "owner": "BANK", "obligation": "investigate",
     "starts": "first_reported_at", "ends": "investigation_concluded_at", "amount": 14, "unit": "WORKING_DAYS"},
    {"code": "REIMBURSE_AFTER_INVESTIGATION", "owner": "BANK", "obligation": "reimburse",
     "requires_outcome": "CONFIRMED_FRAUD",
     "starts": "investigation_concluded_at", "ends": "reimbursed_at", "amount": 48, "unit": "HOURS"},
])


def states(events, *, now, outcome=None, calendar=EASTER_2026):
    return {s["code"]: s for s in evaluate(POLICY, events, outcome=outcome, now=now, calendar=calendar)}


def test_no_report_no_clocks():
    assert evaluate(POLICY, {"fraud_first_at": lagos(2026, 4, 1)}, outcome=None,
                    now=lagos(2026, 4, 2), calendar=EASTER_2026) == []


def test_clock_states_through_a_report():
    reported = lagos(2026, 4, 2, 11, 0)
    events = {"fraud_first_at": lagos(2026, 3, 28, 9), "first_reported_at": reported}

    s = states(events, now=reported + timedelta(minutes=10))
    assert s["CUSTOMER_REPORT_WINDOW"]["state"] == "MET_LATE"   # five days after the fraud
    assert s["NOTIFY_COUNTERPARTY"]["state"] == "RUNNING"
    assert s["CONCLUDE_INVESTIGATION"]["due_at"] == lagos(2026, 4, 25, 0)
    assert s["REIMBURSE_AFTER_INVESTIGATION"]["state"] == "NOT_STARTED"

    assert states(events, now=reported + timedelta(minutes=25))["NOTIFY_COUNTERPARTY"]["state"] == "DUE"
    assert states(events, now=reported + timedelta(minutes=31))["NOTIFY_COUNTERPARTY"]["state"] == "BREACHED"

    late = dict(events, counterparty_notified_at=reported + timedelta(minutes=45))
    assert states(late, now=reported + timedelta(hours=2))["NOTIFY_COUNTERPARTY"]["state"] == "MET_LATE"


def test_refund_clocks_do_not_apply_when_the_investigation_finds_no_fraud():
    reported = lagos(2026, 4, 2, 11)
    events = {"first_reported_at": reported, "investigation_concluded_at": reported + timedelta(days=3)}
    assert states(events, now=reported + timedelta(days=10), outcome="FALSE_POSITIVE")[
        "REIMBURSE_AFTER_INVESTIGATION"]["state"] == "NOT_APPLICABLE"
    assert states(events, now=reported + timedelta(days=10), outcome="CONFIRMED_FRAUD")[
        "REIMBURSE_AFTER_INVESTIGATION"]["state"] == "BREACHED"


def test_estimated_holidays_inside_a_clock_are_named():
    cal = Calendar([Holiday(date(2026, 5, 27), "Eid el-Kabir", confirmed=False)])
    s = states({"first_reported_at": lagos(2026, 5, 20, 10)}, now=lagos(2026, 5, 21), calendar=cal)
    assert s["CONCLUDE_INVESTIGATION"]["estimated_holidays"] == ["Eid el-Kabir"]


def test_the_customer_clock_is_never_the_most_urgent_bank_clock():
    reported = lagos(2026, 4, 2, 11)
    urgent = most_urgent(evaluate(POLICY, {"first_reported_at": reported}, outcome=None,
                                  now=reported + timedelta(minutes=5), calendar=EASTER_2026))
    assert urgent["code"] == "NOTIFY_COUNTERPARTY"


def test_policy_validation():
    with pytest.raises(ValueError):
        ClockDef.from_dict({"code": "X", "owner": "BANK", "obligation": "x", "starts": "first_reported_at",
                            "ends": "first_reported_at", "amount": 1, "unit": "HOURS"})
    with pytest.raises(ValueError):
        parse_policy([POLICY[0].__dict__, POLICY[0].__dict__])


# ---------------------------------------------------------------- database


LEAD = {"id": None, "role": "FRAUD_OPS_LEAD", "permissions": ["cases:close", "cases:review"]}
ANALYST = {"id": None, "role": "ANALYST", "permissions": ["cases:review"]}


def _user(conn, email, template):
    row = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    return dict(template, id=row["id"])


@pytest.fixture
def case_id(conn):
    return conn.execute(
        """
        INSERT INTO cases (subject_token, risk_level, correlation_expires_at, state, assignee_id, assigned_at)
        VALUES ('test-clocks-subject', 'HIGH', now() + interval '1 day', 'UNDER_REVIEW',
                (SELECT id FROM users WHERE email = 'analyst@riskradar.local'), now())
        RETURNING id
        """
    ).fetchone()["id"]


def test_the_migration_ships_an_active_policy_and_a_calendar(conn):
    policy = clock_sweep.active_policy(conn)
    codes = {d["code"] for d in policy["definitions"]}
    assert {"NOTIFY_COUNTERPARTY", "CONCLUDE_INVESTIGATION", "TOTAL_REFUND"} <= codes
    assert parse_policy(policy["definitions"])
    assert {2026, 2027} <= set(clock_sweep.load_calendar(conn).years)


def test_report_starts_clocks_once(conn, case_id):
    analyst = _user(conn, "analyst@riskradar.local", ANALYST)
    reported = datetime.now(timezone.utc) - timedelta(minutes=5)
    out = cases_router.record_report(case_id, ReportIn(reported_at=reported, channel="CONTACT_CENTRE"),
                                     user=analyst, conn=conn)
    assert out["case"]["clock_policy_version"] == 1
    assert {c["code"]: c["state"] for c in out["clocks"]}["NOTIFY_COUNTERPARTY"] == "RUNNING"

    with pytest.raises(HTTPException) as again:
        cases_router.record_report(case_id, ReportIn(reported_at=reported, channel="BRANCH"),
                                   user=analyst, conn=conn)
    assert again.value.status_code == 409


def test_milestones_guard_their_order(conn, case_id):
    analyst = _user(conn, "analyst@riskradar.local", ANALYST)
    lead = _user(conn, "lead@riskradar.local", LEAD)

    with pytest.raises(HTTPException) as before_report:
        cases_router.record_milestone(case_id, MilestoneIn(milestone="ACKNOWLEDGED"), user=analyst, conn=conn)
    assert before_report.value.status_code == 400

    cases_router.record_report(case_id, ReportIn(reported_at=datetime.now(timezone.utc) - timedelta(hours=1),
                                                 channel="MOBILE_APP"), user=analyst, conn=conn)
    with pytest.raises(HTTPException) as unnamed:
        cases_router.record_milestone(case_id, MilestoneIn(milestone="COUNTERPARTY_NOTIFIED"), user=analyst, conn=conn)
    assert unnamed.value.status_code == 400
    with pytest.raises(HTTPException) as no_outcome:
        cases_router.record_milestone(case_id, MilestoneIn(milestone="INVESTIGATION_CONCLUDED"), user=analyst, conn=conn)
    assert no_outcome.value.status_code == 400

    conn.execute("UPDATE cases SET outcome = 'CONFIRMED_FRAUD' WHERE id = %s", (case_id,))
    cases_router.record_milestone(case_id, MilestoneIn(milestone="INVESTIGATION_CONCLUDED"), user=analyst, conn=conn)
    with pytest.raises(HTTPException) as analyst_refund:
        cases_router.record_milestone(case_id, MilestoneIn(milestone="REIMBURSED"), user=analyst, conn=conn)
    assert analyst_refund.value.status_code == 403

    out = cases_router.record_milestone(case_id, MilestoneIn(milestone="REIMBURSED"), user=lead, conn=conn)
    assert {c["code"]: c["state"] for c in out["clocks"]}["TOTAL_REFUND"] == "MET"


def test_sweep_records_escalates_and_audits_a_breach_once(conn, case_id):
    analyst = _user(conn, "analyst@riskradar.local", ANALYST)
    system = system_user_id(conn)
    cases_router.record_report(case_id, ReportIn(reported_at=datetime.now(timezone.utc) - timedelta(minutes=40),
                                                 channel="CONTACT_CENTRE"), user=analyst, conn=conn)

    actions = [a for a in clock_sweep.sweep(conn, system_user_id=system) if a["case_id"] == case_id]
    assert actions == [{"case_id": case_id, "clock": "NOTIFY_COUNTERPARTY", "escalated": True}]
    case = conn.execute("SELECT state, escalated_to FROM cases WHERE id = %s", (case_id,)).fetchone()
    assert (case["state"], case["escalated_to"]) == ("ESCALATED", "FRAUD_OPS")
    audited = conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE object_type = 'case' AND object_id = %s "
        "AND action = 'CLOCK_BREACHED'", (str(case_id),)).fetchone()["n"]
    assert audited == 1

    assert not [a for a in clock_sweep.sweep(conn, system_user_id=system) if a["case_id"] == case_id]


def test_sweep_does_not_reopen_a_closed_case(conn, case_id):
    analyst = _user(conn, "analyst@riskradar.local", ANALYST)
    cases_router.record_report(case_id, ReportIn(reported_at=datetime.now(timezone.utc) - timedelta(hours=2),
                                                 channel="BRANCH"), user=analyst, conn=conn)
    conn.execute("UPDATE cases SET state = 'CLOSED', outcome = 'INCONCLUSIVE', closed_at = now() WHERE id = %s",
                 (case_id,))
    actions = [a for a in clock_sweep.sweep(conn, system_user_id=system_user_id(conn)) if a["case_id"] == case_id]
    assert actions and not any(a["escalated"] for a in actions)
    assert conn.execute("SELECT state FROM cases WHERE id = %s", (case_id,)).fetchone()["state"] == "CLOSED"


def test_report_endpoint_refuses_a_naive_timestamp(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.post("/v1/cases/1/report", json={"reported_at": "2026-09-14T10:00:00", "channel": "BRANCH"})
    assert r.status_code == 422


def test_clock_policy_endpoint(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    body = client.get("/v1/clocks/policy").json()
    assert body["policy"]["version"] >= 1 and body["holidays"]


def test_worklist_carries_the_regulatory_clock(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    body = client.get("/v1/worklist?limit=5").json()
    assert "regulatory_breached" in body["summary"]
    for item in body["items"]:
        assert {"reported", "regulatory_clock"} <= set(item)

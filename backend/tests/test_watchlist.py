"""WP-06 / D73: the twenty-four hour flag (REG-NG-04).

The law's two promises are pinned here: a flag never lasts longer than 24 hours
and ends on its own, and a flag that ends with nobody having reached the
customer is recorded as a missed obligation. Database tests run in a rolled-back
transaction, so no demo customer is ever flagged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.clocks import watchlist
from riskradar.worker.scoring import system_user_id

from conftest import login

T0 = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)


def flag(**overrides):
    row = {"placed_at": T0, "expires_at": T0 + timedelta(hours=24), "customer_contacted_at": None,
           "contact_outcome": None, "lifted_at": None, "lift_reason": None}
    row.update(overrides)
    return row


# ------------------------------------------------------------------ states


def test_an_active_flag_counts_down_and_asks_for_contact():
    d = watchlist.describe(flag(), T0 + timedelta(hours=2))
    assert (d["state"], d["contact_state"], d["remaining_minutes"]) == ("ACTIVE", "PENDING", 22 * 60)
    assert watchlist.describe(flag(), T0 + timedelta(hours=19))["contact_state"] == "DUE"


def test_a_flag_past_expiry_reads_expired_before_any_sweep():
    d = watchlist.describe(flag(), T0 + timedelta(hours=24, seconds=1))
    assert (d["state"], d["contact_state"]) == ("EXPIRED", "MISSED")


def test_contact_and_lifting_states():
    contacted = flag(customer_contacted_at=T0 + timedelta(hours=1), contact_outcome="CUSTOMER_CONFIRMED_GENUINE")
    assert watchlist.describe(contacted, T0 + timedelta(hours=30))["contact_state"] == "CONTACTED"
    cleared = flag(lifted_at=T0 + timedelta(hours=1), lift_reason="CLEARED")
    assert watchlist.describe(cleared, T0 + timedelta(hours=2))["state"] == "LIFTED"
    assert watchlist.describe(cleared, T0 + timedelta(hours=2))["contact_state"] == "NOT_NEEDED"


# ---------------------------------------------------------------- database


@pytest.fixture
def case(conn):
    return conn.execute(
        """
        INSERT INTO cases (subject_token, risk_level, correlation_expires_at, state)
        VALUES ('test-watchlist-subject', 'HIGH', now() + interval '1 day', 'UNDER_REVIEW')
        RETURNING *
        """
    ).fetchone()


@pytest.fixture
def analyst_id(conn):
    return conn.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]


def audit_actions(conn, case_id):
    return [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE object_type = 'case' AND object_id = %s ORDER BY id",
        (str(case_id),)).fetchall()]


def test_place_contact_and_lift_are_recorded(conn, case, analyst_id):
    placed = watchlist.place(conn, case=case, user_id=analyst_id, reason="new device then five payees", hours=24)
    assert placed["state"] == "ACTIVE" and watchlist.active_subjects(conn, [case["subject_token"]])

    with pytest.raises(watchlist.WatchlistError) as twice:
        watchlist.place(conn, case=case, user_id=analyst_id, reason="again, same customer", hours=6)
    assert twice.value.status == 409

    contacted = watchlist.record_contact(conn, flag_id=placed["id"], user_id=analyst_id,
                                         outcome="CUSTOMER_CONFIRMED_GENUINE")
    assert contacted["contact_state"] == "CONTACTED"
    with pytest.raises(watchlist.WatchlistError):
        watchlist.record_contact(conn, flag_id=placed["id"], user_id=analyst_id, outcome="CUSTOMER_REPORTED_FRAUD")

    lifted = watchlist.lift(conn, flag_id=placed["id"], user_id=analyst_id, note="customer confirmed")
    assert lifted["state"] == "LIFTED" and not watchlist.active_subjects(conn, [case["subject_token"]])
    assert audit_actions(conn, case["id"])[-3:] == [
        "WATCHLIST_FLAG_PLACED", "WATCHLIST_CUSTOMER_CONTACTED", "WATCHLIST_FLAG_LIFTED"]


def test_hours_are_bounded_by_the_law(conn, case, analyst_id):
    for hours in (0, 25):
        with pytest.raises(watchlist.WatchlistError):
            watchlist.place(conn, case=case, user_id=analyst_id, reason="out of bounds", hours=hours)


def test_the_database_refuses_a_flag_longer_than_24_hours(conn, case, analyst_id):
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO subject_watchlist (subject_token, reason, placed_by, placed_at, expires_at) "
            "VALUES ('x', 'too long', %s, now(), now() + interval '25 hours')", (analyst_id,))


def test_a_placed_flag_cannot_be_extended(conn, case, analyst_id):
    placed = watchlist.place(conn, case=case, user_id=analyst_id, reason="to be extended", hours=2)
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        conn.execute("UPDATE subject_watchlist SET expires_at = expires_at + interval '1 hour' WHERE id = %s",
                     (placed["id"],))


def test_expiry_without_contact_is_recorded_and_escalated(conn, case, analyst_id):
    placed = watchlist.place(conn, case=case, user_id=analyst_id, reason="nobody will call", hours=1)
    system = system_user_id(conn)
    later = datetime.now(timezone.utc) + timedelta(hours=2)

    actions = [a for a in watchlist.expire_due(conn, system_user_id=system, now=later) if a["flag_id"] == placed["id"]]
    assert actions == [{"flag_id": placed["id"], "case_id": case["id"], "contact_missed": True, "escalated": True}]
    row = conn.execute("SELECT lifted_at, expires_at, lift_reason FROM subject_watchlist WHERE id = %s",
                       (placed["id"],)).fetchone()
    assert row["lift_reason"] == "EXPIRED" and row["lifted_at"] == row["expires_at"]
    state = conn.execute("SELECT state, escalated_to FROM cases WHERE id = %s", (case["id"],)).fetchone()
    assert (state["state"], state["escalated_to"]) == ("ESCALATED", "FRAUD_OPS")
    assert "WATCHLIST_FLAG_EXPIRED" in audit_actions(conn, case["id"])

    assert not [a for a in watchlist.expire_due(conn, system_user_id=system, now=later) if a["flag_id"] == placed["id"]]


def test_expiry_after_contact_is_not_escalated(conn, case, analyst_id):
    placed = watchlist.place(conn, case=case, user_id=analyst_id, reason="called in time", hours=1)
    watchlist.record_contact(conn, flag_id=placed["id"], user_id=analyst_id, outcome="CUSTOMER_REPORTED_FRAUD")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    actions = [a for a in watchlist.expire_due(conn, system_user_id=system_user_id(conn), now=later)
               if a["flag_id"] == placed["id"]]
    assert actions[0]["contact_missed"] is False and actions[0]["escalated"] is False
    assert conn.execute("SELECT state FROM cases WHERE id = %s", (case["id"],)).fetchone()["state"] == "UNDER_REVIEW"


def test_closed_cases_cannot_place_a_flag(conn, case, analyst_id):
    closed = dict(case, state="CLOSED")
    with pytest.raises(watchlist.WatchlistError):
        watchlist.place(conn, case=closed, user_id=analyst_id, reason="too late for this", hours=24)


def test_admin_cannot_see_or_place_flags(client):
    """D12b: the account that tunes detection holds no case permission."""
    login(client, "admin@riskradar.local", "Admin#2026")
    assert client.get("/v1/watchlist").status_code == 403
    assert client.post("/v1/cases/1/watchlist", json={"reason": "not allowed"}).status_code == 403


def test_watchlist_endpoint_and_case_detail_shape(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert "items" in client.get("/v1/watchlist").json()
    items = client.get("/v1/worklist?scope=all&limit=1").json()["items"]
    if items:
        assert "watchlisted" in items[0]


def test_placing_a_flag_asks_support_to_make_the_contact(conn, case, analyst_id):
    """D109f: the desk flags; support contacts the customer and records it."""
    watchlist.place(conn, case=case, user_id=analyst_id, reason="new device then five payees", hours=24)
    asked = conn.execute("SELECT kind, payload FROM support_reports WHERE case_id = %s", (case["id"],)).fetchall()
    assert [r["kind"] for r in asked] == ["CONTACT_REQUEST"]
    assert asked[0]["payload"]["reason"] == "new device then five payees"

    system = system_user_id(conn)
    contacted = watchlist.record_contact_for_case(conn, case_id=case["id"], user_id=system,
                                                  outcome="CUSTOMER_CONFIRMED_GENUINE")
    assert contacted["contact_state"] == "CONTACTED"


def test_the_desk_cannot_record_the_contact_itself(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.post("/v1/watchlist/1/contact", json={"outcome": "CUSTOMER_CONFIRMED_GENUINE"})
    assert r.status_code == 410 and "support" in r.text


def test_support_contact_needs_an_api_key(client):
    r = client.post("/v1/support/watchlist/1/contact", json={"outcome": "CUSTOMER_CONFIRMED_GENUINE"})
    assert r.status_code == 401

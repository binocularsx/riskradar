"""D86 and D87: the alert budget holds at run time, and the front door protects itself.

Pinned:
* the guard's arithmetic: mandatory and machine alerts always pass, the day's
  budget and the hour's share hold the rest back;
* end to end through the worker: an alert over budget is deferred, recorded in
  the decision's trace, and raised later when there is room, most serious first;
* a deferred alert that waits too long expires, and the expiry is counted;
* the budget, deferred queue, readiness and system status endpoints answer;
* a caller over its rate gets 429 with Retry-After, and a backlog gets 503.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.policy import budget
from riskradar.policy.engine import Thresholds
from riskradar.worker import scoring

from conftest import login

CFG = budget.BudgetConfig(per_day=75, hourly_burst=3.0)


def test_the_hour_gets_a_bounded_share_of_the_day():
    assert CFG.hour_ceiling == 10  # ceil(75 * 3 / 24)
    assert budget.BudgetConfig(per_day=1, hourly_burst=1.0).hour_ceiling == 1


@pytest.mark.parametrize(
    "raised, hour, mandatory, machine, expected",
    [
        (0, 0, False, False, (budget.RAISE, budget.WITHIN_BUDGET)),
        (75, 0, False, False, (budget.DEFER, budget.DAILY_CAP)),
        (40, 10, False, False, (budget.DEFER, budget.HOURLY_PACE)),
        (75, 10, True, False, (budget.RAISE, budget.MANDATORY)),
        (75, 10, False, True, (budget.RAISE, budget.MACHINE)),
    ],
)
def test_the_guard_holds_back_only_discretionary_alerts(raised, hour, mandatory, machine, expected):
    assert budget.judge(raised_today=raised, hour_count=hour, config=CFG,
                        mandatory=mandatory, machine=machine) == expected


def test_switched_off_it_counts_but_never_defers():
    off = budget.BudgetConfig(per_day=75, enforced=False)
    assert budget.judge(raised_today=500, hour_count=500, config=off, mandatory=False, machine=False) == (
        budget.RAISE, budget.NOT_ENFORCED)


def test_pace_reads_the_day():
    noon = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)  # 12:00 in Lagos
    assert budget.pace({"raised": 75, "hourly": [0] * 24}, CFG, noon)["state"] == "SPENT"
    assert budget.pace({"raised": 36, "hourly": [0] * 24}, CFG, noon)["state"] == "ON_PACE"
    assert budget.pace({"raised": 60, "hourly": [0] * 24}, CFG, noon)["state"] == "AHEAD"




def _ingest(client, api_headers, sample_transaction) -> int:
    body = sample_transaction()
    r = client.post("/v1/transactions", json=body, headers=api_headers)
    assert r.status_code == 202, r.text
    return r.json()["transaction_id"]


def _score(c, tx_id: int) -> dict:
    c.execute("DELETE FROM decisions WHERE transaction_id = %s", (tx_id,))  # a running worker may have scored it
    active = scoring.active_thresholds(c)
    # Everything alerts: the test is about the budget, not the model.
    everything = Thresholds(id=active.id, version=active.version, p_monitor=0.0, p_review=0.0, p_hold=1.0)
    return scoring.score_transaction(c, tx_id, sys_uid=scoring.system_user_id(c), thresholds=everything)


def test_over_budget_an_alert_waits_and_is_raised_when_there_is_room(client, api_headers, sample_transaction):
    first = _ingest(client, api_headers, sample_transaction)
    second = _ingest(client, api_headers, sample_transaction)
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        try:
            c.execute("UPDATE app_config SET value = '2' WHERE key = 'alert_budget_per_day'")
            c.execute("UPDATE app_config SET value = 'true' WHERE key = 'alert_budget_enforced'")
            # A whole day's budget may land in one hour here; pacing has its own test.
            c.execute("UPDATE app_config SET value = '24' WHERE key = 'alert_budget_hourly_burst'")
            # This is about the day's cap, so the whole budget is the model's;
            # the envelopes have their own tests (D92).
            c.execute("UPDATE app_config SET value = '0' WHERE key = 'alert_budget_rule_share'")
            day, _ = budget.local_day_hour(datetime.now(timezone.utc))
            c.execute("INSERT INTO alert_budget_days (day, budget, raised) VALUES (%s, 2, 2) "
                      "ON CONFLICT (day) DO UPDATE SET raised = 2, budget = 2, rule_raised = 0, model_raised = 2",
                      (day,))

            before = c.execute("SELECT deferred FROM alert_budget_days WHERE day = %s", (day,)).fetchone()["deferred"]
            held = _score(c, first)
            assert held["alert_id"] is None and held["deferred"]["reason"] == budget.DAILY_CAP
            trace = c.execute("SELECT policy_trace FROM decisions WHERE transaction_id = %s", (first,)).fetchone()
            step = [s for s in trace["policy_trace"] if s["step"] == "budget"][0]
            assert step["verdict"] == "DEFER" and step["per_day"] == 2
            # A delta, not an absolute: the demo bank's own day shares this ledger row.
            assert c.execute("SELECT deferred FROM alert_budget_days WHERE day = %s",
                             (day,)).fetchone()["deferred"] == before + 1

            # Nothing released while the day is spent.
            assert scoring.release_deferred(c, scoring.system_user_id(c))["released"] == []

            # Tomorrow's room, today: the waiting alert is raised and counted.
            c.execute("UPDATE alert_budget_days SET raised = 0, rule_raised = 0, model_raised = 0, "
                      "released = 0, hourly = array_fill(0, ARRAY[24]) WHERE day = %s", (day,))
            c.execute("UPDATE alert_deferrals SET state = 'EXPIRED' WHERE state = 'WAITING' "
                      "AND decision_id <> (SELECT id FROM decisions WHERE transaction_id = %s)", (first,))
            done = scoring.release_deferred(c, scoring.system_user_id(c))
            assert len(done["released"]) == 1 and done["released"][0]["alert_id"]
            row = c.execute("SELECT state, alert_id FROM alert_deferrals WHERE decision_id = "
                            "(SELECT id FROM decisions WHERE transaction_id = %s)", (first,)).fetchone()
            assert row["state"] == "RELEASED" and row["alert_id"] == done["released"][0]["alert_id"]
            ledger = c.execute("SELECT raised, released FROM alert_budget_days WHERE day = %s", (day,)).fetchone()
            assert (ledger["raised"], ledger["released"]) == (1, 1)

            # With room, the next alert goes straight through; then the day is spent again.
            passed = _score(c, second)
            assert passed["alert_id"] is not None and "deferred" not in passed

        finally:
            c.rollback()


def test_a_deferred_alert_that_waits_too_long_expires_and_is_counted(client, api_headers, sample_transaction):
    tx = _ingest(client, api_headers, sample_transaction)
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        try:
            c.execute("UPDATE app_config SET value = '1' WHERE key = 'alert_budget_per_day'")
            c.execute("UPDATE app_config SET value = 'true' WHERE key = 'alert_budget_enforced'")
            c.execute("UPDATE app_config SET value = '0' WHERE key = 'alert_budget_rule_share'")
            day, _ = budget.local_day_hour(datetime.now(timezone.utc))
            c.execute("INSERT INTO alert_budget_days (day, budget, raised) VALUES (%s, 1, 1) "
                      "ON CONFLICT (day) DO UPDATE SET raised = 1, budget = 1, rule_raised = 0, model_raised = 1",
                      (day,))
            assert _score(c, tx)["deferred"]
            c.execute("UPDATE alert_deferrals SET expires_at = now() - interval '1 minute' WHERE state = 'WAITING'")
            done = scoring.release_deferred(c, scoring.system_user_id(c))
            assert done["expired"] >= 1 and done["released"] == []
            assert c.execute("SELECT expired FROM alert_budget_days WHERE day = %s", (day,)).fetchone()["expired"] >= 1
        finally:
            c.rollback()


def test_the_budget_and_status_endpoints_answer(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    b = client.get("/v1/budget")
    assert b.status_code == 200, b.text
    body = b.json()
    assert {"config", "today", "pace", "waiting", "history"} <= set(body)
    assert len(body["today"]["hourly"]) == 24 and body["config"]["hour_ceiling"] >= 1
    assert client.get("/v1/budget/deferred").status_code == 200
    assert client.post("/v1/budget/deferred/999999999/release").status_code == 409
    status = client.get("/v1/system/status")
    assert status.status_code == 200, status.text
    assert {"in_force", "workers", "budget", "checks", "limits"} <= set(status.json())
    cal = client.get("/v1/budget/calibration?last_days=3")
    assert cal.status_code == 200 and cal.json()["verdict"] in {"FITS", "OVER", "UNDER", "NO_DATA"}


def test_an_analyst_cannot_release_over_budget(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/budget").status_code == 200
    assert client.post("/v1/budget/release").status_code == 403


def test_liveness_needs_no_database_and_readiness_explains_itself(client):
    assert client.get("/health/live").json()["status"] == "alive"
    r = client.get("/health/ready")
    assert r.status_code in (200, 503)
    assert {"database", "workers", "queue"} <= set(r.json()["checks"])
    assert r.headers.get("X-Request-ID")


@pytest.fixture
def limits():
    from riskradar.api import flow

    s = settings()
    saved = (s.ingest_rate, s.ingest_burst, s.queue_hard_limit)
    flow.buckets.reset()
    flow.gauge.reset()
    yield s
    s.ingest_rate, s.ingest_burst, s.queue_hard_limit = saved
    flow.buckets.reset()
    flow.gauge.reset()


def test_a_caller_over_its_rate_is_told_when_to_retry(client, api_headers, sample_transaction, limits):
    limits.ingest_rate, limits.ingest_burst = 0.01, 2
    codes = [client.post("/v1/transactions", json=sample_transaction(), headers=api_headers) for _ in range(3)]
    assert [r.status_code for r in codes] == [202, 202, 429]
    assert int(codes[2].headers["Retry-After"]) >= 1
    assert codes[0].headers["X-RateLimit-Remaining"] == "1"


def test_a_backlog_refuses_new_work_with_503(client, api_headers, sample_transaction, limits):
    limits.queue_hard_limit = 0
    r = client.post("/v1/transactions", json=sample_transaction(), headers=api_headers)
    assert r.status_code == 503 and r.headers["Retry-After"]


def test_psi_reads_stability_and_shift():
    import numpy as np

    from riskradar.monitoring import psi, reading

    rng = np.random.default_rng(1)
    a = rng.normal(0, 1, 5000)
    assert reading(psi(a, rng.normal(0, 1, 5000))) == "STABLE"
    assert reading(psi(a, rng.normal(1.0, 1, 5000))) == "SHIFTED"
    assert psi(a, a[:10]) is None


def test_the_drift_report_answers(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    r = client.get("/v1/metrics/drift?recent_days=1&baseline_days=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in {"STABLE", "WATCH", "SHIFTED", "TOO_FEW", "NO_DATA"}
    if body["status"] != "NO_DATA":
        assert {"score", "features", "rules"} <= set(body)


# --- D92: two envelopes, neither able to starve the other --------------------

ENV = budget.BudgetConfig(per_day=100, hourly_burst=24.0, rule_share=0.35)


def test_each_layer_spends_its_own_share():
    assert (ENV.rule_quota, ENV.model_quota) == (35, 65)
    # The rules have spent their share; a rule alert waits, the model's does not.
    spent_rules = dict(raised_today=40, hour_count=0, config=ENV, mandatory=False, machine=False,
                       rule_raised=35, model_raised=5)
    assert budget.judge(rule_driven=True, **spent_rules) == (budget.DEFER, budget.RULE_QUOTA)
    assert budget.judge(rule_driven=False, **spent_rules) == (budget.RAISE, budget.WITHIN_BUDGET)
    # And the other way: a busy model cannot eat the rules' share.
    spent_model = dict(raised_today=70, hour_count=0, config=ENV, mandatory=False, machine=False,
                       rule_raised=5, model_raised=65)
    assert budget.judge(rule_driven=False, **spent_model) == (budget.DEFER, budget.MODEL_QUOTA)
    assert budget.judge(rule_driven=True, **spent_model) == (budget.RAISE, budget.WITHIN_BUDGET)


def test_a_veto_or_machine_action_is_outside_both_envelopes():
    full = dict(raised_today=100, hour_count=99, config=ENV, rule_raised=35, model_raised=65)
    assert budget.judge(mandatory=True, machine=False, rule_driven=True, **full) == (budget.RAISE, budget.MANDATORY)
    assert budget.judge(mandatory=False, machine=True, rule_driven=True, **full) == (budget.RAISE, budget.MACHINE)


def test_headroom_is_reported_per_envelope():
    row = {"raised": 40, "rule_raised": 35, "model_raised": 5, "hourly": [0] * 24}
    room = budget.headroom(row, 3, ENV)
    assert room["rules"] == 0 and room["model"] == 60 and room["total"] == 60

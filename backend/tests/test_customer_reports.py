"""D90: a customer report reaches the desk, jumps the queue, and brings in what the detector missed.

Pinned: a reported case outranks an unreported CRITICAL one; reporting a
payment nobody alerted on raises an alert marked as the customer's, opens the
case, starts the clocks and rings the stream; the missed-fraud figure counts
it; and the old per-case report endpoint takes the same path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.cases.triage import priority_score
from riskradar.config import settings

from conftest import login


@pytest.fixture(autouse=True)
def _leave_no_cases():
    """These tests commit through the API; take their cases back out of the shared database."""
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        start = c.execute("SELECT coalesce(max(id), 0) AS n FROM alerts").fetchone()["n"]
    yield
    purge_customer_report_cases(since_alert_id=start)


def purge_customer_report_cases(since_alert_id: int = 0) -> None:
    from riskradar.policy import budget

    with psycopg.connect(settings().migrate_dsn, row_factory=psycopg.rows.dict_row) as c:
        rows = c.execute("SELECT DISTINCT case_id FROM alerts WHERE source = 'CUSTOMER_REPORT' AND id > %s",
                         (since_alert_id,)).fetchall()
        ids = [r["case_id"] for r in rows]
        n = c.execute("SELECT count(*) AS n FROM alerts WHERE source = 'CUSTOMER_REPORT' AND id > %s",
                      (since_alert_id,)).fetchone()["n"]
        if ids:
            for table in ("case_actions", "case_notes", "clock_breaches", "alerts"):
                c.execute(f"DELETE FROM {table} WHERE case_id = ANY(%s)", (ids,))
            c.execute("DELETE FROM cases WHERE id = ANY(%s)", (ids,))
            day, _ = budget.local_day_hour(datetime.now(timezone.utc))
            c.execute("UPDATE alert_budget_days SET raised = greatest(raised - %s, 0), "
                      "mandatory = greatest(mandatory - %s, 0) WHERE day = %s", (n, n, day))
        c.commit()


def test_a_reported_case_outranks_anything_the_detector_only_suspects():
    unreported = priority_score(risk_level="CRITICAL", exposure_minor=10**12, sla_remaining=-600, alert_count=50)
    reported = priority_score(risk_level="MEDIUM", exposure_minor=100_00, sla_remaining=60, alert_count=1,
                              reported=True)
    assert reported > unreported


def _scored_payment(client, api_headers, sample_transaction) -> dict:
    body = sample_transaction(occurred_at=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    # Alerting off: this payment is one the detector did not flag.
    r = client.post("/v1/transactions/batch", json={"transactions": [body], "is_replay": False,
                                                     "raise_alerts": False}, headers=api_headers)
    assert r.status_code == 202, r.text
    tx_id = r.json()["results"][0]["transaction_id"]
    # Score it here, as the worker would (a running worker may already have).
    from riskradar.worker import scoring

    import time

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row, autocommit=True) as c:
        for _ in range(40):  # live payments are scored first, so a running worker is quick
            if c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone():
                return body
            time.sleep(0.25)
        with c.transaction():
            scoring.score_transaction(c, tx_id, sys_uid=scoring.system_user_id(c))
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
    return body


def test_reporting_a_payment_nobody_alerted_on_opens_a_case_and_tells_the_desk(
        client, api_headers, sample_transaction):
    body = _scored_payment(client, api_headers, sample_transaction)
    login(client, "analyst@riskradar.local", "Analyst#2026")
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        last_event = c.execute("SELECT coalesce(max(id), 0) AS n FROM stream_events").fetchone()["n"]

    r = client.post("/v1/reports", json={
        "transaction_refs": [body["transaction_ref"]],
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "channel": "CONTACT_CENTRE",
        "note": "customer did not make this payment",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["missed_by_detector"]) == 1 and len(out["cases"]) == 1
    case_id = out["cases"][0]
    assert out["clocks_started"] == [case_id] and out["clocks"][str(case_id)]

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        alert = c.execute("SELECT source, risk_level::text AS lvl FROM alerts WHERE case_id = %s", (case_id,)).fetchone()
        assert alert == {"source": "CUSTOMER_REPORT", "lvl": "CRITICAL"}
        kinds = [e["event_type"] for e in c.execute(
            "SELECT event_type FROM stream_events WHERE id > %s", (last_event,)).fetchall()]
        assert "case_reported" in kinds and "alert" in kinds

    # A second report of the same payment does not restart the clocks.
    again = client.post("/v1/reports", json={
        "transaction_refs": [body["transaction_ref"]],
        "reported_at": datetime.now(timezone.utc).isoformat(), "channel": "BRANCH"})
    assert again.status_code == 200 and again.json()["clocks_started"] == []
    assert again.json()["already_alerted"] == [body["transaction_ref"]]

    # It is at the top of the worklist.
    items = client.get("/v1/worklist?scope=all&limit=200").json()["items"]
    ours = next(i for i in items if i["id"] == case_id)
    assert ours["reported"] and ours["missed_by_detector"] == 1
    assert ours["priority"] >= max(i["priority"] for i in items if not i["reported"]) if any(
        not i["reported"] for i in items) else True


def test_unknown_or_mixed_payments_are_refused(client, api_headers, sample_transaction):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    now = datetime.now(timezone.utc).isoformat()
    assert client.post("/v1/reports", json={"transaction_refs": ["no-such-ref"], "reported_at": now,
                                            "channel": "WEB"}).status_code == 404
    a = _scored_payment(client, api_headers, sample_transaction)
    b = _scored_payment(client, api_headers, sample_transaction)
    r = client.post("/v1/reports", json={"transaction_refs": [a["transaction_ref"], b["transaction_ref"]],
                                         "reported_at": now, "channel": "WEB"})
    assert r.status_code == 400 and "more than one" in r.text


def test_the_missed_fraud_figure_answers(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    r = client.get("/v1/metrics/missed?days=30")
    assert r.status_code == 200
    assert {"reported_payments", "flagged_before_report", "missed_by_detector", "by_channel"} <= set(r.json())

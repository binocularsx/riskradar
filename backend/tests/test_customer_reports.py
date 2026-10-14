"""D90, as amended by D109e: a customer report reaches the desk from the support
team, jumps the queue, and brings in what the detector missed.

Pinned: a reported case outranks an unreported CRITICAL one; support forwarding
a payment nobody alerted on raises an alert marked as the customer's, opens the
case, starts the clocks, records support's ticket and rings the stream; the
customer record has to match the payments; the desk can no longer record a
report itself; and the missed-fraud figure counts it.
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
        client, api_headers, support_headers, sample_transaction):
    body = _scored_payment(client, api_headers, sample_transaction)
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as c:
        last_event = c.execute("SELECT coalesce(max(id), 0) AS n FROM stream_events").fetchone()["n"]

    r = client.post("/v1/support/reports", headers=support_headers, json={
        "support_ticket_ref": "SUP-1001",
        "customer_id": body["customer_id"],
        "transaction_refs": [body["transaction_ref"]],
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "channel": "CONTACT_CENTRE",
        "customer_statement": "customer did not make this payment",
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
        ticket = c.execute("SELECT support_ticket_ref FROM cases WHERE id = %s", (case_id,)).fetchone()
        assert ticket["support_ticket_ref"] == "SUP-1001"

    # A second report of the same payment does not restart the clocks, or replace the first ticket.
    again = client.post("/v1/support/reports", headers=support_headers, json={
        "support_ticket_ref": "SUP-1002", "customer_id": body["customer_id"],
        "transaction_refs": [body["transaction_ref"]],
        "reported_at": datetime.now(timezone.utc).isoformat(), "channel": "BRANCH"})
    assert again.status_code == 200 and again.json()["clocks_started"] == []
    assert again.json()["already_alerted"] == [body["transaction_ref"]]

    # It is at the top of the worklist. Read as the lead, who sees the whole
    # desk (D94): an analyst sees only their own cases, and routing hands this
    # one to whichever analyst carries the least open work, so asking one named
    # analyst would be asking whether routing happened to pick them.
    login(client, "lead@riskradar.local", "OpsLead#2026")
    items = client.get("/v1/worklist?scope=all&limit=200").json()["items"]
    ours = next(i for i in items if i["id"] == case_id)
    assert ours["reported"] and ours["missed_by_detector"] == 1
    assert ours["priority"] >= max(i["priority"] for i in items if not i["reported"]) if any(
        not i["reported"] for i in items) else True


def test_unknown_payments_and_a_mismatched_customer_are_refused(client, api_headers, support_headers,
                                                               sample_transaction):
    now = datetime.now(timezone.utc).isoformat()
    a = _scored_payment(client, api_headers, sample_transaction)
    b = _scored_payment(client, api_headers, sample_transaction)

    def report(refs, customer):
        return client.post("/v1/support/reports", headers=support_headers, json={
            "support_ticket_ref": "SUP-9", "customer_id": customer, "transaction_refs": refs,
            "reported_at": now, "channel": "WEB"})

    assert report(["no-such-ref"], a["customer_id"]).status_code == 404
    # D109e: the record has to match. Another customer's payment is refused by name.
    r = report([a["transaction_ref"], b["transaction_ref"]], a["customer_id"])
    if a["customer_id"] != b["customer_id"]:
        assert r.status_code == 422 and b["transaction_ref"] in r.text
    r = report([a["transaction_ref"]], "not-this-customer")
    assert r.status_code == 422 and a["transaction_ref"] in r.text


def test_support_needs_an_api_key_and_the_desk_cannot_record_reports(client):
    now = datetime.now(timezone.utc).isoformat()
    body = {"support_ticket_ref": "SUP-1", "customer_id": "c", "transaction_refs": ["x"],
            "reported_at": now, "channel": "WEB"}
    assert client.post("/v1/support/reports", json=body).status_code == 401
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.post("/v1/reports", json={"transaction_refs": ["x"], "reported_at": now, "channel": "WEB"})
    assert r.status_code == 410 and "support" in r.text


def test_the_missed_fraud_figure_answers(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    r = client.get("/v1/metrics/missed?days=30")
    assert r.status_code == 200
    assert {"reported_payments", "flagged_before_report", "missed_by_detector", "by_channel"} <= set(r.json())


def test_each_key_stays_on_its_own_side(client, api_headers, support_headers, sample_transaction):
    """D109g: a bank key cannot forward customer reports, and the support
    team's key cannot post transactions. Both are refused as the wrong door
    (403), not as a bad key (401)."""
    now = datetime.now(timezone.utc).isoformat()
    report = {"support_ticket_ref": "SUP-1", "customer_id": "c", "transaction_refs": ["x"],
              "reported_at": now, "channel": "WEB"}
    r = client.post("/v1/support/reports", headers=api_headers, json=report)
    assert r.status_code == 403 and "bank" in r.text
    r = client.post("/v1/transactions", headers=support_headers, json=sample_transaction())
    assert r.status_code == 403 and "support team" in r.text
    # The action feed is support's to work, so its key reads it.
    assert client.get("/v1/restrictions?after_id=0&limit=1", headers=support_headers).status_code == 200

"""Completed cases and their decision records, for leads.

Read-only views over what is already recorded: the outcome, who proposed and
who approved it (D93), what support was asked and answered (D97, D109b), and
the case's audit entries (D12c). Leads only.
"""

from __future__ import annotations

import pytest

from conftest import login


def test_only_a_lead_sees_the_completed_cases(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/completed-cases").status_code == 403
    login(client, "admin@riskradar.local", "Admin#2026")
    assert client.get("/v1/completed-cases").status_code == 403, "D12b: an administrator sees no case content"


def test_the_log_lists_closed_cases_newest_first(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    body = client.get("/v1/completed-cases?limit=50").json()
    assert set(body) >= {"items", "has_more", "totals"}
    closed = [i["closed_at"] for i in body["items"]]
    assert closed == sorted(closed, reverse=True)
    for outcome in ("CONFIRMED_FRAUD", "FALSE_POSITIVE"):
        filtered = client.get(f"/v1/completed-cases?outcome={outcome}").json()["items"]
        assert all(i["outcome"] == outcome for i in filtered)
    assert client.get("/v1/completed-cases?outcome=MAYBE").status_code == 422


def test_a_decision_record_names_who_decided_and_how(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    items = client.get("/v1/completed-cases?limit=5").json()["items"]
    if not items:
        pytest.skip("no closed case on this database")
    record = client.get(f"/v1/completed-cases/{items[0]['id']}/record").json()
    assert set(record) >= {"case", "decision", "proposals", "actions", "messages", "trail"}
    decision = record["decision"]
    assert decision["outcome"] == record["case"]["outcome"]
    if decision["how"] == "APPROVED_PROPOSAL":
        # Maker-checker (D93): the record shows two different people.
        assert decision["proposed_by"] and decision["approved_by"]
        assert decision["proposed_by"] != decision["approved_by"]
    else:
        assert decision["how"] == "RECORDED_DIRECTLY"
    # Who opened the case is an access record, not part of the decision.
    assert all(e["action"] != "CASE_VIEWED" for e in record["trail"])
    assert any(e["action"] == "CASE_CLOSED" for e in record["trail"])


def test_an_open_case_has_no_completed_record_yet(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    open_items = client.get("/v1/worklist?scope=all&limit=1").json().get("items", [])
    if not open_items:
        pytest.skip("no open case")
    assert client.get(f"/v1/completed-cases/{open_items[0]['id']}/record").status_code == 409
    assert client.get("/v1/completed-cases/99999999/record").status_code == 404

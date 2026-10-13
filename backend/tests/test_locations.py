"""D113: who sees the location views, and what each one holds.

* **Location analysis** — the whole bank by state and over time — is the
  administrator's alone. It holds totals only, never a case or a customer,
  which is what lets it sit with a role that may see no case (D12b).
* **Geo survey** — one case on the map — is case content. Analysts and leads
  reach it exactly as they reach the case (D94); the administrator does not.
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
}


def as_user(email: str) -> TestClient:
    from riskradar.api.app import app

    c = TestClient(app)
    c.__enter__()
    login(c, email, PASSWORDS[email])
    return c


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


# ------------------------------------------------------- location analysis

def test_only_the_administrator_reads_the_bank_wide_analysis():
    analyst, lead, admin = (as_user(e) for e in PASSWORDS)
    for path in ("/v1/locations/overview", "/v1/locations/moves", "/v1/locations/NG-LA"):
        assert analyst.get(path).status_code == 403, path
        assert lead.get(path).status_code == 403, path
        assert admin.get(path).status_code == 200, path


def test_the_analysis_adds_up_and_says_what_a_location_is():
    body = as_user("admin@riskradar.local").get("/v1/locations/overview?hours=720").json()
    assert "IP address" in body["note"]
    assert sum(s["transactions"] for s in body["states"]) == body["national"]["transactions"]
    for s in body["states"]:
        assert s["new_region_alerts"] <= s["alerts"] <= s["transactions"]


def test_a_state_over_time_holds_no_case_or_customer():
    admin = as_user("admin@riskradar.local")
    body = admin.get("/v1/locations/NG-LA?hours=720").json()
    assert "cases" not in body and "customers" not in body
    for day in body["by_day"]:
        assert day["alerts"] <= day["bank_alerts"] and day["transactions"] <= day["bank_transactions"]
    assert len(body["by_hour"]) == 24


def test_inputs_are_checked():
    admin = as_user("admin@riskradar.local")
    assert admin.get("/v1/locations/overview?channel=CARRIER_PIGEON").status_code == 422
    assert admin.get("/v1/locations/overview?hours=9999").status_code == 422
    assert admin.get("/v1/locations/not-a-state").status_code == 404
    assert admin.get("/v1/locations/moves?hours=24&channel=POS").status_code == 200


# ------------------------------------------------------------- geo survey

def _a_case_with_alerts() -> int | None:
    with _db() as c:
        row = c.execute("SELECT a.case_id FROM alerts a JOIN transactions t ON t.id = a.transaction_id "
                        "WHERE t.ip_region IS NOT NULL ORDER BY a.raised_at DESC LIMIT 1").fetchone()
    return row["case_id"] if row else None


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
            (f"sub_geo_{uuid.uuid4().hex[:12]}", now, now, now + timedelta(hours=24), other),
        ).fetchone()["id"]
        c.commit()
    yield case
    with psycopg.connect(settings().migrate_dsn) as c:
        c.execute("DELETE FROM cases WHERE id = %s", (case,))
        c.execute("DELETE FROM users WHERE id = %s", (other,))
        c.commit()


def test_the_geo_survey_is_scoped_like_the_case(somebody_elses_case):
    analyst, lead, admin = (as_user(e) for e in PASSWORDS)
    path = f"/v1/cases/{somebody_elses_case}/geo"
    assert analyst.get(path).status_code == 404, "not their case, so not their map"
    assert lead.get(path).status_code == 200
    assert admin.get(path).status_code == 403, "the administrator sees no case (D12b)"


def test_a_geo_survey_maps_the_case_and_hides_raw_tokens():
    case_id = _a_case_with_alerts()
    if case_id is None:
        pytest.skip("no located alert in this database")
    body = as_user("lead@riskradar.local").get(f"/v1/cases/{case_id}/geo?days=30").json()
    assert body["events"], "the case's own alerts are the survey's subject"
    assert all("beneficiary_token" not in e for e in body["events"])
    assert all("token" not in d for d in body["destinations"]), "accounts are named A, B, C on screen"
    assert "other banks" in body["limits"], "the screen must say where the trail ends"
    labels = {d["label"] for d in body["destinations"]}
    for person in body["linked"]:
        assert set(person["accounts"]) <= labels
    for near in body["nearby"]:
        assert near["case_id"] != case_id


def test_an_analyst_only_gets_links_to_cases_they_may_open():
    case_id = _a_case_with_alerts()
    if case_id is None:
        pytest.skip("no located alert in this database")
    analyst = as_user("analyst@riskradar.local")
    r = analyst.get(f"/v1/cases/{case_id}/geo")
    if r.status_code == 404:
        pytest.skip("the newest located case is not the demo analyst's")
    body = r.json()
    with _db() as c:
        uid = c.execute("SELECT id FROM users WHERE email = 'analyst@riskradar.local'").fetchone()["id"]
        for ref in [*body["linked"], *body["nearby"]]:
            if ref.get("case_visible"):
                own = c.execute("SELECT 1 FROM cases WHERE id = %s AND (assignee_id = %s OR escalated_by = %s)",
                                (ref["case_id"], uid, uid)).fetchone()
                assert own, "a case marked openable must be one the analyst holds"


def test_the_case_network_is_well_formed_and_holds_no_raw_identifiers():
    """D115: one graph for the network and the map, with a reason for every rank."""
    case_id = _a_case_with_alerts()
    if case_id is None:
        pytest.skip("no located alert in this database")
    body = as_user("lead@riskradar.local").get(f"/v1/cases/{case_id}/geo?days=30").json()
    graph = body["graph"]
    ids = {n["id"] for n in graph["nodes"]}
    assert [n["kind"] for n in graph["nodes"]].count("customer") == 1
    for e in graph["edges"]:
        assert e["from"] in ids and e["to"] in ids, e
        assert e["kind"] in ("paid", "used", "similar")
    for n in graph["nodes"]:
        assert 0 <= n["attention"] <= 100
        assert n["attention"] == 0 or n["reasons"], "every point given is said in words"
        if n["kind"] != "customer":
            # The screen shows each reason with its points; they must add up to what it ranks by.
            assert n["attention"] == min(100, sum(b["points"] for b in n["breakdown"]))
            assert [b["why"] for b in n["breakdown"]] == n["reasons"][:len(n["breakdown"])]
        assert "token" not in n and "subject_token" not in n
        if n["kind"] == "account":
            assert len(n["ending"]) == 4
        if n["kind"] == "device":
            # Usual means neither new to the customer nor shared; only usual ones may be hidden.
            assert n["routine"] == (not n["new_device"] and not n["others"])
            assert n["flagged"] <= len(body["events"])
    assert all("device_token" not in e for e in body["events"]), "the raw device token stays on the server"
    if graph["edges"]:
        assert graph["span"]["start"] <= graph["span"]["end"]

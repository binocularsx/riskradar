"""D69d: false alarms per rule and per model (base PRD FR-505).

Pins the attribution rules, because a quiet change to them would move blame
from one rule to another without any number looking wrong.
"""

from __future__ import annotations

from riskradar.cases.performance import MODEL, alert_ratio, budget_menu, by_driver, wilson

from conftest import login


def sig(code, power="ESCALATE"):
    return {"code": code, "power": power, "severity": "HIGH", "evidence": {}}


def row(case_id, outcome, *signals):
    return {"case_id": case_id, "outcome": outcome, "signals": list(signals)}


def table(rows):
    return {r["driver"]: r for r in by_driver(rows)}


def test_a_case_with_no_raising_rule_is_the_models():
    t = table([row(1, "FALSE_POSITIVE")])
    assert t[MODEL]["false_positive"] == 1


def test_a_case_counts_once_per_rule_however_many_alerts_it_has():
    t = table([
        row(1, "CONFIRMED_FRAUD", sig("VELOCITY_BURST_1H")),
        row(1, "CONFIRMED_FRAUD", sig("VELOCITY_BURST_1H")),
        row(1, "CONFIRMED_FRAUD"),
    ])
    assert t["VELOCITY_BURST_1H"]["cases"] == 1
    # One alert had a rule, so the case is not the model's alone.
    assert MODEL not in t


def test_two_rules_on_one_case_each_get_the_case():
    t = table([row(1, "FALSE_POSITIVE", sig("VELOCITY_BURST_1H"), sig("KNOWN_MULE_BENEFICIARY", "OVERRIDE"))])
    assert t["VELOCITY_BURST_1H"]["false_positive"] == 1
    assert t["KNOWN_MULE_BENEFICIARY"]["false_positive"] == 1


def test_suppressing_rules_are_never_blamed_for_an_alert():
    t = table([row(1, "FALSE_POSITIVE", sig("ESTABLISHED_PAYEE_NORMAL", "SUPPRESS"))])
    assert "ESTABLISHED_PAYEE_NORMAL" not in t
    assert t[MODEL]["false_positive"] == 1


def test_inconclusive_is_not_counted_against_precision():
    t = table([
        row(1, "CONFIRMED_FRAUD", sig("CARD_TESTING_PROBES")),
        row(2, "INCONCLUSIVE", sig("CARD_TESTING_PROBES")),
    ])
    r = t["CARD_TESTING_PROBES"]
    assert r["cases"] == 2 and r["precision"] == 1.0 and r["false_alarm_rate"] == 0.0


def test_thin_evidence_is_flagged():
    t = table([row(i, "FALSE_POSITIVE") for i in range(5)])
    assert t[MODEL]["enough_evidence"] is False


def test_noisiest_driver_leads():
    rows = [row(1, "FALSE_POSITIVE"), row(2, "FALSE_POSITIVE"),
            row(3, "FALSE_POSITIVE", sig("CARD_TESTING_PROBES"))]
    assert by_driver(rows)[0]["driver"] == MODEL


def test_wilson_interval():
    assert wilson(0, 0) is None
    low, high = wilson(9, 10)
    assert low < 0.9 < high and high <= 1.0


def case(outcome, alerts, value=0):
    return {"outcome": outcome, "alerts": alerts, "alerted_value_minor": value}


def test_ratio_counts_alerts_on_false_cases_against_confirmed_incidents():
    """WP-09: the unit banks benchmark is false alerts per confirmed incident."""
    r = alert_ratio([case("FALSE_POSITIVE", 20), case("FALSE_POSITIVE", 10),
                     case("CONFIRMED_FRAUD", 4, 500_000), case("CONFIRMED_FRAUD", 1, 250_000)])
    assert r["false_alerts"] == 30 and r["confirmed_incidents"] == 2
    assert r["false_alerts_per_incident"] == 15.0
    assert r["confirmed_fraud_value_minor"] == 750_000


def test_ratio_is_a_floor_inconclusive_sits_on_neither_side():
    # Alerts inside a confirmed case are never counted as false, and "can't
    # tell" neither adds false alerts nor counts as an incident.
    r = alert_ratio([case("CONFIRMED_FRAUD", 9), case("INCONCLUSIVE", 50)])
    assert r["false_alerts"] == 0 and r["confirmed_incidents"] == 1
    assert r["inconclusive_cases"] == 1 and r["false_alerts_per_incident"] == 0.0


def test_ratio_without_confirmed_fraud_is_not_a_number():
    r = alert_ratio([case("FALSE_POSITIVE", 97)])
    assert r["false_alerts_per_incident"] is None
    assert r["enough_evidence"] is False


def test_three_incidents_cannot_set_a_rate():
    """D66d: the demo day's 32 to 1 came from three incidents."""
    r = alert_ratio([case("FALSE_POSITIVE", 97)] + [case("CONFIRMED_FRAUD", 1)] * 3)
    assert r["false_alerts_per_incident"] == 32.3
    assert r["enough_evidence"] is False


MENU = {"measured": "time-ordered test", "test_days": 30.1, "benchmarks": {},
        "options": [{"budget_per_day": b} for b in (120, 75, 60)]}


def test_budget_menu_marks_todays_budget_and_changes_nothing():
    m = budget_menu(MENU, 75)
    assert m["available"] and m["current_is_measured"]
    assert [o["current"] for o in m["options"]] == [False, True, False]
    assert "current" not in MENU["options"][1]  # the artifact is not mutated


def test_an_unmeasured_budget_borrows_no_figures():
    m = budget_menu(MENU, 90)
    assert m["current_is_measured"] is False
    assert not any(o["current"] for o in m["options"])


def test_budget_menu_without_the_artifact():
    m = budget_menu(None, 75)
    assert m == {"available": False, "current_budget_per_day": 75, "options": []}


def test_budget_menu_endpoint(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    body = client.get("/v1/metrics/budget-menu").json()
    assert {"available", "current_budget_per_day", "options"} <= set(body)
    for option in body["options"]:
        assert {"budget_per_day", "false_alerts_per_incident", "value_detection_rate",
                "seen_fraud", "held_out", "current"} <= set(option)


def test_budget_menu_is_not_for_analysts(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/metrics/budget-menu").status_code == 403


def test_detection_endpoint_for_the_lead(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    body = client.get("/v1/metrics/detection?days=30").json()
    assert {"window_days", "by_driver", "rules", "decided_cases", "ratio"} <= set(body)
    assert {"false_alerts", "confirmed_incidents", "false_alerts_per_incident",
            "enough_evidence"} <= set(body["ratio"])
    for rule in body["rules"]:
        assert {"code", "power", "fired", "on_alerts"} <= set(rule)


def test_detection_endpoint_is_not_for_analysts(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/metrics/detection").status_code == 403


def test_rules_inventory_carries_ownership_and_review(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    rules = client.get("/v1/admin/rules").json()["rules"]
    assert rules
    for rule in rules:
        assert {"owner", "rationale", "approved_at", "next_review_at",
                "fired_30d", "orphaned", "review_overdue", "dormant"} <= set(rule)


def test_opening_a_case_is_recorded(client):
    """D69k: reading a customer's case leaves an entry in the permanent record."""
    import psycopg
    from riskradar.config import settings

    login(client, "analyst@riskradar.local", "Analyst#2026")
    items = client.get("/v1/worklist?scope=all&limit=1").json()["items"]
    if not items:
        import pytest
        pytest.skip("no open case in the demo database")
    case_id = items[0]["id"]

    def views():
        with psycopg.connect(settings().app_dsn) as c:
            return c.execute(
                "SELECT count(*) FROM audit_log WHERE action = 'CASE_VIEWED' AND object_id = %s",
                (str(case_id),),
            ).fetchone()[0]

    before = views()
    body = client.get(f"/v1/cases/{case_id}").json()
    assert views() == before + 1
    assert all(h["action"] != "CASE_VIEWED" for h in body["history"])

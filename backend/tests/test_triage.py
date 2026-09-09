"""Worklist ordering, SLA clocks and the recommendation.

These decide what an analyst sees first and what the screen tells them to do,
which makes them worth pinning down: a subtle change here silently reorders
somebody's whole day.
"""

from __future__ import annotations

import pytest

from riskradar.cases import triage

from conftest import login


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def test_sla_targets_are_tighter_for_worse_cases():
    """A critical case that waits four hours is a different failure from a low
    one that does. One clock for everything would hide that."""
    assert triage.SLA_MINUTES["CRITICAL"] < triage.SLA_MINUTES["HIGH"]
    assert triage.SLA_MINUTES["HIGH"] < triage.SLA_MINUTES["MEDIUM"]
    assert triage.SLA_MINUTES["MEDIUM"] < triage.SLA_MINUTES["LOW"]


@pytest.mark.parametrize(
    "age,level,expected",
    [
        (1, "CRITICAL", "OK"),
        (12, "CRITICAL", "DUE"),        # inside the last quarter of a 15m budget
        (20, "CRITICAL", "BREACHED"),
        (10, "MEDIUM", "OK"),
        (300, "MEDIUM", "BREACHED"),
    ],
)
def test_sla_state(age, level, expected):
    state, _ = triage.sla_state(age, level)
    assert state == expected


def test_overdue_minutes_are_negative_so_the_ui_can_say_how_late():
    _, remaining = triage.sla_state(45, "CRITICAL")
    assert remaining == 15 - 45


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def base(**over):
    args = dict(risk_level="HIGH", exposure_minor=100_000, sla_remaining=60, alert_count=1)
    args.update(over)
    return triage.priority_score(**args)


def test_severity_dominates_ordering():
    """A CRITICAL case outranks a HIGH one even when the HIGH one is worth more.
    Severity is the coarse sort; money orders within it."""
    assert base(risk_level="CRITICAL", exposure_minor=1_000) > base(
        risk_level="HIGH", exposure_minor=500_000_000
    )


def test_money_orders_cases_of_equal_severity():
    assert base(exposure_minor=500_000_000) > base(exposure_minor=10_000)


def test_money_is_sublinear_so_one_whale_cannot_bury_the_queue():
    """Ten times the money must not mean ten times the priority, or a single
    large case would sit on top forever while ordinary work aged out."""
    small = base(exposure_minor=1_000_000)
    large = base(exposure_minor=10_000_000)
    assert large > small
    assert large < small * 10


def test_no_amount_of_money_or_lateness_crosses_a_severity_band():
    """The property the first version of this formula did not have.

    A sanctioned destination is CRITICAL regardless of amount; a ₦5m HIGH case
    that looks merely unusual must not be served ahead of it.
    """
    worst_possible_high = triage.priority_score(
        risk_level="HIGH", exposure_minor=10_000_000_000,
        sla_remaining=-100_000, alert_count=500,
    )
    mildest_possible_critical = triage.priority_score(
        risk_level="CRITICAL", exposure_minor=0, sla_remaining=99_999, alert_count=1,
    )
    assert mildest_possible_critical > worst_possible_high


def test_an_overdue_case_climbs():
    assert base(sla_remaining=-120) > base(sla_remaining=120)


def test_priority_is_not_the_model_score():
    """The model answers 'is this fraud'. The queue answers 'what should be
    worked first'. A big, overdue, medium-risk case beats a small fresh high one."""
    fat_and_late = base(risk_level="MEDIUM", exposure_minor=400_000_000, sla_remaining=-90)
    thin_and_fresh = base(risk_level="MEDIUM", exposure_minor=5_000, sla_remaining=200)
    assert fat_and_late > thin_and_fresh


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------


def rec(**over):
    args = dict(
        risk_level="MEDIUM", signals=[], exposure_minor=50_000,
        declined_count=0, alert_count=1, distinct_beneficiaries=1, new_device=False,
    )
    args.update(over)
    return triage.recommend(**args)


def test_a_veto_rule_produces_an_immediate_action():
    r = rec(signals=["SANCTIONED_BENEFICIARY"], risk_level="CRITICAL")
    assert r.urgency == "now"
    assert "InfoSec" in r.action
    assert r.disposition_hint == "CONFIRMED_FRAUD"


def test_a_known_mule_points_straight_at_confirmed_fraud():
    r = rec(signals=["KNOWN_MULE_BENEFICIARY"])
    assert r.disposition_hint == "CONFIRMED_FRAUD"
    assert "earlier case" in r.because


def test_card_testing_recommends_killing_the_card_not_calling_the_customer():
    """The compromised thing is the card. Calling the customer about a card
    somebody else is holding wastes the only minutes that matter."""
    r = rec(signals=["CARD_TESTING_PROBES"], declined_count=14)
    assert "card" in r.action.lower()
    assert "14" in r.because


def test_takeover_shape_recommends_contacting_the_customer():
    r = rec(
        signals=["VELOCITY_BURST_1H"], new_device=True,
        exposure_minor=420_000_000, alert_count=6, distinct_beneficiaries=4,
    )
    assert "Call the customer" in r.action
    assert r.urgency == "now"


def test_suppressed_cases_are_marked_as_probably_nothing():
    """Suppression is the false-positive control. If the rules already pulled a
    case down, the recommendation should say so rather than making the analyst
    rediscover it."""
    r = rec(signals=["ESTABLISHED_PAYEE_NORMAL"])
    assert r.disposition_hint == "FALSE_POSITIVE"
    assert r.urgency == "routine"


def test_every_recommendation_explains_itself():
    """G3 applies here too: an unexplained recommendation is the bare-score
    problem moved up a layer."""
    cases = [
        rec(),
        rec(risk_level="HIGH"),
        rec(risk_level="CRITICAL"),
        rec(signals=["VELOCITY_BURST_1H"], distinct_beneficiaries=7),
        rec(signals=["CARD_TESTING_PROBES"], declined_count=9),
    ]
    for r in cases:
        assert r.action and len(r.action) > 10
        assert r.because and len(r.because) > 20
        assert r.urgency in ("now", "soon", "routine")


def test_the_amount_appears_in_the_reason():
    """An analyst deciding whether to interrupt a customer needs the number in
    the sentence, not on another part of the screen."""
    r = rec(risk_level="HIGH", exposure_minor=250_000_00)
    assert "₦" in r.because


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


def test_worklist_returns_a_desk_summary(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.get("/v1/worklist?limit=5")
    assert r.status_code == 200
    body = r.json()
    for key in ("open_cases", "total_exposure_minor", "breaching", "unassigned", "mine"):
        assert key in body["summary"]
    for case in body["items"]:
        assert "recommendation" in case
        assert "sla_state" in case
        assert isinstance(case["exposure_minor"], int), "exposure must be a number, not a string"


def test_worklist_is_ordered_by_priority(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    items = client.get("/v1/worklist?limit=40").json()["items"]
    priorities = [c["priority"] for c in items]
    assert priorities == sorted(priorities, reverse=True)


def test_admin_cannot_see_the_worklist(client):
    """Separation of duties reaches the new endpoints too — it is not something
    the old routes happened to have."""
    login(client, "admin@riskradar.local", "Admin#2026")
    assert client.get("/v1/worklist").status_code == 403
    assert client.post("/v1/worklist/next").status_code == 403

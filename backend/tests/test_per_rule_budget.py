"""D92 refinement: per-rule budget shares.

Inside the rules envelope, each rule may raise up to its own cap before it
defers, so a noisy low-precision rule cannot crowd out a better one.

The admissions run against a *future* day, so they touch an ``alert_budget_days``
row no live worker is using — the whole flow commits nothing (the ``conn``
fixture rolls back) and never contends with the running demo.
"""

from __future__ import annotations

from datetime import datetime, timezone

from riskradar.policy import budget
from riskradar.policy.budget import BudgetConfig
from riskradar.rules.engine import Signal

FUTURE = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)


def _day(conn):
    d = budget.local_day_hour(FUTURE)[0]
    row = conn.execute("SELECT rule_counts FROM alert_budget_days WHERE day = %s", (d,)).fetchone()
    return dict(row["rule_counts"]) if row else {}


def test_primary_rule_picks_the_highest_severity_escalating_signal():
    sigs = [Signal("VELOCITY_BURST_1H", "ESCALATE", "HIGH"),
            Signal("ACCOUNT_TAKEOVER_SEQUENCE", "ESCALATE", "CRITICAL"),
            Signal("SANCTIONED_BENEFICIARY", "OVERRIDE", "CRITICAL"),
            Signal("SALARY_ALLOWLIST", "SUPPRESS", "LOW")]
    # The CRITICAL escalate wins; the override (mandatory, outside caps) and the
    # suppress (never raises) are ignored.
    assert budget.primary_rule(sigs) == "ACCOUNT_TAKEOVER_SEQUENCE"
    # No escalating rule → a model-only alert charges no rule.
    assert budget.primary_rule([Signal("SANCTIONED_BENEFICIARY", "OVERRIDE", "CRITICAL")]) is None
    assert budget.primary_rule([]) is None


def test_a_capped_rule_defers_once_it_spends_its_cap(conn):
    cfg = BudgetConfig(per_day=75, enforced=True, rule_caps={"NOISY_RULE": 2})
    kw = dict(mandatory=False, machine=False, rule_driven=True, now=FUTURE, config=cfg)

    a1 = budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    a2 = budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    a3 = budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    assert a1.raised and a2.raised
    assert not a3.raised and a3.reason == budget.RULE_CAP

    # A different rule with room of its own still raises, even though the noisy
    # one is capped — the whole point of the refinement.
    good = budget.admit(conn, budget_rule="GOOD_RULE", **kw)
    assert good.raised

    counts = _day(conn)
    assert counts["NOISY_RULE"] == 2   # the deferred third was not charged
    assert counts["GOOD_RULE"] == 1


def test_an_uncapped_rule_draws_only_against_the_rules_envelope(conn):
    cfg = BudgetConfig(per_day=75, enforced=True, rule_caps={"NOISY_RULE": 2})
    kw = dict(mandatory=False, machine=False, rule_driven=True, now=FUTURE, config=cfg)
    # Ten alerts for a rule with no cap all raise (well within the 45 rules quota).
    for _ in range(10):
        assert budget.admit(conn, budget_rule="UNCAPPED_RULE", **kw).raised
    assert _day(conn).get("UNCAPPED_RULE") == 10


def test_the_cap_trace_names_the_rule_and_its_cap(conn):
    cfg = BudgetConfig(per_day=75, enforced=True, rule_caps={"NOISY_RULE": 1})
    kw = dict(mandatory=False, machine=False, rule_driven=True, now=FUTURE, config=cfg)
    budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    deferred = budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    trace = deferred.as_trace()
    assert trace["reason"] == budget.RULE_CAP
    assert trace["budget_rule"] == "NOISY_RULE" and trace["rule_cap"] == 1


def test_undo_gives_back_the_rule_charge(conn):
    cfg = BudgetConfig(per_day=75, enforced=True, rule_caps={"NOISY_RULE": 5})
    kw = dict(mandatory=False, machine=False, rule_driven=True, now=FUTURE, config=cfg)
    a = budget.admit(conn, budget_rule="NOISY_RULE", **kw)
    assert _day(conn)["NOISY_RULE"] == 1
    budget.undo(conn, a)
    assert _day(conn).get("NOISY_RULE", 0) == 0


def test_a_veto_is_outside_the_caps(conn):
    """A mandatory (OVERRIDE) alert is never charged to a rule cap, even if a
    rule code is passed — it must reach the desk today whatever the budget says."""
    cfg = BudgetConfig(per_day=75, enforced=True, rule_caps={"SANCTIONED_BENEFICIARY": 1})
    kw = dict(machine=False, rule_driven=True, now=FUTURE, config=cfg)
    for _ in range(3):
        a = budget.admit(conn, mandatory=True, budget_rule="SANCTIONED_BENEFICIARY", **kw)
        assert a.raised and a.reason == budget.MANDATORY
    # The veto alerts were not charged against the per-rule cap.
    assert "SANCTIONED_BENEFICIARY" not in _day(conn)

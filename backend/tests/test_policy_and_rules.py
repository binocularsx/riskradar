"""Policy composition and rule powers (D11, D11a, D11b)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from riskradar.features.types import TxView
from riskradar.policy.engine import Thresholds, apply, presentation_score
from riskradar.rules.engine import RuleContext, Signal, evaluate

T = Thresholds(id=1, version=1, p_monitor=0.02, p_review=0.15, p_hold=0.60)

ALL_ENABLED = {
    "VELOCITY_BURST_1H": {"enabled": True, "params": {"min_count": 5}},
    "CARD_TESTING_PROBES": {
        "enabled": True,
        "params": {"min_decline_rate_24h": 0.5, "min_failed_1h": 3},
    },
    "SANCTIONED_BENEFICIARY": {"enabled": True, "params": {}},
    "KNOWN_MULE_BENEFICIARY": {"enabled": True, "params": {}},
    "PRE_REGISTERED_BENEFICIARY": {"enabled": True, "params": {}},
    "ESTABLISHED_PAYEE_NORMAL": {
        "enabled": True,
        "params": {"min_beneficiary_age_days": 60, "max_amount_ratio": 1.0},
    },
}


def tx(**over) -> TxView:
    base = dict(
        transaction_ref="t1",
        occurred_at=datetime.now(timezone.utc),
        amount_minor=100_000,
        currency="NGN",
        channel="MOBILE_APP",
        instrument="ACCOUNT_TRANSFER",
        rail="NIP",
        subject_token="sub_1",
        account_token="acc_1",
        beneficiary_token="acc_2",
    )
    base.update(over)
    return TxView(**base)


def features(**over) -> dict[str, float]:
    base = {
        "amount_log10": 3.0,
        "amount_ratio_to_account_p95_30d": 0.5,
        "txn_count_1h_account": 1.0,
        "approved_value_ratio_24h_vs_daily_mean_30d": 1.0,
        "failed_attempts_1h_account": 0.0,
        "decline_rate_24h_account": 0.0,
        "distinct_beneficiaries_1h_account": 1.0,
        "beneficiary_is_new_to_account": 1.0,
        "beneficiary_first_seen_days": 5.0,
        "device_is_new_to_subject": 0.0,
        "account_age_days": 400.0,
        "days_since_account_activity": 1.0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "p,expected",
    [(0.001, "LOW"), (0.02, "MEDIUM"), (0.14, "MEDIUM"), (0.15, "HIGH"),
     (0.59, "HIGH"), (0.60, "CRITICAL"), (0.99, "CRITICAL")],
)
def test_bands_follow_the_calibrated_probability(p, expected):
    assert apply(p, [], T).risk_level == expected


def test_score_is_presentation_only_and_derived_from_probability():
    """D11b. The 0-100 number sorts a queue; it decides nothing."""
    assert presentation_score(0.42) == 42
    r = apply(0.42, [], T)
    assert r.score_0_100 == 42
    # Nothing in the trace ever thresholds on the score.
    assert all("score" not in step for step in r.trace)


# ---------------------------------------------------------------------------
# Powers
# ---------------------------------------------------------------------------


def test_escalation_raises_one_band():
    sig = Signal(code="VELOCITY_BURST_1H", power="ESCALATE", severity="HIGH")
    assert apply(0.05, [], T).risk_level == "MEDIUM"
    assert apply(0.05, [sig], T).risk_level == "HIGH"


def test_suppression_can_cancel_an_escalation():
    """D11a: suppression is the *primary* false-positive control.

    A control that cannot cancel an escalation is not a control. Paying your own
    landlord six times in an hour is unusual and not suspicious.
    """
    esc = Signal(code="VELOCITY_BURST_1H", power="ESCALATE", severity="HIGH")
    sup = Signal(code="PRE_REGISTERED_BENEFICIARY", power="SUPPRESS", severity="LOW")
    assert apply(0.05, [esc], T).risk_level == "HIGH"
    assert apply(0.05, [esc, sup], T).risk_level == "MEDIUM"


def test_override_beats_everything_including_suppression():
    """Some things are not probabilistic."""
    sup = Signal(code="PRE_REGISTERED_BENEFICIARY", power="SUPPRESS", severity="LOW")
    ovr = Signal(code="SANCTIONED_BENEFICIARY", power="OVERRIDE", severity="CRITICAL")
    result = apply(0.0001, [sup, ovr], T)
    assert result.risk_level == "CRITICAL"
    assert result.decision == "HOLD"
    assert result.trace[-2]["step"] == "override"


def test_rule_only_mode_does_not_invent_a_probability():
    """FR-017 / D15d. When the model is gone we say so; we do not guess."""
    ovr = Signal(code="KNOWN_MULE_BENEFICIARY", power="OVERRIDE", severity="CRITICAL")
    result = apply(0.0, [ovr], T, rule_only_mode=True)
    assert result.trace[0]["source"] == "rule_only_mode"
    assert "p_fraud" not in result.trace[0]
    assert result.risk_level == "CRITICAL"


def test_every_step_is_traced_for_explainability():
    """G3: 100% of alerts carry named signals with evidence."""
    esc = Signal(
        code="VELOCITY_BURST_1H", power="ESCALATE", severity="HIGH",
        evidence={"count": 7, "threshold": 5},
    )
    trace = apply(0.3, [esc], T).trace
    steps = [s["step"] for s in trace]
    assert steps == ["base", "escalate", "final"]
    assert trace[1]["evidence"] == {"count": 7, "threshold": 5}


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------


def test_velocity_rule_fires_at_the_threshold_not_before():
    quiet = evaluate(RuleContext(tx=tx(), features=features(txn_count_1h_account=4)), ALL_ENABLED)
    loud = evaluate(RuleContext(tx=tx(), features=features(txn_count_1h_account=5)), ALL_ENABLED)
    assert [s.code for s in quiet] == []
    assert [s.code for s in loud] == ["VELOCITY_BURST_1H"]
    assert loud[0].evidence == {"count": 5, "threshold": 5, "window": "1h"}


def test_card_testing_rule_requires_card_and_declines():
    """The rule exists only because auth_result exists (D21)."""
    probing = features(decline_rate_24h_account=0.8, failed_attempts_1h_account=4)
    on_card = evaluate(RuleContext(tx=tx(instrument="CARD"), features=probing), ALL_ENABLED)
    on_transfer = evaluate(RuleContext(tx=tx(), features=probing), ALL_ENABLED)
    assert "CARD_TESTING_PROBES" in [s.code for s in on_card]
    assert "CARD_TESTING_PROBES" not in [s.code for s in on_transfer]


def test_familiarity_alone_does_not_suppress():
    """A known payee receiving ten times the usual amount is exactly the shape
    of a compromised session reusing a real payee."""
    normal = features(beneficiary_is_new_to_account=0.0, beneficiary_first_seen_days=200,
                      amount_ratio_to_account_p95_30d=0.4)
    huge = features(beneficiary_is_new_to_account=0.0, beneficiary_first_seen_days=200,
                    amount_ratio_to_account_p95_30d=10.0)
    assert "ESTABLISHED_PAYEE_NORMAL" in [
        s.code for s in evaluate(RuleContext(tx=tx(), features=normal), ALL_ENABLED)
    ]
    assert "ESTABLISHED_PAYEE_NORMAL" not in [
        s.code for s in evaluate(RuleContext(tx=tx(), features=huge), ALL_ENABLED)
    ]


def test_disabled_rules_do_not_fire():
    """FR-040: enable/disable without a code change."""
    configs = {**ALL_ENABLED, "VELOCITY_BURST_1H": {"enabled": False, "params": {"min_count": 5}}}
    signals = evaluate(RuleContext(tx=tx(), features=features(txn_count_1h_account=99)), configs)
    assert [s.code for s in signals] == []


def test_signals_are_facts_never_points():
    """D11: rules emit named signals with evidence, never a number to add up."""
    signals = evaluate(
        RuleContext(tx=tx(), features=features(txn_count_1h_account=9)), ALL_ENABLED
    )
    for s in signals:
        d = s.as_dict()
        assert set(d) == {"code", "power", "severity", "evidence"}
        assert "score" not in d and "points" not in d and "weight" not in d


# ---------------------------------------------------------------- D77


SEQUENCE = {"ACCOUNT_TAKEOVER_SEQUENCE": {"enabled": True, "params": {"within_hours": 24, "min_failed_logins": 3}}}
SEQUENCE_AS_SEEDED = {"ACCOUNT_TAKEOVER_SEQUENCE": {"enabled": True,
                      "params": {"within_hours": 24, "min_failed_logins": 3, "min_precursors": 2}}}


def quiet(**over) -> dict[str, float]:
    """No precursor anywhere: the lookback caps."""
    f = features()
    f.update({"failed_logins_1h_subject": 0.0, "device_bound_hours": 72.0, "credential_changed_hours": 72.0,
              "sim_changed_hours": 72.0, "payee_added_minutes": 4320.0})
    f.update(over)
    return f


def fired(f: dict[str, float]):
    return [s for s in evaluate(RuleContext(tx=tx(), features=f), SEQUENCE)
            if s.code == "ACCOUNT_TAKEOVER_SEQUENCE"]


def test_takeover_sequence_needs_a_precursor_and_a_new_destination():
    assert not fired(quiet())
    for precursor in ({"device_bound_hours": 0.5}, {"sim_changed_hours": 3.0},
                      {"credential_changed_hours": 23.9}, {"failed_logins_1h_subject": 3.0}):
        f = quiet()
        f.update(precursor)
        signal = fired(f)
        assert signal and set(signal[0].evidence["precursors"]) == set(precursor)
        f["beneficiary_is_new_to_account"] = 0.0  # a payee this account already pays
        assert not fired(f)


def test_takeover_sequence_ignores_old_or_absent_signals():
    f = quiet(device_bound_hours=-1.0)  # no device on the payment
    f.update({"credential_changed_hours": 24.0, "failed_logins_1h_subject": 2.0})
    assert not fired(f)
    f["beneficiary_is_new_to_account"] = -1.0  # no destination at all
    f["sim_changed_hours"] = 1.0
    assert not fired(f)


def test_as_seeded_one_precursor_is_not_enough():
    """D77: a single SIM change before a new payee is ordinary; two signals are the sequence."""
    one = quiet(sim_changed_hours=2.0)
    two = quiet(sim_changed_hours=2.0, device_bound_hours=1.0)
    ctx = lambda f: RuleContext(tx=tx(), features=f)  # noqa: E731
    assert not evaluate(ctx(one), SEQUENCE_AS_SEEDED)
    assert [s.code for s in evaluate(ctx(two), SEQUENCE_AS_SEEDED)] == ["ACCOUNT_TAKEOVER_SEQUENCE"]

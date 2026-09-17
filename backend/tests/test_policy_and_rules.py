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


# ---------------------------------------------------------------- D78


FANIN = {"MULE_INBOUND_FANIN": {"enabled": True, "params": {"min_remitters": 3, "min_count_ratio": 5.0}},
         "VELOCITY_BURST_1H": {"enabled": True, "params": {"min_count": 5}}}


def credit_tx():
    return tx(direction="INBOUND", beneficiary_token=None, remitter_token="acc_sender")


def test_fanin_needs_many_senders_on_an_unusual_day():
    busy = features(distinct_remitters_24h_account=6.0, inbound_count_ratio_24h_vs_daily_mean_30d=12.0)
    trader = features(distinct_remitters_24h_account=40.0, inbound_count_ratio_24h_vs_daily_mean_30d=1.1)
    few = features(distinct_remitters_24h_account=2.0, inbound_count_ratio_24h_vs_daily_mean_30d=30.0)
    code = lambda f: [s.code for s in evaluate(RuleContext(tx=credit_tx(), features=f), FANIN)]  # noqa: E731
    assert code(busy) == ["MULE_INBOUND_FANIN"]
    assert code(trader) == [], "a trader's every-day fan-in is not an event"
    assert code(few) == []


def test_rules_only_apply_to_their_direction():
    f = features(txn_count_1h_account=9.0, distinct_remitters_24h_account=6.0,
                 inbound_count_ratio_24h_vs_daily_mean_30d=12.0)
    on_credit = [s.code for s in evaluate(RuleContext(tx=credit_tx(), features=f), FANIN)]
    on_payment = [s.code for s in evaluate(RuleContext(tx=tx(), features=f), FANIN)]
    assert "VELOCITY_BURST_1H" not in on_credit
    assert "MULE_INBOUND_FANIN" not in on_payment


def test_a_credit_is_decided_without_the_model():
    th = Thresholds(id=1, version=1, p_monitor=0.02, p_review=0.15, p_hold=0.6, alert_min_level="MEDIUM")
    signal = Signal(code="MULE_INBOUND_FANIN", power="ESCALATE", severity="HIGH")
    result = apply(0.99, [signal], th, model_applies=False)
    assert result.trace[0]["source"] == "receiving_side"
    assert result.risk_level == "MEDIUM", "LOW, raised one band by the fan-in, never by a probability"


# ---------------------------------------------------------------- D79


LEG = {"SECOND_LEG_ONWARD_PAYMENT": {"enabled": True, "params": {
    "min_remitters": 3, "min_count_ratio": 5.0, "max_minutes_since_credit": 180, "min_pass_through": 0.5}}}


def test_second_leg_fires_on_the_payment_that_moves_a_fanin_on():
    moving = features(distinct_remitters_24h_account=5.0, inbound_count_ratio_24h_vs_daily_mean_30d=20.0,
                      minutes_since_last_credit=40.0, pass_through_ratio_24h=0.9)
    fired = evaluate(RuleContext(tx=tx(), features=moving), LEG)
    assert [s.code for s in fired] == ["SECOND_LEG_ONWARD_PAYMENT"]
    for change in ({"minutes_since_last_credit": 400.0}, {"pass_through_ratio_24h": 0.2},
                   {"distinct_remitters_24h_account": 2.0}, {"inbound_count_ratio_24h_vs_daily_mean_30d": 1.2}):
        assert not evaluate(RuleContext(tx=tx(), features={**moving, **change}), LEG), change


def test_second_leg_never_fires_on_the_credit_itself():
    moving = features(distinct_remitters_24h_account=5.0, inbound_count_ratio_24h_vs_daily_mean_30d=20.0,
                      minutes_since_last_credit=40.0, pass_through_ratio_24h=0.9)
    assert not evaluate(RuleContext(tx=credit_tx(), features=moving), LEG)


# ---------------------------------------------------------------- D82


def _d82(code: str) -> dict:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ml"))
    from offline_rules import RULE_CONFIGS

    return {code: RULE_CONFIGS[code]}


def codes(f, t=None, rules=None):
    return [s.code for s in evaluate(RuleContext(tx=t or tx(), features=f), rules)]


def test_scam_collection_account_needs_other_payers_and_a_new_destination():
    rules = _d82("SCAM_BENEFICIARY_FANIN")
    scam = features(beneficiary_distinct_senders_24h=3.0, beneficiary_first_seen_days=0.4)
    assert codes(scam, rules=rules) == ["SCAM_BENEFICIARY_FANIN"]
    assert not codes({**scam, "beneficiary_first_seen_days": 400.0}, rules=rules), "a school paid for years"
    assert not codes({**scam, "beneficiary_is_new_to_account": 0.0}, rules=rules), "this customer pays it already"
    assert not codes({**scam, "beneficiary_distinct_senders_24h": 0.0}, rules=rules)
    assert not codes({**scam, "beneficiary_distinct_senders_24h": -1.0}, rules=rules), "no destination"


def test_sim_swap_needs_a_recent_sim_change_and_a_new_destination():
    rules = _d82("SIM_SWAP_TRANSFER")
    drain = features(sim_changed_hours=2.0)
    assert codes(drain, t=tx(channel="USSD"), rules=rules) == ["SIM_SWAP_TRANSFER"]
    assert not codes({**drain, "sim_changed_hours": 72.0}, rules=rules), "not recent: the cap"
    assert not codes({**drain, "beneficiary_is_new_to_account": 0.0}, rules=rules), "a known payee"


def test_dormant_reactivation_needs_months_of_quiet_and_real_money():
    rules = _d82("DORMANT_ACCOUNT_REACTIVATION")
    drain = features(days_since_account_activity=200.0, amount_log10=5.8)
    assert codes(drain, rules=rules) == ["DORMANT_ACCOUNT_REACTIVATION"]
    assert not codes({**drain, "days_since_account_activity": 3.0}, rules=rules)
    assert not codes({**drain, "amount_log10": 3.0}, rules=rules), "airtime from an old account"
    assert not codes({**drain, "days_since_account_activity": -1.0}, rules=rules), "never seen is not dormant"


def test_card_cashout_needs_card_present_a_run_and_a_new_region():
    rules = _d82("CARD_PRESENT_NEW_REGION_CASHOUT")
    atm = tx(channel="ATM", instrument="CARD", beneficiary_token=None, ip_region="NG-KN")
    run = features(card_present_count_1h_account=4.0, region_is_new_to_subject=1.0)
    assert codes(run, t=atm, rules=rules) == ["CARD_PRESENT_NEW_REGION_CASHOUT"]
    assert not codes({**run, "region_is_new_to_subject": 0.0}, t=atm, rules=rules), "at home"
    assert not codes({**run, "card_present_count_1h_account": 1.0}, t=atm, rules=rules), "one withdrawal on a trip"
    assert not codes(run, t=tx(channel="WEB", instrument="CARD", beneficiary_token=None), rules=rules), "not present"


def test_offline_rules_are_the_seeded_rules():
    """D82: every offline script measures ml/offline_rules.py; it must be what ships."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "ml"))
    from offline_rules import RULE_CONFIGS

    spec = importlib.util.spec_from_file_location("seed", root / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    seeded = {code: params for code, _power, _sev, params in seed.RULES}
    assert set(seeded) == set(RULE_CONFIGS)
    for code, params in seeded.items():
        assert RULE_CONFIGS[code]["params"] == params, code

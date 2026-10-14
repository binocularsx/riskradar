"""Pins the fixes in feature spec 1.1.0 (D64).

Each test names a real failure the old behaviour caused, so nobody "tidies" one
of these back into the bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from riskradar.features import compute_features
from riskradar.features.spec import NOT_APPLICABLE, RATIO_CAP
from riskradar.features.types import HistoryBundle, PriorTx, TxView
from riskradar.rules.engine import RuleContext, evaluate

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

SUPPRESSION_ONLY = {
    "ESTABLISHED_PAYEE_NORMAL": {
        "enabled": True,
        "params": {"min_beneficiary_age_days": 60, "max_amount_ratio": 1.0},
    }
}


def tx(**over) -> TxView:
    base = dict(
        transaction_ref="t", occurred_at=NOW, amount_minor=50_000, currency="NGN",
        channel="WEB", instrument="CARD", rail="CARD_SCHEME",
        subject_token="s", account_token="a", beneficiary_token=None,
        account_opened_at=NOW - timedelta(days=400), last_activity_at=NOW - timedelta(days=2),
    )
    base.update(over)
    return TxView(**base)


def history_of_card_payments(n: int = 20) -> HistoryBundle:
    """An account with plenty of ordinary card history — and so no payees."""
    priors = [
        PriorTx(occurred_at=NOW - timedelta(days=1 + i), amount_minor=400_000,
                auth_result="APPROVED", beneficiary_token=None)
        for i in range(n)
    ]
    return HistoryBundle(account=priors, subject=priors, beneficiary_first_seen_at=None)


def test_a_card_payment_has_no_beneficiary_rather_than_a_familiar_one():
    """1.0.0 said 0.0 — 'paid before' — for every card and cash payment."""
    f = compute_features(tx(), history_of_card_payments())
    assert f["beneficiary_is_new_to_account"] == NOT_APPLICABLE
    assert f["beneficiary_first_seen_days"] == NOT_APPLICABLE


def test_a_never_seen_beneficiary_is_brand_new_not_ancient():
    """1.0.0 said 999 days — a long-standing payee — for a brand-new one."""
    f = compute_features(
        tx(instrument="ACCOUNT_TRANSFER", rail="NIP", beneficiary_token="brand-new"),
        HistoryBundle(beneficiary_first_seen_at=None),
    )
    assert f["beneficiary_first_seen_days"] == 0.0
    assert f["beneficiary_is_new_to_account"] == 1.0


def test_missing_account_dates_are_outside_every_real_range():
    """999 sat inside real account ages, which reach 2,629 days."""
    f = compute_features(tx(account_opened_at=None, last_activity_at=None), HistoryBundle())
    assert f["account_age_days"] == NOT_APPLICABLE < 0
    assert f["days_since_account_activity"] == NOT_APPLICABLE < 0


def test_ratios_are_capped():
    """Uncapped values reached 14,045 and 421,388."""
    priors = [PriorTx(occurred_at=NOW - timedelta(days=3), amount_minor=100,
                      auth_result="APPROVED")]
    f = compute_features(tx(amount_minor=10_000_000_00), HistoryBundle(account=priors))
    assert f["amount_ratio_to_account_p95_30d"] == RATIO_CAP
    assert f["approved_value_ratio_24h_vs_daily_mean_30d"] == RATIO_CAP


def test_established_payee_suppression_never_fires_on_a_card_payment():
    """The bug this spec exists for: the risk-LOWERING rule fired on 93.9% of
    card-testing fraud, because 'no payee' read as 'old, familiar payee'."""
    card = tx(amount_minor=300)  # a tiny card probe
    features = compute_features(card, history_of_card_payments())
    signals = evaluate(RuleContext(tx=card, features=features), SUPPRESSION_ONLY)
    assert not [s for s in signals if s.code == "ESTABLISHED_PAYEE_NORMAL"]


def test_the_rule_guard_holds_even_if_a_feature_regresses():
    """Defence in depth: feed the rule the exact 1.0.0 values that fooled it."""
    old_bug = {"beneficiary_is_new_to_account": 0.0,
               "beneficiary_first_seen_days": 999.0,
               "amount_ratio_to_account_p95_30d": 0.01}
    signals = evaluate(RuleContext(tx=tx(), features=old_bug), SUPPRESSION_ONLY)
    assert not signals


def test_established_payee_still_suppresses_a_real_familiar_payee():
    """The fix must not break the rule's actual job."""
    transfer = tx(instrument="ACCOUNT_TRANSFER", rail="NIP",
                  beneficiary_token="landlord", amount_minor=100_000)
    priors = [PriorTx(occurred_at=NOW - timedelta(days=5 + i), amount_minor=150_000,
                      auth_result="APPROVED", beneficiary_token="landlord")
              for i in range(10)]
    h = HistoryBundle(account=priors, subject=priors,
                      beneficiary_first_seen_at=NOW - timedelta(days=200))
    features = compute_features(transfer, h)
    signals = evaluate(RuleContext(tx=transfer, features=features), SUPPRESSION_ONLY)
    assert [s.code for s in signals] == ["ESTABLISHED_PAYEE_NORMAL"]

"""D67: the velocity rule fires on speed to a brand-new destination only."""

from riskradar.rules.engine import RuleContext, velocity_burst_1h
from riskradar.features.spec import NOT_APPLICABLE

PARAMS = {"min_count": 5, "new_destination_days": 1, "no_destination_min_count": 10}


class Tx:
    beneficiary_token = "BEN1"


def ctx(count, first_seen):
    return RuleContext(tx=Tx(), features={
        "txn_count_1h_account": count,
        "beneficiary_first_seen_days": first_seen,
    })


def test_fires_on_burst_to_fresh_destination():
    s = velocity_burst_1h(ctx(6, 0.2), PARAMS)
    assert s is not None and s.evidence["destination_first_seen_days"] == 0.2


def test_never_seen_destination_counts_as_fresh():
    assert velocity_burst_1h(ctx(6, 0.0), PARAMS) is not None


def test_silent_on_burst_to_established_destination():
    # A trader paying suppliers the bank has seen for months.
    assert velocity_burst_1h(ctx(30, 200.0), PARAMS) is None


def test_no_destination_needs_the_higher_bar():
    assert velocity_burst_1h(ctx(9, NOT_APPLICABLE), PARAMS) is None
    s = velocity_burst_1h(ctx(10, NOT_APPLICABLE), PARAMS)
    assert s is not None and s.evidence["no_destination_threshold"] == 10


def test_no_destination_never_reads_as_new_without_the_bar():
    params = {"min_count": 5, "new_destination_days": 1}
    assert velocity_burst_1h(ctx(30, NOT_APPLICABLE), params) is None


def test_silent_below_the_count():
    assert velocity_burst_1h(ctx(4, 0.0), PARAMS) is None


def test_without_the_gate_it_is_speed_only():
    assert velocity_burst_1h(ctx(10, 200.0), {"min_count": 10}) is not None

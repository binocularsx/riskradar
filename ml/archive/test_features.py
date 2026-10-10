"""
Proves the PRD requirement (section 9.4 / 13.2): the same feature-calculation
code produces identical results whether called in a training-style batch loop
or one transaction at a time, the way live scoring would call it.

Run with: pytest tests/test_features.py -v
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml.features import (
    Transaction,
    CustomerState,
    compute_features,
    compute_features_batch,
    update_state,
    FEATURE_NAMES,
)


def make_customer_transactions():
    base = datetime(2026, 1, 10, 9, 0, 0)
    return [
        Transaction("T1", "CUST1", 500_000, base, "MOBILE_APP", "TRANSFER", "NIP", "DEV1", "Lagos"),
        Transaction("T2", "CUST1", 520_000, base + timedelta(hours=2), "MOBILE_APP", "TRANSFER", "NIP", "DEV1", "Lagos"),
        Transaction("T3", "CUST1", 15_000_000, base + timedelta(hours=2, minutes=10), "WEB", "TRANSFER", "NIP", "DEV2", "Kano"),
    ]


def test_feature_vector_has_exact_contract_keys():
    txns = make_customer_transactions()
    state = CustomerState()
    feats = compute_features(txns[0], state)
    assert set(feats.keys()) == set(FEATURE_NAMES), "compute_features output must match FEATURE_NAMES exactly"


def test_first_transaction_has_no_history_markers():
    txns = make_customer_transactions()
    state = CustomerState()
    feats = compute_features(txns[0], state)
    # No prior transactions -> everything history-dependent is "-1" (does not apply)
    assert feats["spending_deviation_score"] == -1.0
    assert feats["time_since_last"] == -1.0
    # A first-ever transaction has a known device_id (just never seen before by
    # definition) - is_ato_risk is correctly 0 here, not -1. -1 is reserved for
    # when device_id itself is missing entirely (see test below).
    assert feats["is_ato_risk"] == 0


def test_ato_risk_does_not_apply_when_device_id_missing():
    base = datetime(2026, 1, 10, 9, 0, 0)
    txn = Transaction("T1", "CUST3", 50_000, base, "USSD", "TRANSFER", "NIP", None, "Lagos")
    feats = compute_features(txn, CustomerState())
    assert feats["is_ato_risk"] == -1
    assert feats["device_seen_count"] == -1


def test_batch_and_incremental_calls_produce_identical_results():
    """The core proof: computing features one-by-one (live-scoring style)
    must give byte-for-byte the same result as the batch helper (training
    style), because both call the exact same underlying function."""
    txns = make_customer_transactions()

    # "Live" style: call compute_features + update_state manually, one at a time
    live_state = CustomerState()
    live_results = []
    for txn in txns:
        live_results.append(compute_features(txn, live_state))
        update_state(live_state, txn)

    # "Training" style: the batch helper
    batch_results = compute_features_batch(txns)

    assert live_results == batch_results, "Live and batch feature computation diverged"


def test_new_device_flags_ato_risk():
    txns = make_customer_transactions()
    results = compute_features_batch(txns)
    # T3 uses DEV2, never seen before, after DEV1 was already established -> flagged
    assert results[2]["is_ato_risk"] == 1
    # T2 reuses DEV1, already seen in T1 -> not flagged
    assert results[1]["is_ato_risk"] == 0


def test_impossible_travel_detected():
    txns = make_customer_transactions()
    results = compute_features_batch(txns)
    # T3 is Lagos -> Kano in 10 minutes: physically impossible
    assert results[2]["geospatial_velocity_anomaly"] == 1


def test_high_risk_state_flag():
    base = datetime(2026, 1, 10, 9, 0, 0)
    txn = Transaction("T1", "CUST2", 50_000, base, "MOBILE_APP", "TRANSFER", "NIP", "DEV1", "Zamfara")
    feats = compute_features(txn, CustomerState())
    assert feats["is_high_risk_state"] == 1


def test_deterministic_across_repeated_runs():
    """Same input, called twice independently, must give the same output -
    required for reproducible demos and debugging (PRD section 13.2)."""
    txns = make_customer_transactions()
    run1 = compute_features_batch(txns)
    run2 = compute_features_batch(txns)
    assert run1 == run2

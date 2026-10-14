"""
RiskRadar shared feature-calculation package.

Per the PRD (section 9.4): "The same feature-calculation code must be used
for both model training and live scoring. Do not calculate 'transactions in
the last hour' one way in a notebook and another way in FastAPI."

This module is that single source of truth. Both `03_baseline_model.ipynb` /
`05_gbm_baseline.ipynb` (training, batch mode) and Chidera's FastAPI backend
(live scoring, one transaction at a time) must call `compute_features()` -
never reimplement any of this logic separately.

FEATURE_VERSION follows the same "does not apply -> -1" convention noted in
the team's reference deliverables document, so a feature that cannot be
computed (e.g. no prior transaction yet) is explicit rather than silently
zero or NaN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import math

FEATURE_VERSION = "1.0.0"

# The 24 features the model is trained and scored on. This list, and only
# this list, defines the model's input contract - see docs/model-contract.md.
# NOTE: merchant_fraud_rate, channel_risk_score, persona_fraud_risk and
# location_fraud_risk are intentionally excluded - they are pre-computed
# "someone else's risk score" columns and are target leakage. See
# docs/updates/ml-progress-update-3.md for the full reasoning.
FEATURE_NAMES = [
    "amount_ngn",
    "spending_deviation_score",
    "is_amount_unusual",
    "velocity_score",
    "txn_count_last_1h",
    "txn_count_last_24h",
    "total_amount_last_1h",
    "time_since_last",
    "avg_gap_between_txns",
    "user_txn_count_total",
    "user_avg_txn_amt",
    "user_std_txn_amt",
    "user_txn_frequency_24h",
    "txn_hour",
    "is_weekend",
    "is_salary_week",
    "is_night_txn",
    "device_seen_count",
    "is_device_shared",
    "is_ato_risk",
    "geo_anomaly_score",
    "geospatial_velocity_anomaly",
    "is_high_risk_state",
    "is_high_risk_lga",
]

# Real-world-grounded high-risk regions. See ml-progress-update-1.md for the
# NIBSS-linked research behind this list.
HIGH_RISK_STATES = {"Zamfara", "Borno", "Nasarawa", "Plateau"}
HIGH_RISK_LGA = {"Lekki"}

# Approximate lat/long centroids for the states/LGAs we currently support,
# used only for the impossible-travel speed calculation. Extend as the
# simulator's location list grows.
LOCATION_COORDS = {
    "Lagos": (6.5244, 3.3792), "Ikeja": (6.6018, 3.3515), "Lekki": (6.4698, 3.5852),
    "Lagos Island": (6.4550, 3.3941), "Oshodi": (6.5558, 3.3474),
    "Ikorodu": (6.6194, 3.5105), "Apapa": (6.4432, 3.3591), "Victoria Island": (6.4281, 3.4219),
    "FCT": (9.0765, 7.3986), "Kano": (12.0022, 8.5920), "Rivers": (4.8156, 7.0498),
    "Edo": (6.3350, 5.6037), "Kaduna": (10.5222, 7.4383), "Oyo": (7.3775, 3.9470),
    "Enugu": (6.5244, 7.5086), "Abia": (5.4527, 7.5248), "Anambra": (6.2209, 6.9370),
}

NGN_SALARY_WEEK_DAYS = {25, 26, 27, 28, 29, 30, 31, 1}  # end/start of month


@dataclass
class Transaction:
    """Raw transaction fields, aligned with the PRD's transaction API contract
    (section 11.2). `amount_minor` is in kobo, per PRD section 8.3."""
    transaction_reference: str
    customer_token: str
    amount_minor: int
    occurred_at: datetime
    channel: str
    instrument: str
    payment_rail: str
    device_id: Optional[str] = None
    location: Optional[str] = None  # state or LGA name


@dataclass
class CustomerState:
    """Rolling history for one customer. In production this is backed by
    Postgres queries (Chidera's side); for training and for the simulator,
    an in-memory instance of this class is enough - the feature LOGIC is
    identical either way, only where the history lives differs."""
    txn_amounts: list = field(default_factory=list)
    txn_timestamps: list = field(default_factory=list)
    known_devices: set = field(default_factory=set)
    last_location: Optional[str] = None
    last_timestamp: Optional[datetime] = None


def _is_high_risk_state(location: Optional[str]) -> int:
    if location is None:
        return -1
    return int(location in HIGH_RISK_STATES)


def _is_high_risk_lga(location: Optional[str]) -> int:
    if location is None:
        return -1
    return int(location in HIGH_RISK_LGA)


def _haversine_km(coord_a, coord_b) -> float:
    lat1, lon1 = coord_a
    lat2, lon2 = coord_b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _geo_anomaly(txn: Transaction, state: CustomerState) -> tuple[float, int]:
    """Returns (geo_anomaly_score, is_impossible_travel). Score is the
    implied travel speed in km/h since the customer's last transaction;
    -1 if there is no prior transaction or either location is unknown.
    Threshold of 900 km/h matches the original rule spec (~ commercial
    flight speed)."""
    if state.last_location is None or txn.location is None:
        return -1.0, -1
    if state.last_location not in LOCATION_COORDS or txn.location not in LOCATION_COORDS:
        return -1.0, -1
    if state.last_timestamp is None:
        return -1.0, -1

    hours = max((txn.occurred_at - state.last_timestamp).total_seconds() / 3600.0, 1e-6)
    distance_km = _haversine_km(LOCATION_COORDS[state.last_location], LOCATION_COORDS[txn.location])
    speed_kmh = distance_km / hours
    return speed_kmh, int(speed_kmh > 900)


def compute_features(txn: Transaction, state: CustomerState) -> dict:
    """
    Compute the full feature vector for one transaction, given the
    customer's rolling history BEFORE this transaction is applied.

    This is the single function both training (called once per historical
    row, in order, per customer) and live scoring (called once per incoming
    transaction) must use. Do not duplicate this logic anywhere else.

    Returns a dict with exactly the keys in FEATURE_NAMES.
    """
    amount_ngn = txn.amount_minor / 100.0
    now = txn.occurred_at

    # --- Behavioural baseline (Rule 2: spending deviation) ---
    if state.txn_amounts:
        avg = sum(state.txn_amounts) / len(state.txn_amounts)
        if len(state.txn_amounts) > 1:
            variance = sum((a - avg) ** 2 for a in state.txn_amounts) / len(state.txn_amounts)
            std = math.sqrt(variance)
        else:
            std = 0.0
        spending_deviation_score = (amount_ngn - avg) / (std + 1)
        is_amount_unusual = int(spending_deviation_score > 2)
    else:
        avg, std = -1.0, -1.0
        spending_deviation_score = -1.0
        is_amount_unusual = -1

    # --- Velocity (Rule 4) ---
    window_1h = [t for t in state.txn_timestamps if now - t <= timedelta(hours=1)]
    window_24h = [t for t in state.txn_timestamps if now - t <= timedelta(hours=24)]
    amounts_1h = state.txn_amounts[-len(window_1h):] if window_1h else []

    txn_count_last_1h = len(window_1h)
    txn_count_last_24h = len(window_24h)
    total_amount_last_1h = sum(amounts_1h)
    velocity_score = txn_count_last_1h  # simple, explainable composite for now

    if state.txn_timestamps:
        time_since_last = (now - state.txn_timestamps[-1]).total_seconds()
    else:
        time_since_last = -1.0

    if len(state.txn_timestamps) > 1:
        gaps = [
            (state.txn_timestamps[i] - state.txn_timestamps[i - 1]).total_seconds()
            for i in range(1, len(state.txn_timestamps))
        ]
        avg_gap_between_txns = sum(gaps) / len(gaps)
    else:
        avg_gap_between_txns = -1.0

    user_txn_count_total = len(state.txn_timestamps)
    user_txn_frequency_24h = txn_count_last_24h

    # --- Temporal (Rule 3) ---
    txn_hour = now.hour
    is_weekend = int(now.weekday() >= 5)
    is_salary_week = int(now.day in NGN_SALARY_WEEK_DAYS)
    is_night_txn = int(txn_hour < 6 or txn_hour > 22)

    # --- Device / ATO (Rule 5, simplified per progress-update-2 finding) ---
    if txn.device_id is None:
        device_seen_count, is_device_shared, is_ato_risk = -1, -1, -1
    else:
        device_seen_count = 1 if txn.device_id in state.known_devices else 0
        is_device_shared = -1  # not computable without cross-customer device data
        is_ato_risk = int(txn.device_id not in state.known_devices and user_txn_count_total > 0)

    # --- Geographic (Rules 1 & 6) ---
    geo_anomaly_score, geospatial_velocity_anomaly = _geo_anomaly(txn, state)
    is_high_risk_state = _is_high_risk_state(txn.location)
    is_high_risk_lga = _is_high_risk_lga(txn.location)

    return {
        "amount_ngn": amount_ngn,
        "spending_deviation_score": spending_deviation_score,
        "is_amount_unusual": is_amount_unusual,
        "velocity_score": velocity_score,
        "txn_count_last_1h": txn_count_last_1h,
        "txn_count_last_24h": txn_count_last_24h,
        "total_amount_last_1h": total_amount_last_1h,
        "time_since_last": time_since_last,
        "avg_gap_between_txns": avg_gap_between_txns,
        "user_txn_count_total": user_txn_count_total,
        "user_avg_txn_amt": avg,
        "user_std_txn_amt": std,
        "user_txn_frequency_24h": user_txn_frequency_24h,
        "txn_hour": txn_hour,
        "is_weekend": is_weekend,
        "is_salary_week": is_salary_week,
        "is_night_txn": is_night_txn,
        "device_seen_count": device_seen_count,
        "is_device_shared": is_device_shared,
        "is_ato_risk": is_ato_risk,
        "geo_anomaly_score": geo_anomaly_score,
        "geospatial_velocity_anomaly": geospatial_velocity_anomaly,
        "is_high_risk_state": is_high_risk_state,
        "is_high_risk_lga": is_high_risk_lga,
    }


def update_state(state: CustomerState, txn: Transaction, max_history: int = 500) -> None:
    """Apply a transaction to the customer's rolling state AFTER features
    have been computed for it. Must be called in strict chronological order
    per customer, in both training and live scoring."""
    amount_ngn = txn.amount_minor / 100.0
    state.txn_amounts.append(amount_ngn)
    state.txn_timestamps.append(txn.occurred_at)
    if len(state.txn_amounts) > max_history:
        state.txn_amounts.pop(0)
        state.txn_timestamps.pop(0)
    if txn.device_id:
        state.known_devices.add(txn.device_id)
    state.last_location = txn.location
    state.last_timestamp = txn.occurred_at


def compute_features_batch(transactions: list[Transaction]) -> list[dict]:
    """
    Batch/training-mode helper: given a customer's transactions in
    chronological order, returns one feature dict per transaction, using
    EXACTLY the same per-transaction logic as live scoring. This is what
    proves training and live are not two different implementations - see
    tests/test_features.py.
    """
    state = CustomerState()
    results = []
    for txn in transactions:
        feats = compute_features(txn, state)
        results.append(feats)
        update_state(state, txn)
    return results

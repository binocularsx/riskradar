"""Feature specification.

D15 — the single most likely way this model fails is train/serve skew: the same
feature computed twice, in pandas and in SQL, by two people, diverging quietly
while offline metrics keep looking fine.

The control is this package. One set of pure functions taking a transaction plus
its history and returning a feature vector. Training feeds it a dataframe; the
scoring worker feeds it a Postgres query. Same functions, different data access.

D22 — nothing here reads a mutable table. Account context arrives already
stamped on the transaction as timestamps, and age/dormancy are derived from
``occurred_at``, which makes every feature point-in-time correct by construction.
An as-of join would be a second way to get this wrong; there isn't one.
"""

from __future__ import annotations

# Bumped whenever the meaning of any feature changes. Every decision record
# stores the version that produced it, so a decision is reproducible (G4).
FEATURE_SPEC_VERSION = "1.0.0"

# D25 cut this from ~15 to 12 to pay for the identity work in D19a/D22.
FEATURE_NAMES: tuple[str, ...] = (
    "amount_log10",
    "amount_ratio_to_account_p95_30d",
    "txn_count_1h_account",
    "approved_value_ratio_24h_vs_daily_mean_30d",
    "failed_attempts_1h_account",
    "decline_rate_24h_account",
    "distinct_beneficiaries_1h_account",
    "beneficiary_is_new_to_account",
    "beneficiary_first_seen_days",
    "device_is_new_to_subject",
    "account_age_days",
    "days_since_account_activity",
)

# Lookback windows the history loaders must honour. Both data-access paths read
# these constants, so a change cannot apply to one path and not the other.
ACCOUNT_HISTORY_DAYS = 30
SUBJECT_HISTORY_DAYS = 30
BENEFICIARY_LOOKBACK_DAYS = 90

# Sentinel for "we have never seen this before". A large finite number rather
# than NaN: tree models split on it cleanly and it survives JSON round-tripping
# into the decision's feature snapshot.
NEVER_SEEN_DAYS = 999.0

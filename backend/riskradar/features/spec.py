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
FEATURE_SPEC_VERSION = "1.2.0"  # D77: signals before the money moves

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
    # D77 (WP-02): what happened to the customer before this payment. Read from
    # events, not transactions; account takeover announces itself here.
    "failed_logins_1h_subject",
    "device_bound_hours",
    "credential_changed_hours",
    "sim_changed_hours",
    "payee_added_minutes",
)

# Lookback windows the history loaders must honour. Both data-access paths read
# these constants, so a change cannot apply to one path and not the other.
ACCOUNT_HISTORY_DAYS = 30
SUBJECT_HISTORY_DAYS = 30
BENEFICIARY_LOOKBACK_DAYS = 90

# D77. Non-payment events are read for the customer over this window. The
# "how long ago" features cap at it: a device bound a month ago and a device
# bound three days ago are both simply "not recently", and an uncapped value
# would teach the model the age of the simulation rather than the customer.
EVENT_LOOKBACK_HOURS = 72

# D64. "This does not apply" - no beneficiary on a card payment, no opening
# date on an account we know nothing about.
#
# Version 1.0.0 used 999.0 for this, and it was wrong twice over. Real account
# ages reach 2,629 days, so 999 sat *inside* the valid range and read as a
# 2.7-year-old account. And for beneficiaries it read as "first seen 999 days
# ago" - a long-standing, trusted payee - the opposite of the truth. Combined
# with a second bug, that let the rule that *lowers* risk fire on 93.9% of
# card-testing fraud.
#
# -1 is outside every real range (days and counts are never negative), so no
# threshold rule can mistake it for a value, and trees still split on it
# cleanly. A beneficiary that exists but has never been seen before is not
# "not applicable" - it is brand new, and gets 0.
NOT_APPLICABLE = -1.0

# D64. Ratios are capped. "100 times your usual" is already as extreme as the
# signal gets; uncapped values reached 14,045 and 421,388, which made the
# explanation bars meaningless and would break any drift statistic.
RATIO_CAP = 100.0

# D72, D77. A decision is made on a payment: that is where money is at risk and
# where the alert budget is spent. The other types are read by features, as
# history before the payment, but do not get a decision of their own.
SCORED_EVENT_TYPES = frozenset({"PAYMENT"})
FEATURE_EVENT_TYPES = frozenset({"LOGIN", "DEVICE_BOUND", "CREDENTIAL_CHANGED", "PAYEE_ADDED", "SIM_CHANGED"})

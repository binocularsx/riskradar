"""The feature functions themselves.

Plain English
-------------
This file answers one question, twelve times: *is this transaction unusual for
this customer?*

Each function takes the transaction being examined plus a list of that
account's earlier transactions, and returns a single number. "How many
transactions in the last hour?" "How big is this compared to their usual
ceiling?" "Have they ever paid this person before?" Nothing here knows about
fraud — these are just measurements. Deciding what the measurements mean is
somebody else's job (``policy/engine.py``).

The functions are deliberately dull and self-contained: no database, no
network, no clock. That is what makes it possible to run exactly the same code
during training and during live scoring, which is the single most important
property in this system.

Pure. No database handle, no dataframe, no clock. Everything they need arrives in
``TxView`` and ``HistoryBundle``, which is what makes the equality test in
``tests/test_train_serve_equality.py`` possible — and that test is the highest-
value test in the suite (D15).

Two rules hold throughout:

* **Money is integer kobo until the last moment.** Aggregates are summed as
  integers; only the final ratio becomes a float. Floating-point money is the
  most common production defect in financial software and every aggregate
  inherits the error (D9a).
* **Only APPROVED transactions move value.** A declined probe and a reversed
  transfer are events, not money (D21). Value features filter; count features
  do not, because the attempts themselves are the signal.
"""

from __future__ import annotations

import math
from datetime import timedelta

from .spec import FEATURE_NAMES, NOT_APPLICABLE, RATIO_CAP
from .types import HistoryBundle, PriorTx, TxView


def _within(priors: list[PriorTx], tx: TxView, hours: float) -> list[PriorTx]:
    """Priors strictly before this transaction, inside the window.

    ``occurred_at`` only (D9b). The strict ``<`` matters: a transaction must
    never be part of its own history, or every count is off by one and the model
    learns an offset instead of a behaviour.
    """
    cutoff = tx.occurred_at - timedelta(hours=hours)
    return [p for p in priors if cutoff <= p.occurred_at < tx.occurred_at]


def _approved(priors: list[PriorTx]) -> list[PriorTx]:
    return [p for p in priors if p.auth_result == "APPROVED"]


def _percentile(values: list[int], pct: float) -> float:
    """Nearest-rank percentile.

    Chosen over linear interpolation because both data-access paths must agree
    exactly, and nearest-rank has no floating-point tie-breaking to diverge on.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


# ---------------------------------------------------------------------------
# Individual features
# ---------------------------------------------------------------------------


def amount_log10(tx: TxView, _h: HistoryBundle) -> float:
    """Log-scaled naira. Raw kobo spans six orders of magnitude; models hate that."""
    naira = tx.amount_minor / 100.0
    return round(math.log10(1.0 + naira), 6)


def amount_ratio_to_account_p95_30d(tx: TxView, h: HistoryBundle) -> float:
    """How far above this account's normal ceiling is this?

    The 95th percentile rather than the mean: a single historical outlier should
    not license every future one.
    """
    priors = _approved(_within(h.account, tx, 24 * 30))
    p95 = _percentile([p.amount_minor for p in priors], 95.0)
    if p95 <= 0:
        # No approved history. Ratio is undefined, not infinite — a brand-new
        # account is handled by account_age_days, not by a fabricated spike.
        return 0.0
    return round(min(tx.amount_minor / p95, RATIO_CAP), 6)


def txn_count_1h_account(tx: TxView, h: HistoryBundle) -> float:
    """Burst velocity. Attempts, not just successes — the probing is the signal."""
    return float(len(_within(h.account, tx, 1)))


def approved_value_ratio_24h_vs_daily_mean_30d(tx: TxView, h: HistoryBundle) -> float:
    """Is today's outflow unlike this account's normal day?

    Includes the transaction under assessment, because the question is "would
    today be abnormal if this went through".
    """
    day = _approved(_within(h.account, tx, 24))
    month = _approved(_within(h.account, tx, 24 * 30))
    if not month:
        return 0.0
    today_minor = sum(p.amount_minor for p in day) + tx.amount_minor
    daily_mean_minor = sum(p.amount_minor for p in month) / 30.0
    if daily_mean_minor <= 0:
        return 0.0
    return round(min(today_minor / daily_mean_minor, RATIO_CAP), 6)


def failed_attempts_1h_account(tx: TxView, h: HistoryBundle) -> float:
    """D21a. Insufficient funds and limit rejections cluster around an ATO drain."""
    priors = _within(h.account, tx, 1)
    return float(sum(1 for p in priors if p.auth_result in ("DECLINED", "FAILED")))


def decline_rate_24h_account(tx: TxView, h: HistoryBundle) -> float:
    """D21a. Card testing is a decline-heavy pattern by definition."""
    priors = _within(h.account, tx, 24)
    if not priors:
        return 0.0
    declined = sum(1 for p in priors if p.auth_result in ("DECLINED", "FAILED"))
    return round(declined / len(priors), 6)


def distinct_beneficiaries_1h_account(tx: TxView, h: HistoryBundle) -> float:
    """Mule fan-out: one account, many destinations, minutes apart."""
    priors = _within(h.account, tx, 1)
    seen = {p.beneficiary_token for p in priors if p.beneficiary_token}
    if tx.beneficiary_token:
        seen.add(tx.beneficiary_token)
    return float(len(seen))


def beneficiary_is_new_to_account(tx: TxView, h: HistoryBundle) -> float:
    """Has this account ever paid this destination before?

    D64: a payment with no beneficiary - every card and cash payment - used to
    return 0.0, "paid before". That told the established-payee rule these were
    familiar destinations, and it suppressed risk on 93.9% of card-testing fraud.
    There is no destination to be new or familiar, so the answer is "not
    applicable".
    """
    if not tx.beneficiary_token:
        return NOT_APPLICABLE
    priors = _within(h.account, tx, 24 * 90)
    return 0.0 if any(p.beneficiary_token == tx.beneficiary_token for p in priors) else 1.0


def beneficiary_first_seen_days(tx: TxView, h: HistoryBundle) -> float:
    """How new is this destination to the bank's traffic as a whole?

    Deliberately derived from our own history rather than from a stamped
    beneficiary account age: for an outbound NIP transfer the beneficiary is at
    another institution and we could never know its open date (§9.1). This is the
    honest version of the same signal, and it works for both rails.
    """
    if not tx.beneficiary_token:
        return NOT_APPLICABLE
    if h.beneficiary_first_seen_at is None:
        # Present, and never seen before: brand new. Version 1.0.0 returned 999
        # here, which read as a long-standing payee - the opposite of the truth.
        return 0.0
    delta = tx.occurred_at - h.beneficiary_first_seen_at
    return round(max(0.0, delta.total_seconds() / 86400.0), 6)


def device_is_new_to_subject(tx: TxView, h: HistoryBundle) -> float:
    """Device novelty at *customer* level, not account level (D19).

    An attacker who compromises a customer reaches all of their accounts from the
    same handset; scoping this per-account would call it new every time and
    reward the attacker for spreading out.
    """
    if not tx.device_token:
        return 0.0
    priors = _within(h.subject, tx, 24 * 30)
    return 0.0 if any(p.device_token == tx.device_token for p in priors) else 1.0


def account_age_days(tx: TxView, _h: HistoryBundle) -> float:
    """D20b, D22a. Derived here from a stamped timestamp, never joined.

    Storing the timestamp and deriving the age is what makes this point-in-time
    correct: an age column would change every night and silently rewrite history.
    """
    if tx.account_opened_at is None:
        return NOT_APPLICABLE
    delta = tx.occurred_at - tx.account_opened_at
    return round(max(0.0, delta.total_seconds() / 86400.0), 6)


def days_since_account_activity(tx: TxView, _h: HistoryBundle) -> float:
    """D20b. Dormant-then-active is the account-takeover shape."""
    if tx.last_activity_at is None:
        return NOT_APPLICABLE
    delta = tx.occurred_at - tx.last_activity_at
    return round(max(0.0, delta.total_seconds() / 86400.0), 6)


# ---------------------------------------------------------------------------
# The vector
# ---------------------------------------------------------------------------

_FUNCTIONS = {
    "amount_log10": amount_log10,
    "amount_ratio_to_account_p95_30d": amount_ratio_to_account_p95_30d,
    "txn_count_1h_account": txn_count_1h_account,
    "approved_value_ratio_24h_vs_daily_mean_30d": approved_value_ratio_24h_vs_daily_mean_30d,
    "failed_attempts_1h_account": failed_attempts_1h_account,
    "decline_rate_24h_account": decline_rate_24h_account,
    "distinct_beneficiaries_1h_account": distinct_beneficiaries_1h_account,
    "beneficiary_is_new_to_account": beneficiary_is_new_to_account,
    "beneficiary_first_seen_days": beneficiary_first_seen_days,
    "device_is_new_to_subject": device_is_new_to_subject,
    "account_age_days": account_age_days,
    "days_since_account_activity": days_since_account_activity,
}

assert set(_FUNCTIONS) == set(FEATURE_NAMES), "feature registry disagrees with the spec"


def compute_features(tx: TxView, history: HistoryBundle) -> dict[str, float]:
    """The one entry point. Both paths call this; neither may bypass it."""
    return {name: _FUNCTIONS[name](tx, history) for name in FEATURE_NAMES}


def to_vector(features: dict[str, float]) -> list[float]:
    """Deterministic ordering. Never rely on dict insertion order across a wire."""
    return [float(features[name]) for name in FEATURE_NAMES]

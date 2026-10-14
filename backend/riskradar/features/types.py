"""The shapes the feature functions consume.

Deliberately plain. Both data-access paths — SQL in the worker, pandas in
training — build these same structures, and :mod:`riskradar.features.compute`
never learns which one it was handed. That is the whole train/serve-skew control
(D15) expressed as a type boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class TxView:
    """The transaction being scored.

    Money stays in integer kobo (D9a). ``occurred_at`` is the only time used for
    behaviour (D9b) — scoring a batch of late arrivals on ``ingested_at``
    manufactures a fake velocity spike and floods the queue.
    """

    transaction_ref: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    channel: str
    instrument: str
    rail: str
    subject_token: str
    account_token: str
    beneficiary_token: str | None = None
    device_token: str | None = None
    ip_region: str | None = None
    merchant_category: str | None = None
    auth_result: str = "APPROVED"
    decline_reason: str | None = None
    # D22: stamped at the ingestion boundary, immutable, point-in-time correct.
    account_opened_at: datetime | None = None
    last_activity_at: datetime | None = None
    product_type: str | None = None
    origin_sol_id: str | None = None
    # D78: OUTBOUND money leaves this account; INBOUND arrives in it from
    # ``remitter_token``, and ``beneficiary_token`` is empty.
    direction: str = "OUTBOUND"
    remitter_token: str | None = None


@dataclass(frozen=True)
class PriorTx:
    """One earlier transaction. The subset of columns any feature actually reads."""

    occurred_at: datetime
    amount_minor: int
    auth_result: str
    beneficiary_token: str | None = None
    device_token: str | None = None
    # D82: where and how, for region novelty and card-present counts.
    ip_region: str | None = None
    channel: str | None = None
    instrument: str | None = None


@dataclass(frozen=True)
class PriorEvent:
    """One earlier non-payment event for the same customer (D77).

    Only the facts a feature reads: whether a login failed, which device was
    bound, which payee was enrolled. Tokens, never raw identifiers.
    """

    occurred_at: datetime
    event_type: str
    device_token: str | None = None
    login_result: str | None = None
    binding: str | None = None
    beneficiary_token: str | None = None


@dataclass
class HistoryBundle:
    """Everything the feature functions may look at, and nothing else.

    Three slices rather than one, because they have different keys and different
    windows. Assembling this is the *only* job that differs between training and
    serving.
    """

    # Prior transactions on the same account (D19: the baseline is per-account,
    # because a salary current account and a dormant domiciliary account have
    # nothing in common).
    account: list[PriorTx] = field(default_factory=list)
    # Prior transactions for the same customer across all their accounts
    # (D19: the subject is the customer). Used for device novelty.
    subject: list[PriorTx] = field(default_factory=list)
    # When this beneficiary was first seen anywhere in our traffic, or None.
    beneficiary_first_seen_at: datetime | None = None
    # D77: the customer's non-payment events in the lookback window.
    events: list[PriorEvent] = field(default_factory=list)
    # D78: credits into this account in the lookback window. A credit is a
    # PriorTx whose ``beneficiary_token`` holds the remitter, the other party.
    credits: list[PriorTx] = field(default_factory=list)
    # D82: how many *other* customers paid this transaction's destination in the
    # day before it. Bank-wide, so it is a count, not rows.
    beneficiary_other_senders_24h: int = 0

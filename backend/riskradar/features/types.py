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


@dataclass(frozen=True)
class PriorTx:
    """One earlier transaction. The subset of columns any feature actually reads."""

    occurred_at: datetime
    amount_minor: int
    auth_result: str
    beneficiary_token: str | None = None
    device_token: str | None = None


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

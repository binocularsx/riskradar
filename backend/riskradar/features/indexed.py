"""Indexed history for the training path.

``load_history_frame`` masks the whole dataframe once per transaction, which is
O(n) per row and therefore O(n²) over a corpus. At 500,000 transactions that is
not slow, it is impossible — and the honest consequence would have been someone
quietly writing a second, faster implementation of the features for training,
which is exactly the train/serve skew D15 exists to prevent.

So the *data access* gets an index and the *feature functions* do not change.
This class builds per-account and per-subject sorted histories once, then serves
each transaction's slice by binary search. It returns the same ``HistoryBundle``
as the SQL loader and the naive frame loader, and the equality test covers all
three paths against each other.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from .spec import ACCOUNT_HISTORY_DAYS, EVENT_LOOKBACK_HOURS, FEATURE_EVENT_TYPES, SUBJECT_HISTORY_DAYS
from .types import HistoryBundle, PriorEvent, PriorTx, TxView


class PandasHistorySource:
    """Pre-indexed history over a corpus of transactions.

    Accepts a pandas DataFrame or any iterable of mappings, so training,
    evaluation and the fixture builder all share one path.
    """

    def __init__(self, rows: Any, events: Any = None) -> None:
        self._account_times: dict[str, list[datetime]] = defaultdict(list)
        self._account_rows: dict[str, list[PriorTx]] = defaultdict(list)
        self._subject_times: dict[str, list[datetime]] = defaultdict(list)
        self._subject_rows: dict[str, list[PriorTx]] = defaultdict(list)
        self._beneficiary_first: dict[str, list[datetime]] = defaultdict(list)

        for row in _iter_rows(rows):
            prior = PriorTx(
                occurred_at=row["occurred_at"],
                amount_minor=int(row["amount_minor"]),
                auth_result=row["auth_result"],
                beneficiary_token=_clean(row.get("beneficiary_token")),
                device_token=_clean(row.get("device_token")),
            )
            self._account_rows[row["account_token"]].append(prior)
            self._subject_rows[row["subject_token"]].append(prior)
            if prior.beneficiary_token:
                self._beneficiary_first[prior.beneficiary_token].append(prior.occurred_at)

        for key, items in self._account_rows.items():
            items.sort(key=lambda p: p.occurred_at)
            self._account_times[key] = [p.occurred_at for p in items]
        for key, items in self._subject_rows.items():
            items.sort(key=lambda p: p.occurred_at)
            self._subject_times[key] = [p.occurred_at for p in items]
        for key, times in self._beneficiary_first.items():
            times.sort()

        # D77: the customer's non-payment events, indexed the same way.
        self._event_times: dict[str, list[datetime]] = defaultdict(list)
        self._event_rows: dict[str, list[PriorEvent]] = defaultdict(list)
        if events is not None:
            from .sources import event_from_row

            for row in _iter_rows(events):
                if row["event_type"] in FEATURE_EVENT_TYPES:
                    self._event_rows[row["subject_token"]].append(event_from_row(row))
            for key, items in self._event_rows.items():
                items.sort(key=lambda e: e.occurred_at)
                self._event_times[key] = [e.occurred_at for e in items]

    def _slice(
        self,
        times: list[datetime],
        rows: list[PriorTx],
        start: datetime,
        end: datetime,
    ) -> list[PriorTx]:
        """Rows in ``[start, end)``.

        ``bisect_left`` on the end bound is what implements the strict ``<`` the
        SQL path uses — a transaction is never part of its own history.
        """
        lo = bisect.bisect_left(times, start)
        hi = bisect.bisect_left(times, end)
        return rows[lo:hi]

    def load(self, tx: TxView) -> HistoryBundle:
        account = self._slice(
            self._account_times.get(tx.account_token, []),
            self._account_rows.get(tx.account_token, []),
            tx.occurred_at - timedelta(days=ACCOUNT_HISTORY_DAYS),
            tx.occurred_at,
        )
        subject = self._slice(
            self._subject_times.get(tx.subject_token, []),
            self._subject_rows.get(tx.subject_token, []),
            tx.occurred_at - timedelta(days=SUBJECT_HISTORY_DAYS),
            tx.occurred_at,
        )

        first_seen = None
        if tx.beneficiary_token:
            times = self._beneficiary_first.get(tx.beneficiary_token)
            if times and times[0] < tx.occurred_at:
                first_seen = times[0]

        events = self._slice(
            self._event_times.get(tx.subject_token, []),
            self._event_rows.get(tx.subject_token, []),
            tx.occurred_at - timedelta(hours=EVENT_LOOKBACK_HOURS),
            tx.occurred_at,
        )
        return HistoryBundle(
            account=account, subject=subject, beneficiary_first_seen_at=first_seen, events=events
        )


def _iter_rows(rows: Any) -> Iterable[dict[str, Any]]:
    if hasattr(rows, "to_dict"):  # pandas DataFrame
        return rows.to_dict("records")
    return rows


def _clean(value: Any) -> Any:
    """None/NaN-safe.

    pandas turns SQL NULL into NaN, and NaN != NaN, which would make
    ``beneficiary_is_new_to_account`` disagree with the SQL path on every cash
    withdrawal.
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    return value

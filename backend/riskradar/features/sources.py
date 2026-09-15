"""The two data-access paths.

Plain English
-------------
The feature functions need a customer's transaction history. This file fetches
it — twice, in two different ways.

When the system is running live, history comes from the database. When somebody
is training the model, it comes from a big table loaded in memory. Those are
genuinely different jobs, so they are two functions here, and they are the
*only* two places in the whole codebase where that difference exists.

Both hand back the identical shape, so the feature functions cannot tell which
one they were given. If they could, the two paths would slowly drift apart and
the model would start seeing slightly different numbers in production than it
learned from — a failure that is almost invisible until it is expensive.

This module is the *only* place training and serving differ. Everything below
returns the identical ``HistoryBundle``, and :mod:`riskradar.features.compute`
cannot tell which one produced it.

If a feature ever needs data neither loader supplies, both get extended
together, in this file, or the equality test fails — which is exactly the
failure mode we want, loudly, at test time rather than silently in production.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .spec import (
    ACCOUNT_HISTORY_DAYS,
    CREDIT_HISTORY_DAYS,
    BENEFICIARY_LOOKBACK_DAYS,
    EVENT_LOOKBACK_HOURS,
    FEATURE_EVENT_TYPES,
    SUBJECT_HISTORY_DAYS,
)
from .types import HistoryBundle, PriorEvent, PriorTx, TxView

_PRIOR_COLUMNS = "occurred_at, amount_minor, auth_result, beneficiary_token, device_token"


# ---------------------------------------------------------------------------
# Serving path: PostgreSQL
# ---------------------------------------------------------------------------

_ACCOUNT_SQL = f"""
    SELECT {_PRIOR_COLUMNS}
      FROM transactions
     WHERE account_token = %(account)s
       AND direction = 'OUTBOUND'
       AND occurred_at <  %(now)s
       AND occurred_at >= %(from)s
"""

_SUBJECT_SQL = f"""
    SELECT {_PRIOR_COLUMNS}
      FROM transactions
     WHERE subject_token = %(subject)s
       AND direction = 'OUTBOUND'
       AND occurred_at <  %(now)s
       AND occurred_at >= %(from)s
"""

# D78. Credits into the account; the remitter stands where the beneficiary
# stands on a payment, the other party.
_CREDITS_SQL = """
    SELECT occurred_at, amount_minor, auth_result, remitter_token AS beneficiary_token, device_token
      FROM transactions
     WHERE account_token = %(account)s
       AND direction = 'INBOUND'
       AND occurred_at <  %(now)s
       AND occurred_at >= %(from)s
"""

# D77. Tokens only: detail was tokenised at the boundary.
_EVENTS_SQL = """
    SELECT occurred_at, event_type::text AS event_type, device_token,
           detail->>'result' AS login_result, detail->>'binding' AS binding,
           detail->>'beneficiary_token' AS beneficiary_token
      FROM events
     WHERE subject_token = %(subject)s
       AND event_type::text = ANY(%(types)s)
       AND occurred_at <  %(now)s
       AND occurred_at >= %(from)s
"""

_BENEFICIARY_SQL = """
    SELECT min(occurred_at) AS first_seen
      FROM transactions
     WHERE beneficiary_token = %(beneficiary)s
       AND direction = 'OUTBOUND'
       AND occurred_at < %(now)s
"""


def _row_to_prior(row: Any) -> PriorTx:
    return PriorTx(
        occurred_at=row["occurred_at"],
        amount_minor=int(row["amount_minor"]),
        auth_result=row["auth_result"],
        beneficiary_token=row["beneficiary_token"],
        device_token=row["device_token"],
    )


def load_history_sql(conn: Any, tx: TxView) -> HistoryBundle:
    """Assemble history from Postgres. Used by the scoring worker.

    Three indexed range scans. Note there is no join to ``accounts``: account
    context arrives stamped on the transaction (D22), so this cannot read a
    mutable table even by accident.
    """
    with conn.cursor() as cur:
        cur.execute(
            _ACCOUNT_SQL,
            {
                "account": tx.account_token,
                "now": tx.occurred_at,
                "from": tx.occurred_at - timedelta(days=ACCOUNT_HISTORY_DAYS),
            },
        )
        account = [_row_to_prior(r) for r in cur.fetchall()]

        cur.execute(
            _SUBJECT_SQL,
            {
                "subject": tx.subject_token,
                "now": tx.occurred_at,
                "from": tx.occurred_at - timedelta(days=SUBJECT_HISTORY_DAYS),
            },
        )
        subject = [_row_to_prior(r) for r in cur.fetchall()]

        first_seen = None
        if tx.beneficiary_token:
            cur.execute(
                _BENEFICIARY_SQL,
                {"beneficiary": tx.beneficiary_token, "now": tx.occurred_at},
            )
            row = cur.fetchone()
            first_seen = row["first_seen"] if row else None

        cur.execute(
            _EVENTS_SQL,
            {
                "subject": tx.subject_token,
                "types": sorted(FEATURE_EVENT_TYPES),
                "now": tx.occurred_at,
                "from": tx.occurred_at - timedelta(hours=EVENT_LOOKBACK_HOURS),
            },
        )
        events = [event_from_row(r) for r in cur.fetchall()]

        cur.execute(
            _CREDITS_SQL,
            {
                "account": tx.account_token,
                "now": tx.occurred_at,
                "from": tx.occurred_at - timedelta(days=CREDIT_HISTORY_DAYS),
            },
        )
        credits = [_row_to_prior(r) for r in cur.fetchall()]

    return HistoryBundle(
        account=account, subject=subject, beneficiary_first_seen_at=first_seen, events=events,
        credits=credits,
    )


def event_from_row(row: Any) -> PriorEvent:
    """One shape for an event row, whichever path read it."""
    return PriorEvent(
        occurred_at=row["occurred_at"],
        event_type=row["event_type"],
        device_token=_none(row.get("device_token")),
        login_result=_none(row.get("login_result")),
        binding=_none(row.get("binding")),
        beneficiary_token=_none(row.get("beneficiary_token")),
    )


def _none(value: Any) -> Any:
    return value if _present(value) else None


# ---------------------------------------------------------------------------
# Training path: pandas
# ---------------------------------------------------------------------------


def load_history_frame(frame: Any, tx: TxView, events: Any = None) -> HistoryBundle:
    """Assemble history from a dataframe. Used by training and evaluation.

    ``frame`` carries the same columns as ``transactions``; ``events`` (D77) the
    columns the events query returns plus ``subject_token``. The windows come
    from the same constants the SQL path uses, so a change to a lookback cannot
    apply to one path and not the other.
    """
    occurred = frame["occurred_at"]
    before = occurred < tx.occurred_at
    # D78: a frame without a direction column predates credits; all outbound.
    inbound = (frame["direction"] == "INBOUND") if "direction" in frame else (occurred != occurred)
    outbound = ~inbound

    account_mask = (
        before
        & outbound
        & (frame["account_token"] == tx.account_token)
        & (occurred >= tx.occurred_at - timedelta(days=ACCOUNT_HISTORY_DAYS))
    )
    subject_mask = (
        before
        & outbound
        & (frame["subject_token"] == tx.subject_token)
        & (occurred >= tx.occurred_at - timedelta(days=SUBJECT_HISTORY_DAYS))
    )

    def to_priors(mask: Any) -> list[PriorTx]:
        sub = frame.loc[mask, list(_PRIOR_COLUMNS.replace(" ", "").split(","))]
        return [
            PriorTx(
                occurred_at=rec[0],
                amount_minor=int(rec[1]),
                auth_result=rec[2],
                beneficiary_token=rec[3] if _present(rec[3]) else None,
                device_token=rec[4] if _present(rec[4]) else None,
            )
            for rec in sub.itertuples(index=False, name=None)
        ]

    first_seen = None
    if tx.beneficiary_token:
        ben_mask = before & outbound & (frame["beneficiary_token"] == tx.beneficiary_token)
        if bool(ben_mask.any()):
            first_seen = frame.loc[ben_mask, "occurred_at"].min()

    prior_events: list[PriorEvent] = []
    if events is not None and len(events):
        eo = events["occurred_at"]
        ev_mask = (
            (events["subject_token"] == tx.subject_token)
            & events["event_type"].isin(list(FEATURE_EVENT_TYPES))
            & (eo < tx.occurred_at)
            & (eo >= tx.occurred_at - timedelta(hours=EVENT_LOOKBACK_HOURS))
        )
        prior_events = [event_from_row(r) for r in events.loc[ev_mask].to_dict("records")]

    credit_mask = (
        before
        & inbound
        & (frame["account_token"] == tx.account_token)
        & (occurred >= tx.occurred_at - timedelta(days=CREDIT_HISTORY_DAYS))
    )
    credits = []
    if bool(credit_mask.any()):
        sub = frame.loc[credit_mask]
        credits = [
            PriorTx(occurred_at=r["occurred_at"], amount_minor=int(r["amount_minor"]), auth_result=r["auth_result"],
                    beneficiary_token=r["remitter_token"] if _present(r.get("remitter_token")) else None,
                    device_token=r["device_token"] if _present(r.get("device_token")) else None)
            for r in sub.to_dict("records")
        ]

    _ = BENEFICIARY_LOOKBACK_DAYS  # documented window; first-seen is unbounded by design
    return HistoryBundle(
        account=to_priors(account_mask),
        subject=to_priors(subject_mask),
        beneficiary_first_seen_at=first_seen,
        events=prior_events,
        credits=credits,
    )


def _present(value: Any) -> bool:
    """None/NaN-safe truthiness.

    pandas turns a SQL NULL into NaN, and ``NaN == NaN`` is False — which would
    make ``beneficiary_is_new_to_account`` disagree between the two paths for
    every cash withdrawal. Normalising here is what keeps the equality test green.
    """
    if value is None:
        return False
    try:
        return not (isinstance(value, float) and value != value)
    except TypeError:  # pragma: no cover - defensive
        return True

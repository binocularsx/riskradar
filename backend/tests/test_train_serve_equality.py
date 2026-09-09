"""The highest-value test in the suite (D15).

Train/serve skew is the single most likely way this model fails. IRE computes
``velocity_24h`` in pandas; the worker computes it in SQL. Two implementations,
two people, two mental models — they diverge, the model receives features in
production that are subtly not what it was trained on, and offline metrics keep
looking fine because offline evaluation keeps using the offline implementation.

The control is one shared package. This test is what makes the control real:
identical feature vectors from both data-access paths, on a shared fixture.

It cannot be retrofitted once both paths exist and have quietly drifted, which is
why it was written in week 2 rather than week 5.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from riskradar.features import (
    FEATURE_NAMES,
    PandasHistorySource,
    TxView,
    compute_features,
    load_history_frame,
    load_history_sql,
)


def _fixture_rows(subject: str, account: str, now: datetime) -> list[dict]:
    """A deliberately awkward history.

    It contains everything that has ever made two implementations disagree:
    declines and reversals alongside approvals, a NULL beneficiary (a cash
    withdrawal), a repeated beneficiary, two devices, an out-of-window row that
    must be excluded, and a row at the exact window boundary.
    """
    ben_a = f"acc_{uuid.uuid4().hex[:20]}"
    ben_b = f"acc_{uuid.uuid4().hex[:20]}"
    dev_known = f"dev_{uuid.uuid4().hex[:20]}"

    def row(minutes_ago: float, amount: int, result: str, ben: str | None, dev: str | None):
        return {
            "transaction_ref": uuid.uuid4().hex,
            "occurred_at": now - timedelta(minutes=minutes_ago),
            "amount_minor": amount,
            "currency": "NGN",
            "channel": "MOBILE_APP",
            "instrument": "ACCOUNT_TRANSFER",
            "rail": "NIP",
            "subject_token": subject,
            "account_token": account,
            "beneficiary_token": ben,
            "device_token": dev,
            "auth_result": result,
        }

    return [
        row(5, 120_000, "APPROVED", ben_a, dev_known),
        row(12, 90_000, "DECLINED", ben_b, dev_known),
        row(25, 4_500_000, "APPROVED", ben_a, dev_known),
        row(48, 60_000, "FAILED", ben_b, dev_known),
        row(55, 75_000, "REVERSED", ben_a, dev_known),
        row(59.5, 30_000, "APPROVED", None, dev_known),          # boundary of the 1h window
        row(60 * 26, 250_000, "APPROVED", ben_a, dev_known),     # outside 1h, inside 24h
        row(60 * 24 * 20, 1_000_000, "APPROVED", ben_a, dev_known),  # inside 30d
        row(60 * 24 * 45, 9_000_000, "APPROVED", ben_a, dev_known),  # OUTSIDE 30d — must not count
    ]


@pytest.fixture
def scenario(conn):
    subject = f"sub_{uuid.uuid4().hex[:20]}"
    account = f"acc_{uuid.uuid4().hex[:20]}"
    now = datetime.now(timezone.utc)
    rows = _fixture_rows(subject, account, now)

    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO transactions
                    (transaction_ref, occurred_at, amount_minor, currency, channel,
                     instrument, rail, subject_token, account_token, beneficiary_token,
                     device_token, auth_result)
                VALUES (%(transaction_ref)s, %(occurred_at)s, %(amount_minor)s, %(currency)s,
                        %(channel)s, %(instrument)s, %(rail)s, %(subject_token)s,
                        %(account_token)s, %(beneficiary_token)s, %(device_token)s,
                        %(auth_result)s)
                """,
                r,
            )

    tx = TxView(
        transaction_ref=uuid.uuid4().hex,
        occurred_at=now,
        amount_minor=3_000_000,
        currency="NGN",
        channel="MOBILE_APP",
        instrument="ACCOUNT_TRANSFER",
        rail="NIP",
        subject_token=subject,
        account_token=account,
        beneficiary_token=rows[0]["beneficiary_token"],
        device_token=f"dev_{uuid.uuid4().hex[:20]}",  # a device never seen before
        auth_result="APPROVED",
        account_opened_at=now - timedelta(days=365),
        last_activity_at=now - timedelta(days=3),
        product_type="CURRENT",
        origin_sol_id="SOL001",
    )
    return tx, rows, conn


def test_sql_and_pandas_paths_agree_exactly(scenario):
    """The assertion the whole design hangs on."""
    tx, rows, conn = scenario

    sql_features = compute_features(tx, load_history_sql(conn, tx))
    frame_features = compute_features(tx, load_history_frame(pd.DataFrame(rows), tx))

    assert set(sql_features) == set(FEATURE_NAMES)
    assert set(frame_features) == set(FEATURE_NAMES)

    mismatches = {
        name: (sql_features[name], frame_features[name])
        for name in FEATURE_NAMES
        if sql_features[name] != frame_features[name]
    }
    assert not mismatches, (
        "train/serve skew detected — the SQL path and the pandas path disagree. "
        f"Divergent features (sql, pandas): {mismatches}"
    )


def test_features_are_actually_exercised(scenario):
    """Guard against a green test that proves nothing.

    Two paths agreeing that every feature is zero is not evidence of anything.
    This asserts the fixture genuinely moves the features it was built to move.
    """
    tx, _rows, conn = scenario
    f = compute_features(tx, load_history_sql(conn, tx))

    assert f["txn_count_1h_account"] == 6, "1h window should exclude the 26h and 20d rows"
    assert f["failed_attempts_1h_account"] == 2, "one DECLINED and one FAILED inside 1h"
    assert 0 < f["decline_rate_24h_account"] < 1
    assert f["device_is_new_to_subject"] == 1.0
    assert f["beneficiary_is_new_to_account"] == 0.0
    assert f["account_age_days"] == pytest.approx(365, abs=1)
    assert f["days_since_account_activity"] == pytest.approx(3, abs=1)
    assert f["amount_ratio_to_account_p95_30d"] > 0


def test_out_of_window_history_is_excluded(scenario):
    """The 45-day-old NGN 90,000 transaction must not raise the 30-day ceiling.

    If it leaked in, ``amount_ratio_to_account_p95_30d`` would be depressed and
    every large transaction would look ordinary — a silent, model-wide failure.
    """
    tx, rows, conn = scenario
    sql = compute_features(tx, load_history_sql(conn, tx))
    frame = compute_features(tx, load_history_frame(pd.DataFrame(rows), tx))
    assert sql["amount_ratio_to_account_p95_30d"] == frame["amount_ratio_to_account_p95_30d"]
    # The out-of-window row is the largest by far; if it counted, the ratio
    # would fall below 1.
    assert sql["amount_ratio_to_account_p95_30d"] > 0.6


def test_a_transaction_is_never_in_its_own_history(scenario):
    """Strictly ``<``, not ``<=``.

    An off-by-one here shifts every count by exactly one and the model learns the
    offset instead of the behaviour — which still trains, still validates, and is
    still wrong.
    """
    tx, _rows, conn = scenario
    history = load_history_sql(conn, tx)
    assert all(p.occurred_at < tx.occurred_at for p in history.account)
    assert all(p.occurred_at < tx.occurred_at for p in history.subject)


def test_all_three_data_access_paths_agree(scenario):
    """SQL, naive pandas, and the indexed training source.

    The indexed source exists because the naive one is O(n) per row and would be
    unusable over a real corpus — and an unusable training path is precisely how
    a team ends up with a second, faster, *different* implementation of the
    features. Adding a path means adding it to this assertion.
    """
    tx, rows, conn = scenario
    frame = pd.DataFrame(rows)

    by_sql = compute_features(tx, load_history_sql(conn, tx))
    by_frame = compute_features(tx, load_history_frame(frame, tx))
    by_index = compute_features(tx, PandasHistorySource(frame).load(tx))

    for name in FEATURE_NAMES:
        assert by_sql[name] == by_frame[name] == by_index[name], (
            f"{name} diverges: sql={by_sql[name]} frame={by_frame[name]} "
            f"indexed={by_index[name]}"
        )

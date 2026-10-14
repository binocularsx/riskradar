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

import json
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

    def row(minutes_ago: float, amount: int, result: str, ben: str | None, dev: str | None,
            channel: str = "MOBILE_APP", instrument: str = "ACCOUNT_TRANSFER", region: str | None = "NG-LA",
            subject_token: str | None = None, account_token: str | None = None):
        return {
            "transaction_ref": uuid.uuid4().hex,
            "occurred_at": now - timedelta(minutes=minutes_ago),
            "amount_minor": amount,
            "currency": "NGN",
            "channel": channel,
            "instrument": instrument,
            "rail": "NIP",
            "subject_token": subject_token or subject,
            "account_token": account_token or account,
            "beneficiary_token": ben,
            "device_token": dev,
            "auth_result": result,
            "direction": "OUTBOUND",
            "remitter_token": None,
            "ip_region": region,
        }

    def credit(minutes_ago: float, amount: int, result: str, remitter: str):
        """D78: money into the same account. Must never count as outgoing history."""
        r = row(minutes_ago, amount, result, None, None)
        r.update(direction="INBOUND", remitter_token=remitter)
        return r

    other = f"sub_{uuid.uuid4().hex[:20]}"
    other_acc = f"acc_{uuid.uuid4().hex[:20]}"
    rem_x = f"acc_{uuid.uuid4().hex[:20]}"
    rem_y = f"acc_{uuid.uuid4().hex[:20]}"

    return [
        credit(30, 2_000_000, "APPROVED", rem_x),
        credit(90, 1_500_000, "APPROVED", rem_y),
        credit(200, 700_000, "DECLINED", rem_x),                # declined: counts, moves no money
        credit(60 * 23.9, 500_000, "APPROVED", rem_y),          # inside the day
        credit(60 * 24 * 10, 300_000, "APPROVED", rem_x),       # inside 30d, outside the day
        credit(60 * 24 * 40, 9_000_000, "APPROVED", rem_x),     # OUTSIDE 30d
        row(5, 120_000, "APPROVED", ben_a, dev_known),
        row(12, 90_000, "DECLINED", ben_b, dev_known, "POS", "CARD"),      # D82: card present
        row(25, 4_500_000, "APPROVED", ben_a, dev_known),
        row(48, 60_000, "FAILED", ben_b, dev_known, "ATM", "CARD"),        # D82: card present
        row(55, 75_000, "REVERSED", ben_a, dev_known),
        row(59.5, 30_000, "APPROVED", None, dev_known, region="NG-AB"),  # boundary of the 1h window
        row(60 * 26, 250_000, "APPROVED", ben_a, dev_known),     # outside 1h, inside 24h
        row(60 * 24 * 20, 1_000_000, "APPROVED", ben_a, dev_known),  # inside 30d
        row(60 * 24 * 45, 9_000_000, "APPROVED", ben_a, dev_known, region="NG-KN"),  # OUTSIDE 30d — must not count
        # D82: other customers paying ben_a. Two inside the day (one twice), one outside it.
        row(30, 50_000, "APPROVED", ben_a, None, subject_token=f"sub_{uuid.uuid4().hex[:20]}",
            account_token=f"acc_{uuid.uuid4().hex[:20]}"),
        *[row(m, 50_000, "APPROVED", ben_a, None, subject_token=other, account_token=other_acc)
          for m in (60 * 5, 60 * 6)],
        row(60 * 30, 50_000, "APPROVED", ben_a, None, subject_token=f"sub_{uuid.uuid4().hex[:20]}",
            account_token=f"acc_{uuid.uuid4().hex[:20]}"),
    ]


def _fixture_events(subject: str, device: str, payee: str, now: datetime) -> list[dict]:
    """D77: the events that precede a takeover, and the ones that must not count.

    Failed logins inside and outside the hour, the payment's device bound hours
    ago and another device bound more recently, a credential change inside the
    window, a SIM change outside it, the payment's payee enrolled minutes before
    and another payee enrolled after nothing, and a PAYMENT envelope that the
    event features must ignore.
    """
    other_device = f"dev_{uuid.uuid4().hex[:20]}"

    def ev(minutes_ago: float, event_type: str, device=None, detail=None):
        return {
            "event_ref": uuid.uuid4().hex,
            "event_type": event_type,
            "occurred_at": now - timedelta(minutes=minutes_ago),
            "subject_token": subject,
            "device_token": device,
            "detail": detail or {},
        }

    return [
        ev(3, "LOGIN", device, {"result": "SUCCESS", "method": "OTP"}),
        ev(8, "LOGIN", device, {"result": "FAILED", "method": "PASSWORD"}),
        ev(20, "LOGIN", device, {"result": "FAILED", "method": "PASSWORD"}),
        ev(59.5, "LOGIN", device, {"result": "FAILED", "method": "PASSWORD"}),   # inside 1h
        ev(70, "LOGIN", device, {"result": "FAILED", "method": "PASSWORD"}),     # outside 1h
        ev(60 * 5, "DEVICE_BOUND", device, {"binding": "BOUND"}),
        ev(60 * 2, "DEVICE_BOUND", other_device, {"binding": "BOUND"}),          # not this device
        ev(60 * 80, "DEVICE_BOUND", device, {"binding": "BOUND"}),               # outside 72h
        ev(60 * 30, "CREDENTIAL_CHANGED", None, {"credential": "PIN", "initiated_by": "CUSTOMER"}),
        ev(60 * 100, "SIM_CHANGED", None, {"msisdn_token": "msi_x", "carrier": "MTN"}),  # outside 72h
        ev(20, "PAYEE_ADDED", None, {"beneficiary_token": payee}),
        ev(10, "PAYEE_ADDED", None, {"beneficiary_token": f"acc_{uuid.uuid4().hex[:20]}"}),
    ]


def _events_frame(events: list[dict]) -> pd.DataFrame:
    """The events as the training path holds them: detail flattened, like the SQL."""
    return pd.DataFrame([
        {
            "occurred_at": e["occurred_at"],
            "event_type": e["event_type"],
            "subject_token": e["subject_token"],
            "device_token": e["device_token"],
            "login_result": e["detail"].get("result"),
            "binding": e["detail"].get("binding"),
            "beneficiary_token": e["detail"].get("beneficiary_token"),
        }
        for e in events
    ])


@pytest.fixture
def scenario(conn):
    subject = f"sub_{uuid.uuid4().hex[:20]}"
    account = f"acc_{uuid.uuid4().hex[:20]}"
    now = datetime.now(timezone.utc)
    rows = _fixture_rows(subject, account, now)
    device = f"dev_{uuid.uuid4().hex[:20]}"  # a device never used for a payment before
    events = _fixture_events(subject, device, next(r for r in rows if r["direction"] == "OUTBOUND")["beneficiary_token"], now)

    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO transactions
                    (transaction_ref, occurred_at, amount_minor, currency, channel,
                     instrument, rail, subject_token, account_token, beneficiary_token,
                     device_token, auth_result, direction, remitter_token, ip_region)
                VALUES (%(transaction_ref)s, %(occurred_at)s, %(amount_minor)s, %(currency)s,
                        %(channel)s, %(instrument)s, %(rail)s, %(subject_token)s,
                        %(account_token)s, %(beneficiary_token)s, %(device_token)s,
                        %(auth_result)s, %(direction)s, %(remitter_token)s, %(ip_region)s)
                """,
                r,
            )
        for e in events:
            cur.execute(
                """
                INSERT INTO events (event_ref, event_type, occurred_at, subject_token, device_token, detail)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (e["event_ref"], e["event_type"], e["occurred_at"], e["subject_token"],
                 e["device_token"], json.dumps(e["detail"])),
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
        beneficiary_token=next(r for r in rows if r["direction"] == "OUTBOUND")["beneficiary_token"],
        device_token=device,
        ip_region="NG-KN",
        auth_result="APPROVED",
        account_opened_at=now - timedelta(days=365),
        last_activity_at=now - timedelta(days=3),
        product_type="CURRENT",
        origin_sol_id="SOL001",
    )
    return tx, rows, conn, _events_frame(events)


def test_sql_and_pandas_paths_agree_exactly(scenario):
    """The assertion the whole design hangs on."""
    tx, rows, conn, events = scenario

    sql_features = compute_features(tx, load_history_sql(conn, tx))
    frame_features = compute_features(tx, load_history_frame(pd.DataFrame(rows), tx, events))

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
    tx, _rows, conn, _events = scenario
    f = compute_features(tx, load_history_sql(conn, tx))

    assert f["txn_count_1h_account"] == 6, "1h window should exclude the 26h and 20d rows"
    assert f["failed_attempts_1h_account"] == 2, "one DECLINED and one FAILED inside 1h"
    assert 0 < f["decline_rate_24h_account"] < 1
    assert f["device_is_new_to_subject"] == 1.0
    assert f["beneficiary_is_new_to_account"] == 0.0
    assert f["account_age_days"] == pytest.approx(365, abs=1)
    assert f["days_since_account_activity"] == pytest.approx(3, abs=1)
    assert f["amount_ratio_to_account_p95_30d"] > 0
    # D77
    assert f["failed_logins_1h_subject"] == 3, "the 70-minute failure is outside the hour"
    assert f["device_bound_hours"] == pytest.approx(5, abs=0.01), "this device, not the other"
    assert f["credential_changed_hours"] == pytest.approx(30, abs=0.01)
    assert f["sim_changed_hours"] == 72.0, "a SIM change 100 hours ago is not recent"
    assert f["payee_added_minutes"] == pytest.approx(20, abs=0.01), "this payee, not the later one"
    # D78: credits count on the receiving side and nowhere else.
    assert f["txn_count_1h_account"] == 6, "credits must not count as outgoing velocity"
    assert f["credits_24h_account"] == 4, "four credits inside the day; the 10- and 40-day ones outside"
    assert f["distinct_remitters_24h_account"] == 2
    assert f["minutes_since_last_credit"] == pytest.approx(30, abs=0.01)
    assert f["inbound_count_ratio_24h_vs_daily_mean_30d"] == pytest.approx(4 / (5 / 30), rel=1e-6)
    # Out: approved outgoing in the day (NGN 1,200 + 45,000 + 300, the 26-hour payment excluded, plus this
    # payment's 30,000) over approved in (20,000 + 15,000 + 5,000; the declined credit moved nothing).
    # D82
    assert f["region_is_new_to_subject"] == 1.0, "NG-KN was used 45 days ago only, outside the month"
    assert f["card_present_count_1h_account"] == 2, "the POS and ATM attempts; this payment is a transfer"
    assert f["beneficiary_distinct_senders_24h"] == 2, "two other customers in the day; the 30-hour one is out"
    assert 0 <= f["hour_of_day_local"] <= 23
    assert f["pass_through_ratio_24h"] == pytest.approx((120_000 + 4_500_000 + 30_000 + 3_000_000)
                                                         / (2_000_000 + 1_500_000 + 500_000), rel=1e-6)


def test_out_of_window_history_is_excluded(scenario):
    """The 45-day-old NGN 90,000 transaction must not raise the 30-day ceiling.

    If it leaked in, ``amount_ratio_to_account_p95_30d`` would be depressed and
    every large transaction would look ordinary — a silent, model-wide failure.
    """
    tx, rows, conn, events = scenario
    sql = compute_features(tx, load_history_sql(conn, tx))
    frame = compute_features(tx, load_history_frame(pd.DataFrame(rows), tx, events))
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
    tx, _rows, conn, _events = scenario
    history = load_history_sql(conn, tx)
    assert all(p.occurred_at < tx.occurred_at for p in history.account)
    assert all(p.occurred_at < tx.occurred_at for p in history.subject)
    assert all(e.occurred_at < tx.occurred_at for e in history.events)


def test_all_three_data_access_paths_agree(scenario):
    """SQL, naive pandas, and the indexed training source.

    The indexed source exists because the naive one is O(n) per row and would be
    unusable over a real corpus — and an unusable training path is precisely how
    a team ends up with a second, faster, *different* implementation of the
    features. Adding a path means adding it to this assertion.
    """
    tx, rows, conn, events = scenario
    frame = pd.DataFrame(rows)

    by_sql = compute_features(tx, load_history_sql(conn, tx))
    by_frame = compute_features(tx, load_history_frame(frame, tx, events))
    by_index = compute_features(tx, PandasHistorySource(frame, events).load(tx))

    for name in FEATURE_NAMES:
        assert by_sql[name] == by_frame[name] == by_index[name], (
            f"{name} diverges: sql={by_sql[name]} frame={by_frame[name]} "
            f"indexed={by_index[name]}"
        )


def test_the_paths_agree_on_a_credit(scenario):
    """D78: the scored transaction can itself be a credit; the paths must still agree."""
    import dataclasses

    tx, rows, conn, events = scenario
    credit = dataclasses.replace(tx, direction="INBOUND", beneficiary_token=None, device_token=None,
                                 remitter_token=f"acc_{uuid.uuid4().hex[:20]}")
    frame = pd.DataFrame(rows)
    by_sql = compute_features(credit, load_history_sql(conn, credit))
    by_frame = compute_features(credit, load_history_frame(frame, credit, events))
    by_index = compute_features(credit, PandasHistorySource(frame, events).load(credit))
    for name in FEATURE_NAMES:
        assert by_sql[name] == by_frame[name] == by_index[name], name
    assert by_sql["distinct_remitters_24h_account"] == 3, "this credit's new sender counts"
    assert by_sql["pass_through_ratio_24h"] > 0

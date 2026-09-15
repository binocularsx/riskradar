"""WP-08 / D80: who acts on an alert, the system or a person.

Pinned: named high-precision signals become machine actions, a deterministic
veto always goes to a person, auto-close only happens when enabled and never
on a rule's alert, and a takeover sequence end to end becomes a machine-handled
case carrying the 24-hour flag.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.policy.disposition import (
    AUTO_CLOSE,
    HUMAN_REVIEW,
    MACHINE_ACTION,
    NONE,
    DispositionPolicy,
    choose,
)
from riskradar.rules.engine import Signal
from riskradar.worker.scoring import active_ruleset, score_transaction, system_user_id

V1 = DispositionPolicy(version=1, machine_action_signals=frozenset({"CARD_TESTING_PROBES", "ACCOUNT_TAKEOVER_SEQUENCE"}))


def sig(code, power="ESCALATE"):
    return Signal(code=code, power=power, severity="HIGH")


def test_dispositions_follow_the_policy():
    assert choose(actionable=False, signals=[], p_fraud=0.9, model_applies=True, policy=V1) == NONE
    assert choose(actionable=True, signals=[sig("CARD_TESTING_PROBES")], p_fraud=0.1, model_applies=True,
                  policy=V1) == MACHINE_ACTION
    assert choose(actionable=True, signals=[sig("VELOCITY_BURST_1H")], p_fraud=0.1, model_applies=True,
                  policy=V1) == HUMAN_REVIEW
    assert choose(actionable=True, signals=[], p_fraud=0.3, model_applies=True, policy=None) == HUMAN_REVIEW


def test_a_veto_is_always_a_persons():
    signals = [sig("ACCOUNT_TAKEOVER_SEQUENCE"), sig("SANCTIONED_BENEFICIARY", "OVERRIDE")]
    assert choose(actionable=True, signals=signals, p_fraud=0.9, model_applies=True, policy=V1) == HUMAN_REVIEW


def test_auto_close_only_when_enabled_and_never_on_a_rule():
    on = DispositionPolicy(version=2, machine_action_signals=frozenset(), auto_close_enabled=True,
                           auto_close_max_probability=0.05)
    assert choose(actionable=True, signals=[], p_fraud=0.04, model_applies=True, policy=on) == AUTO_CLOSE
    assert choose(actionable=True, signals=[], p_fraud=0.04, model_applies=True, policy=V1) == HUMAN_REVIEW
    assert choose(actionable=True, signals=[sig("VELOCITY_BURST_1H")], p_fraud=0.01, model_applies=True,
                  policy=on) == HUMAN_REVIEW
    assert choose(actionable=True, signals=[], p_fraud=0.04, model_applies=False, policy=on) == HUMAN_REVIEW


def test_auto_close_needs_a_cut_in_the_schema(conn):
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO disposition_policies (version, machine_action_signals, auto_close_enabled, "
                     "review_capacity_per_day, notes) VALUES (99, '{}', true, 10, 'no cut given')")


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def test_a_takeover_sequence_becomes_a_machine_handled_case_with_the_flag(client, api_headers, sample_transaction):
    customer, account = f"CIF{uuid.uuid4().hex[:10]}", f"ACC{uuid.uuid4().hex[:10]}"
    device = f"dev-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    common = {"customer_id": customer, "occurred_at": (now - timedelta(minutes=20)).isoformat(), "channel": "MOBILE_APP"}
    for body in (
        {"event_type": "DEVICE_BOUND", "event_ref": uuid.uuid4().hex, "device_fingerprint": device,
         "detail": {"binding": "BOUND"}, **common},
        {"event_type": "CREDENTIAL_CHANGED", "event_ref": uuid.uuid4().hex,
         "detail": {"credential": "PIN", "initiated_by": "CUSTOMER"}, **common},
    ):
        assert client.post("/v1/events", json=body, headers=api_headers).status_code == 202
    payment = sample_transaction(customer_id=customer, account_id=account, device_fingerprint=device)
    tx_id = client.post("/v1/transactions", json=payment, headers=api_headers).json()["transaction_id"]

    with _db() as c:
        try:
            c.execute("DELETE FROM decisions WHERE transaction_id = %s", (tx_id,))  # a running worker may have scored it
            ruleset_id, configs = active_ruleset(c)
            configs = dict(configs)
            configs["ACCOUNT_TAKEOVER_SEQUENCE"] = {"enabled": True, "severity": "HIGH",
                                                    "params": {"within_hours": 24, "min_failed_logins": 3,
                                                               "min_precursors": 2}}
            out = score_transaction(c, tx_id, sys_uid=system_user_id(c), ruleset=(ruleset_id, configs))
            decision = c.execute("SELECT disposition, signals FROM decisions WHERE transaction_id = %s",
                                 (tx_id,)).fetchone()
            assert "ACCOUNT_TAKEOVER_SEQUENCE" in [s["code"] for s in decision["signals"]]
            assert decision["disposition"] == "MACHINE_ACTION" == out["disposition"]
            case = c.execute("SELECT handling, subject_token FROM cases WHERE id = %s", (out["case_id"],)).fetchone()
            assert case["handling"] == "MACHINE"
            flag = c.execute("SELECT reason FROM subject_watchlist WHERE subject_token = %s AND lifted_at IS NULL",
                             (case["subject_token"],)).fetchone()
            assert flag and "Machine action" in flag["reason"]
        finally:
            c.rollback()
            c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
            c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
            c.commit()

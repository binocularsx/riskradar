"""WP-03 / D78: money coming in.

A credit enters through the same door as a payment, is tokenised the same way,
gets a CREDIT envelope, and is decided by receiving-side rules with no model
involved. None of that may change what an outgoing payment's features read.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from riskradar.config import settings
from riskradar.security.tokens import account_token
from riskradar.worker.scoring import score_transaction, system_user_id


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


@pytest.fixture
def credit(sample_transaction):
    def build(**over):
        payload = sample_transaction(direction="INBOUND", beneficiary_account_id=None, device_fingerprint=None,
                                     channel="API", remitter_account_id=f"RMT{uuid.uuid4().hex[:12]}",
                                     remitter_bank_code="058")
        payload.update(over)
        return payload
    return build


@pytest.fixture
def cleanup():
    ids: list[int] = []
    yield ids
    with _db() as c:
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = ANY(%s)", (ids,))
        c.execute("DELETE FROM transactions WHERE id = ANY(%s)", (ids,))
        c.commit()


def test_a_credit_is_stored_with_its_sender_tokenised(client, api_headers, credit, cleanup):
    payload = credit()
    r = client.post("/v1/transactions", json=payload, headers=api_headers)
    assert r.status_code == 202, r.text
    cleanup.append(r.json()["transaction_id"])
    with _db() as c:
        tx = c.execute("SELECT * FROM transactions WHERE id = %s", (r.json()["transaction_id"],)).fetchone()
        ev = c.execute("SELECT event_type::text AS t FROM events WHERE transaction_id = %s", (tx["id"],)).fetchone()
    assert (tx["direction"], tx["beneficiary_token"], tx["remitter_bank_code"]) == ("INBOUND", None, "058")
    assert tx["remitter_token"] == account_token(payload["remitter_account_id"])
    assert payload["remitter_account_id"] not in " ".join(str(v) for v in tx.values())
    assert ev["t"] == "CREDIT"


def test_direction_must_be_consistent(client, api_headers, credit, sample_transaction):
    no_sender = credit(remitter_account_id=None)
    assert client.post("/v1/transactions", json=no_sender, headers=api_headers).status_code == 422
    with_payee = credit(beneficiary_account_id="BEN123")
    assert client.post("/v1/transactions", json=with_payee, headers=api_headers).status_code == 422
    outbound_sender = sample_transaction(remitter_account_id="RMT1")
    assert client.post("/v1/transactions", json=outbound_sender, headers=api_headers).status_code == 422


def test_a_credit_is_decided_by_receiving_side_rules_without_the_model(client, api_headers, credit, cleanup):
    payload = credit()
    tx_id = client.post("/v1/transactions", json=payload, headers=api_headers).json()["transaction_id"]
    cleanup.append(tx_id)
    with _db() as c:
        if not c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone():
            score_transaction(c, tx_id, sys_uid=system_user_id(c))
            c.commit()
        d = c.execute("SELECT model_version_id, p_fraud, policy_trace, features FROM decisions WHERE transaction_id = %s",
                      (tx_id,)).fetchone()
    assert d["model_version_id"] is None and float(d["p_fraud"]) == 0.0
    assert d["policy_trace"][0]["source"] == "receiving_side"
    assert d["features"]["distinct_remitters_24h_account"] == 1.0


def test_a_payment_after_credits_sees_them_on_the_receiving_side_only(client, api_headers, credit,
                                                                      sample_transaction, cleanup):
    customer, account = f"CIF{uuid.uuid4().hex[:10]}", f"ACC{uuid.uuid4().hex[:10]}"
    for _ in range(3):
        r = client.post("/v1/transactions", json=credit(customer_id=customer, account_id=account), headers=api_headers)
        cleanup.append(r.json()["transaction_id"])
    pay = sample_transaction(customer_id=customer, account_id=account)
    tx_id = client.post("/v1/transactions", json=pay, headers=api_headers).json()["transaction_id"]
    cleanup.append(tx_id)
    with _db() as c:
        if not c.execute("SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone():
            score_transaction(c, tx_id, sys_uid=system_user_id(c))
            c.commit()
        f = c.execute("SELECT features FROM decisions WHERE transaction_id = %s", (tx_id,)).fetchone()["features"]
    assert f["credits_24h_account"] == 3.0 and f["distinct_remitters_24h_account"] == 3.0
    assert f["txn_count_1h_account"] == 0.0, "credits are not outgoing velocity"
    assert f["pass_through_ratio_24h"] > 0

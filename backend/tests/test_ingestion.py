"""Ingestion boundary behaviour (FR-001 to FR-006)."""

from __future__ import annotations

import psycopg
import pytest

from riskradar.config import settings
from riskradar.security.tokens import account_token, subject_token


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def test_requires_api_key(client, sample_transaction):
    r = client.post("/v1/transactions", json=sample_transaction())
    assert r.status_code == 401


def test_rejects_unknown_api_key(client, sample_transaction):
    r = client.post(
        "/v1/transactions", json=sample_transaction(), headers={"X-API-Key": "nope"}
    )
    assert r.status_code == 401


def test_accepts_and_enqueues_in_one_transaction(client, api_headers, sample_transaction):
    """FR-005: persisted and enqueued together, or not at all.

    The invariant is "this transaction was accepted **for scoring**" — which is
    satisfied either by a queue row waiting, or by a decision already written.
    Asserting only the queue row would race a running worker: on a machine where
    the workers are up, they can claim and score it before this test looks.
    That would be a race in the test, not in the system.
    """
    payload = sample_transaction()
    r = client.post("/v1/transactions", json=payload, headers=api_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["queued"] is True

    tx_id = body["transaction_id"]
    with _db() as c:
        queued = c.execute(
            "SELECT 1 FROM scoring_queue WHERE transaction_id = %s", (tx_id,)
        ).fetchone()
        scored = c.execute(
            "SELECT 1 FROM decisions WHERE transaction_id = %s", (tx_id,)
        ).fetchone()
        assert queued or scored, (
            "transaction was persisted but neither queued nor scored — the "
            "enqueue did not happen in the same database transaction as the insert"
        )
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
        c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        c.commit()


def test_idempotent_resubmission_is_a_noop(client, api_headers, sample_transaction):
    """FR-002. A caller retrying after a timeout is behaving correctly."""
    payload = sample_transaction()
    first = client.post("/v1/transactions", json=payload, headers=api_headers)
    second = client.post("/v1/transactions", json=payload, headers=api_headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["transaction_id"] == first.json()["transaction_id"]

    with _db() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM transactions WHERE transaction_ref = %s",
            (payload["transaction_ref"],),
        ).fetchone()["n"]
        assert n == 1
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (first.json()["transaction_id"],))
        c.execute("DELETE FROM transactions WHERE transaction_ref = %s", (payload["transaction_ref"],))
        c.commit()


def test_raw_identifiers_never_reach_the_database(client, api_headers, sample_transaction):
    """FR-004 / D9c. The boundary is the last place an account number exists."""
    payload = sample_transaction()
    r = client.post("/v1/transactions", json=payload, headers=api_headers)
    tx_id = r.json()["transaction_id"]

    with _db() as c:
        row = c.execute(
            "SELECT subject_token, account_token FROM transactions WHERE id = %s", (tx_id,)
        ).fetchone()
        assert row["subject_token"] == subject_token(payload["customer_id"])
        assert row["account_token"] == account_token(payload["account_id"])
        assert payload["customer_id"] not in row["subject_token"]
        assert payload["account_id"] not in row["account_token"]

        # And nothing anywhere in the row echoes the raw value back.
        full = c.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,)).fetchone()
        blob = " ".join(str(v) for v in full.values())
        assert payload["customer_id"] not in blob
        assert payload["account_id"] not in blob

        c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
        c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        c.commit()


def test_tokens_are_deterministic_and_namespaced():
    """Deterministic so baselines work; namespaced so a customer id and an
    account id that happen to match cannot collide into one baseline."""
    assert subject_token("X1") == subject_token("X1")
    assert subject_token("X1") != account_token("X1")


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"amount_minor": "250000"}, "string amount must not be coerced"),
        ({"amount_minor": -5}, "negative money"),
        ({"currency": "NAIRA"}, "not ISO 4217"),
        ({"occurred_at": "2026-09-09T10:00:00"}, "naive timestamp"),
        ({"channel": "TELEPATHY"}, "unknown channel"),
        ({"unexpected_field": 1}, "unknown field must not be silently ignored"),
    ],
)
def test_malformed_payloads_are_rejected_and_dead_lettered(
    client, api_headers, sample_transaction, bad, reason
):
    """FR-003: reject, never coerce — and keep the evidence."""
    with _db() as c:
        before = c.execute("SELECT count(*) AS n FROM dead_letter").fetchone()["n"]

    r = client.post("/v1/transactions", json=sample_transaction(**bad), headers=api_headers)
    assert r.status_code == 422, f"{reason}: expected rejection, got {r.status_code}"

    with _db() as c:
        after = c.execute("SELECT count(*) AS n FROM dead_letter").fetchone()["n"]
        assert after == before + 1, f"{reason}: no dead-letter row written"
        row = c.execute(
            "SELECT raw_body, validation_error FROM dead_letter ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["raw_body"], "raw body must be preserved for the caller"
        assert row["validation_error"]


def test_batch_replay_does_not_raise_alerts_by_default(client, api_headers, sample_transaction):
    """D8d. Re-scoring six weeks of history must not page anybody."""
    payloads = [sample_transaction() for _ in range(3)]
    r = client.post(
        "/v1/transactions/batch", json={"transactions": payloads}, headers=api_headers
    )
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] == 3

    ids = [item["transaction_id"] for item in r.json()["results"]]
    with _db() as c:
        rows = c.execute(
            "SELECT is_replay, raise_alerts FROM transactions WHERE id = ANY(%s)", (ids,)
        ).fetchall()
        assert all(row["is_replay"] and not row["raise_alerts"] for row in rows)
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = ANY(%s)", (ids,))
        c.execute("DELETE FROM transactions WHERE id = ANY(%s)", (ids,))
        c.commit()

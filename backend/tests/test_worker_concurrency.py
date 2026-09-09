"""Queue claiming and scoring idempotency.

These are regression tests for a bug that only appeared under concurrency, and
only showed itself as a *performance* symptom first: three workers drained a
backlog no faster than one.

The cause was that the worker held `FOR UPDATE SKIP LOCKED` row locks for a whole
64-transaction batch and did the scoring inside that same transaction. Because
`pg_advisory_xact_lock` lives until the transaction ends, the first alert in a
batch took the global audit-chain lock and held it for the rest of the batch —
serialising every worker, then deadlocking them. A deadlock rolled the
transaction back, which released the claim on the batch's unprocessed rows while
the loop kept going, so two workers scored the same transaction: 77 unique-key
violations in one run.

Both halves are tested here: the lease must actually hide a claimed row from
another worker, and scoring must be a no-op the second time regardless.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from riskradar.config import settings
from riskradar.worker import scoring


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


@pytest.fixture
def transaction(request):
    """A transaction, cleaned up afterwards.

    It is **not** enqueued unless the test asks for it via the ``enqueue`` mark.
    Live workers share this database, and a fixture that enqueued unconditionally
    would have its row scored out from under the test — a race in the test, not
    in the system.
    """
    ref = f"conc-{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    with _db() as c:
        row = c.execute(
            """
            INSERT INTO transactions
                (transaction_ref, occurred_at, amount_minor, currency, channel,
                 instrument, rail, subject_token, account_token, beneficiary_token,
                 auth_result, account_opened_at, last_activity_at, product_type,
                 origin_sol_id, raise_alerts)
            VALUES (%s, %s, 500000, 'NGN', 'MOBILE_APP', 'ACCOUNT_TRANSFER', 'NIP',
                    %s, %s, %s, 'APPROVED', %s, %s, 'CURRENT', 'SOL001', false)
            RETURNING id
            """,
            (
                ref, now,
                f"sub_{uuid.uuid4().hex[:20]}",
                f"acc_{uuid.uuid4().hex[:20]}",
                f"acc_{uuid.uuid4().hex[:20]}",
                now - timedelta(days=300),
                now - timedelta(days=2),
            ),
        ).fetchone()
        tx_id = row["id"]
        if request.node.get_closest_marker("enqueue"):
            c.execute("INSERT INTO scoring_queue (transaction_id) VALUES (%s)", (tx_id,))
        c.commit()

    yield tx_id

    with _db() as c:
        c.execute("DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,))
        c.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        c.commit()


@pytest.mark.enqueue
def test_two_claims_never_return_the_same_row(transaction):
    """The invariant that matters: no transaction is handed to two workers.

    The lease, not a held lock, is what hides the row — its ``available_at`` is
    pushed into the future and **committed**, so it stays hidden after the
    claiming transaction ends. Under the old design the row was only hidden
    while a lock was held, and a rollback handed it straight to somebody else.

    Asserted as a disjointness property rather than "worker A got *my* row",
    because live workers share this queue and may legitimately take it first.
    That does not weaken the test: whoever claims it, nobody else may.
    """
    first = scoring.Worker(batch_size=500)
    second = scoring.Worker(batch_size=500)

    claimed_a, *_ = first.claim()
    claimed_b, *_ = second.claim()

    overlap = set(claimed_a) & set(claimed_b)
    assert not overlap, (
        f"{len(overlap)} row(s) were leased to two workers at once — they would "
        f"both score the same transaction: {sorted(overlap)[:5]}"
    )

    with _db() as c:
        row = c.execute(
            "SELECT available_at, attempts FROM scoring_queue WHERE transaction_id = %s",
            (transaction,),
        ).fetchone()
    if row:  # still queued: someone leased it, so the lease must be visible
        assert row["available_at"] > datetime.now(timezone.utc), "lease was not applied"
        assert row["attempts"] >= 1, "attempts must increment at claim, bounding retries"


def test_scoring_the_same_transaction_twice_is_a_no_op(transaction):
    """Belt and braces behind the lease.

    A lease can expire under a slow score and a worker can be restarted
    mid-batch. A decision is immutable and written exactly once, so the second
    arrival must be a no-op — not a unique-key violation that takes the worker's
    whole transaction down with it.
    """
    with _db() as conn:
        sys_uid = scoring.system_user_id(conn)
        ruleset = scoring.active_ruleset(conn)
        thresholds = scoring.active_thresholds(conn)

        first = scoring.score_transaction(
            conn, transaction, sys_uid=sys_uid,
            ruleset=ruleset, thresholds=thresholds,
        )
        assert "decision_id" in first

        second = scoring.score_transaction(
            conn, transaction, sys_uid=sys_uid,
            ruleset=ruleset, thresholds=thresholds,
        )
        assert second.get("skipped") == "already scored"

        n = conn.execute(
            "SELECT count(*) AS n FROM decisions WHERE transaction_id = %s",
            (transaction,),
        ).fetchone()["n"]
        assert n == 1, "a transaction must have exactly one decision"
        conn.rollback()


def test_a_decision_is_always_written_even_when_no_alert_is_raised(transaction):
    """D7a. Always, for every scored transaction, including the boring ones.

    This transaction was ingested with `raise_alerts = false`, so nothing should
    alert — and a decision must still exist, or the alert table becomes the only
    record of what the system thought.
    """
    with _db() as conn:
        result = scoring.score_transaction(
            conn, transaction,
            sys_uid=scoring.system_user_id(conn),
            ruleset=scoring.active_ruleset(conn),
            thresholds=scoring.active_thresholds(conn),
        )
        assert result["decision_id"]
        assert result["alert_id"] is None, "raise_alerts=false must not raise an alert"

        decision = conn.execute(
            "SELECT features, signals, model_version_id, ruleset_id, threshold_set_id, "
            "       feature_spec_version "
            "  FROM decisions WHERE transaction_id = %s",
            (transaction,),
        ).fetchone()
        # G4: reproducible from stored model version, ruleset version and snapshot.
        assert decision["features"], "the feature snapshot is what makes it reproducible"
        assert decision["ruleset_id"] and decision["threshold_set_id"]
        assert decision["feature_spec_version"]
        conn.rollback()

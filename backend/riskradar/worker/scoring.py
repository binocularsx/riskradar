"""The scoring worker.

Claims queued transactions with ``SELECT ... FOR UPDATE SKIP LOCKED`` (D8a) and
runs the three layers: features, model, rules, then policy.

Postgres is the queue. No Kafka, no Redis, no Celery — one database, one
durability story, one backup strategy. ``SKIP LOCKED`` gives us competing
consumers with no broker, and the claim is a row lock inside the same transaction
that writes the decision, so a worker that dies mid-score releases its claim and
the work is retried rather than lost.

Ordering that matters:

* **The decision is written before any alert is raised** (D7a). Always, for every
  scored transaction, including the boring ones. That is what makes a decision
  reproducible months later and what stops the alert table becoming the only
  record of what the system thought.
* **The notification is sent last, and carries an id only** (D14a). The table is
  the durable record; ``NOTIFY`` is a doorbell.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any

from ..audit import chain
from ..cases import correlation
from ..db import connection, pool
from ..features import compute_features, load_history_sql, to_vector
from ..features.spec import FEATURE_SPEC_VERSION
from ..features.types import TxView
from ..model import registry
from ..policy.engine import Thresholds, apply as apply_policy
from ..rules.engine import RuleContext, evaluate as evaluate_rules

log = logging.getLogger("riskradar.worker")

# Claim by lease, not by held lock.
#
# The first version held `FOR UPDATE SKIP LOCKED` row locks for the whole batch
# and did the scoring inside that same transaction. Two things went wrong under
# concurrency, and both were measured rather than guessed:
#
# 1. **Deadlock.** `pg_advisory_xact_lock` — used to serialise the audit chain
#    (D12c) and case correlation (D13a) — is held until the *transaction* ends.
#    With a 64-transaction batch in one transaction, the first alert grabbed the
#    global audit lock and held it for the rest of the batch, while another
#    worker held a subject lock and waited for the audit lock. Classic ABBA.
# 2. **Duplicate scoring.** A deadlock rolls the transaction back, which
#    releases the claim on every *unprocessed* row in the batch — and the loop
#    kept going. Another worker picked the same rows up. 77 unique-key
#    violations on `decisions.transaction_id` in one run.
#
# The measurable symptom was that three workers drained a backlog no faster than
# one: the global lock had serialised them.
#
# So the claim is now a short, committed lease. The rows are pushed into the
# future and made invisible to other workers, the transaction commits
# immediately, and each transaction is then scored in its own short transaction.
# A worker that dies mid-score simply lets its lease expire.
CLAIM_SQL = """
    UPDATE scoring_queue
       SET available_at = now() + %(lease)s::interval,
           attempts     = attempts + 1
     WHERE transaction_id IN (
             SELECT q.transaction_id
               FROM scoring_queue q
              WHERE q.available_at <= now()
              ORDER BY q.available_at, q.transaction_id
                FOR UPDATE SKIP LOCKED
              LIMIT %(limit)s
           )
    RETURNING transaction_id
"""

# Long enough that a slow score finishes inside it, short enough that a crashed
# worker's backlog is picked up promptly.
LEASE = "60 seconds"

TX_SQL = """
    SELECT id, transaction_ref, occurred_at, amount_minor, currency,
           channel, instrument, rail, subject_token, account_token,
           beneficiary_token, device_token, ip_region, merchant_category,
           auth_result, decline_reason, account_opened_at, last_activity_at,
           product_type, origin_sol_id, is_replay, raise_alerts
      FROM transactions
     WHERE id = %s
"""

MAX_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def system_user_id(conn: Any) -> int:
    """The reserved SYSTEM principal. Audit actor is never null (D12c)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE is_system LIMIT 1")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("no SYSTEM principal — run scripts/seed.py")
    return int(row["id"] if isinstance(row, dict) else row[0])


def active_ruleset(conn: Any) -> tuple[int, dict[str, dict[str, Any]]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM rulesets WHERE is_active LIMIT 1")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("no active ruleset — run scripts/seed.py")
        ruleset_id = int(row["id"] if isinstance(row, dict) else row[0])
        cur.execute(
            "SELECT code, enabled, severity, params FROM rule_configs WHERE ruleset_id = %s",
            (ruleset_id,),
        )
        configs = {}
        for r in cur.fetchall():
            d = dict(r) if not isinstance(r, dict) else r
            params = d["params"]
            if isinstance(params, str):
                params = json.loads(params)
            configs[d["code"]] = {
                "enabled": d["enabled"],
                "severity": d["severity"],
                "params": params or {},
            }
    return ruleset_id, configs


def active_thresholds(conn: Any) -> Thresholds:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version, p_monitor, p_review, p_hold, alert_min_level
              FROM threshold_sets WHERE is_active LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError("no active threshold set — run scripts/seed.py")
    return Thresholds.from_row(dict(row) if not isinstance(row, dict) else row)


def list_membership(conn: Any, tx: TxView) -> tuple[bool, bool, bool]:
    """Resolve rule-list membership once, before evaluation.

    One query for all three lists rather than one per rule: rules must not be
    able to turn themselves into the latency bottleneck.
    """
    if not tx.beneficiary_token:
        return False, False, False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT kind, account_token
              FROM beneficiary_lists
             WHERE token = %s
               AND (account_token IS NULL OR account_token = %s)
            """,
            (tx.beneficiary_token, tx.account_token),
        )
        rows = [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]
    kinds = {r["kind"] for r in rows}
    return (
        "SANCTIONED" in kinds,
        "KNOWN_MULE" in kinds,
        "ALLOWLIST" in kinds,
    )


# ---------------------------------------------------------------------------
# Scoring one transaction
# ---------------------------------------------------------------------------


def _tx_view(row: dict[str, Any]) -> TxView:
    return TxView(
        transaction_ref=row["transaction_ref"],
        occurred_at=row["occurred_at"],
        amount_minor=int(row["amount_minor"]),
        currency=row["currency"],
        channel=row["channel"],
        instrument=row["instrument"],
        rail=row["rail"],
        subject_token=row["subject_token"],
        account_token=row["account_token"],
        beneficiary_token=row["beneficiary_token"],
        device_token=row["device_token"],
        ip_region=row["ip_region"],
        merchant_category=row["merchant_category"],
        auth_result=row["auth_result"],
        decline_reason=row["decline_reason"],
        account_opened_at=row["account_opened_at"],
        last_activity_at=row["last_activity_at"],
        product_type=row["product_type"],
        origin_sol_id=row["origin_sol_id"],
    )


def score_transaction(
    conn: Any,
    transaction_id: int,
    *,
    sys_uid: int,
    ruleset: tuple[int, dict[str, Any]] | None = None,
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    """Score one transaction and persist everything it implies.

    Runs inside the caller's transaction, which also holds the queue row lock.

    ``ruleset`` and ``thresholds`` are passed in by the batch loop. Loading them
    per transaction cost three extra round trips each and dominated throughput
    once the model call was cheap — they change only when an administrator
    publishes a new version, so a batch is the right granularity. A batch is
    also short enough that a mid-batch retune takes effect within a second,
    which is the property FR-040 actually needs.
    """
    started = time.perf_counter()

    with conn.cursor() as cur:
        # Idempotency guard. A lease can expire under a slow score, or a worker
        # can be restarted mid-batch, and either way two workers could reach the
        # same transaction. A decision is immutable and written exactly once, so
        # the second arrival is a no-op rather than a unique-key violation.
        cur.execute("SELECT id FROM decisions WHERE transaction_id = %s", (transaction_id,))
        if cur.fetchone():
            return {"skipped": "already scored", "transaction_id": transaction_id}

        cur.execute(TX_SQL, (transaction_id,))
        row = cur.fetchone()
    if not row:
        return {"skipped": "transaction vanished"}
    row = dict(row) if not isinstance(row, dict) else row
    tx = _tx_view(row)

    # --- features (D15: the shared package, SQL data-access path) -----------
    history = load_history_sql(conn, tx)
    features = compute_features(tx, history)
    vector = to_vector(features)

    # --- model -------------------------------------------------------------
    rule_only = False
    model_id: int | None = None
    p_fraud = 0.0
    bundle = None
    try:
        bundle = registry.load_active(conn)
        model_id = bundle.id
        p_fraud = bundle.predict(vector)
    except registry.ModelUnavailable as exc:
        # FR-017 / D15d: rule-only mode is never a silent degradation.
        rule_only = True
        _raise_alarm(conn, sys_uid, "MODEL_UNAVAILABLE", str(exc))

    # --- rules -------------------------------------------------------------
    ruleset_id, rule_configs = ruleset if ruleset else active_ruleset(conn)
    sanctioned, known_mule, pre_registered = list_membership(conn, tx)
    signals = evaluate_rules(
        RuleContext(
            tx=tx,
            features=features,
            sanctioned=sanctioned,
            known_mule=known_mule,
            pre_registered=pre_registered,
        ),
        rule_configs,
    )

    # --- policy ------------------------------------------------------------
    thresholds = thresholds or active_thresholds(conn)
    result = apply_policy(p_fraud, signals, thresholds, rule_only_mode=rule_only)

    # --- explanation, only where it is needed -------------------------------
    # G3 requires attributions on **alerts**, not on every decision, and the vast
    # majority of decisions are ALLOW. Explaining all of them cost ~180ms each
    # and would have made NFR-001 unreachable on a laptop. The decision record
    # still stores the model version and the full feature snapshot, so an
    # attribution for a non-alerting decision can always be recomputed exactly.
    attributions: dict[str, float] = {}
    if bundle is not None and result.actionable:
        attributions = bundle.attributions(vector, registry.baseline_for(bundle))

    latency_ms = int((time.perf_counter() - started) * 1000)

    # --- decision: always written, written first (D7a) ----------------------
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decisions
                (transaction_id, p_fraud, score_0_100, decision, risk_level,
                 model_version_id, ruleset_id, threshold_set_id, feature_spec_version,
                 features, signals, attributions, policy_trace, rule_only_mode, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                transaction_id,
                result.p_fraud,
                result.score_0_100,
                result.decision,
                result.risk_level,
                model_id,
                ruleset_id,
                thresholds.id,
                FEATURE_SPEC_VERSION,
                json.dumps(features),
                json.dumps([s.as_dict() for s in signals]),
                json.dumps(attributions),
                json.dumps(result.trace),
                rule_only,
                latency_ms,
            ),
        )
        dec_row = cur.fetchone()
        decision_id = int(dec_row["id"] if isinstance(dec_row, dict) else dec_row[0])

    outcome: dict[str, Any] = {
        "transaction_id": transaction_id,
        "decision_id": decision_id,
        "risk_level": result.risk_level,
        "decision": result.decision,
        "score": result.score_0_100,
        "latency_ms": latency_ms,
        "alert_id": None,
        "case_id": None,
    }

    # --- alert and case ----------------------------------------------------
    # D8d: replayed transactions do not raise alerts unless explicitly requested.
    # Re-scoring six weeks of history on a model change must not page anybody.
    if result.actionable and row["raise_alerts"]:
        case_id, created = correlation.attach(
            conn,
            subject_token=tx.subject_token,
            risk_level=result.risk_level,
            at=row["occurred_at"],
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts
                    (decision_id, transaction_id, case_id, subject_token,
                     risk_level, score_0_100)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    decision_id,
                    transaction_id,
                    case_id,
                    tx.subject_token,
                    result.risk_level,
                    result.score_0_100,
                ),
            )
            a_row = cur.fetchone()
            alert_id = int(a_row["id"] if isinstance(a_row, dict) else a_row[0])

        outcome["alert_id"] = alert_id
        outcome["case_id"] = case_id

        chain.append(
            conn,
            actor_user_id=sys_uid,
            action="ALERT_RAISED",
            object_type="alert",
            object_id=alert_id,
            to_state=result.risk_level,
            payload={
                "case_id": case_id,
                "case_created": created,
                "decision_id": decision_id,
                "transaction_ref": tx.transaction_ref,
                "score": result.score_0_100,
                "signals": [s.code for s in signals],
                "rule_only_mode": rule_only,
            },
        )
        _publish(
            conn,
            "alert",
            {
                "alert_id": alert_id,
                "case_id": case_id,
                "risk_level": result.risk_level,
                "score": result.score_0_100,
            },
        )

    return outcome


# ---------------------------------------------------------------------------
# Notification and alarms
# ---------------------------------------------------------------------------


def _publish(conn: Any, event_type: str, payload: dict[str, Any]) -> int:
    """Durable event row plus an id-only doorbell (D14a).

    ``LISTEN``/``NOTIFY`` has an 8000-byte payload cap and is not durable: a
    client disconnected at the moment of the notify never learns it happened. So
    the row is the record and the notification carries only its id; the SSE
    endpoint reads the row, and ``Last-Event-ID`` replays the gap from the table.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stream_events (event_type, payload) VALUES (%s, %s) RETURNING id",
            (event_type, json.dumps(payload)),
        )
        row = cur.fetchone()
        event_id = int(row["id"] if isinstance(row, dict) else row[0])
        cur.execute("SELECT pg_notify('riskradar_events', %s)", (str(event_id),))
    return event_id


def _raise_alarm(conn: Any, sys_uid: int, code: str, detail: str) -> None:
    log.error("ALARM %s: %s", code, detail)
    _publish(conn, "alarm", {"code": code, "detail": detail})
    chain.append(
        conn,
        actor_user_id=sys_uid,
        action="ALARM",
        object_type="system",
        object_id=code,
        payload={"detail": detail},
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, batch_size: int = 64, idle_sleep: float = 0.25) -> None:
        self.batch_size = batch_size
        self.idle_sleep = idle_sleep
        self._stop = False

    def request_stop(self, *_: Any) -> None:
        log.info("stop requested; finishing current batch")
        self._stop = True

    def claim(self) -> tuple[list[int], int, tuple[int, dict[str, Any]], Thresholds]:
        """Lease a batch and read the reference data. Commits immediately."""
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(CLAIM_SQL, {"lease": LEASE, "limit": self.batch_size})
                claimed = [
                    int(r["transaction_id"] if isinstance(r, dict) else r[0])
                    for r in cur.fetchall()
                ]
            if not claimed:
                return [], 0, (0, {}), None  # type: ignore[return-value]
            return (
                claimed,
                system_user_id(conn),
                active_ruleset(conn),
                active_thresholds(conn),
            )

    def _record_failure(self, tx_id: int, exc: Exception) -> None:
        """NFR-004: a scoring failure must not reject ingestion, and must not spin.

        Its own transaction, because the one that failed is unusable. Attempts
        were already incremented at claim time, so a transaction that keeps
        failing eventually leaves the queue rather than retrying forever.
        """
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scoring_queue
                   SET last_error   = %s,
                       available_at = now() + (interval '5 seconds' * attempts)
                 WHERE transaction_id = %s
                """,
                (str(exc)[:500], tx_id),
            )
            cur.execute(
                "DELETE FROM scoring_queue WHERE transaction_id = %s AND attempts >= %s",
                (tx_id, MAX_ATTEMPTS),
            )

    def run_once(self) -> int:
        """Claim a batch, then score each transaction in its own transaction."""
        claimed, sys_uid, ruleset, thresholds = self.claim()
        if not claimed:
            return 0

        processed = 0
        for tx_id in claimed:
            try:
                # One transaction per scored transaction. This is what keeps the
                # correlation and audit advisory locks short-lived, which is what
                # lets workers actually run in parallel.
                with connection() as conn:
                    score_transaction(
                        conn, tx_id, sys_uid=sys_uid,
                        ruleset=ruleset, thresholds=thresholds,
                    )
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,)
                        )
                processed += 1
            except Exception as exc:  # noqa: BLE001 - must not kill the loop
                log.exception("scoring failed for transaction %s", tx_id)
                try:
                    self._record_failure(tx_id, exc)
                except Exception:  # noqa: BLE001
                    log.exception("could not record failure for %s", tx_id)
        return processed

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        log.info("scoring worker started")
        while not self._stop:
            try:
                if self.run_once() == 0:
                    time.sleep(self.idle_sleep)
            except Exception:  # noqa: BLE001
                log.exception("worker loop error; backing off")
                time.sleep(1.0)
        pool().close()
        log.info("scoring worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    Worker().run_forever()


if __name__ == "__main__":
    main()

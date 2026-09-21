"""The scoring worker.

Plain English
-------------
The engine room. This is the loop that actually scores transactions.

It runs continuously and does the same five things forever: take some
transactions off the queue, work out their features, ask the model and the
rules what they think, write down the decision, and — only if the decision is
serious enough — raise an alert and attach it to a case.

Two details are worth knowing. It always writes the decision, even for the
99% of transactions that turn out to be perfectly ordinary; that record is what
makes it possible to explain a decision months later. And it takes work off the
queue by *reserving* it for sixty seconds rather than holding a database lock,
so several copies of this program can run side by side without tripping over
each other.

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
import os
import signal
import socket
import time
from datetime import datetime, timezone
from typing import Any

from ..audit import chain
from ..cases import assignment, correlation
from ..db import connection, pool
from ..features import MODEL_FEATURE_NAMES, compute_features, load_history_sql, to_vector
from ..features.spec import FEATURE_SPEC_VERSION
from ..features.types import TxView
from ..model import registry
from ..policy import budget
from ..policy import directives
from ..policy import disposition as tiers
from ..policy.engine import Thresholds, apply as apply_policy

# D92: "would the rules have raised this even if the model said zero?" Asked
# with the real policy engine and thresholds no probability can reach, so the
# answer cannot drift from what the policy actually does.
MODEL_SILENT = Thresholds(id=0, version=0, p_monitor=2.0, p_review=2.0, p_hold=2.0, alert_min_level="MEDIUM")
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
              -- D86: live traffic first; replayed history fills the gaps.
              ORDER BY q.priority, q.available_at, q.transaction_id
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
           product_type, origin_sol_id, is_replay, raise_alerts, direction, remitter_token
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


def _machine_action(conn: Any, sys_uid: int, case_id: int, codes: list[str], transaction_ref: str) -> None:
    """Take the action a machine-action signal names (D80).

    The hold is already in the directive, which follows the decision (D74). A
    takeover sequence also places the 24-hour flag (D73, D75), whose customer
    contact is the person's part. A flag already in force is left alone.
    """
    from ..clocks import watchlist

    if "ACCOUNT_TAKEOVER_SEQUENCE" in codes:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
            case = cur.fetchone()
        case = dict(case) if not isinstance(case, dict) else case
        try:
            with conn.transaction():
                watchlist.place(conn, case=case, user_id=sys_uid, hours=24,
                                reason="Machine action: takeover sequence before a payment to a new destination (D80)")
        except watchlist.WatchlistError:
            pass  # already flagged: one flag per customer and per BVN
    chain.append(
        conn,
        actor_user_id=sys_uid,
        action="DISPOSITION_MACHINE_ACTION",
        object_type="case",
        object_id=case_id,
        payload={"signals": codes, "transaction_ref": transaction_ref},
    )


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
        direction=row["direction"],
        remitter_token=row["remitter_token"],
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
    # D78: the model's own inputs; the decision still records every feature.
    vector = to_vector(features, MODEL_FEATURE_NAMES)

    # --- model -------------------------------------------------------------
    rule_only = False
    model_id: int | None = None
    p_fraud = 0.0
    bundle = None
    # D78: the model applies to money leaving; a credit is judged by rules.
    model_applies = tx.direction == "OUTBOUND"
    if model_applies:
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
    result = apply_policy(p_fraud, signals, thresholds, rule_only_mode=rule_only, model_applies=model_applies)

    # --- disposition (WP-08, D80): who acts, the system or a person ---------
    disposition_policy = tiers.active_policy(conn)
    disposition = tiers.choose(actionable=result.actionable, signals=signals, p_fraud=p_fraud,
                               model_applies=model_applies, policy=disposition_policy)

    # --- explanation, only where it is needed -------------------------------
    # G3 requires attributions on **alerts**, not on every decision, and the vast
    # majority of decisions are ALLOW. Explaining all of them cost ~180ms each
    # and would have made NFR-001 unreachable on a laptop. The decision record
    # still stores the model version and the full feature snapshot, so an
    # attribution for a non-alerting decision can always be recomputed exactly.
    attributions: dict[str, float] = {}
    if bundle is not None and result.actionable:
        attributions = bundle.attributions(vector, registry.baseline_for(bundle))

    # --- budget (D86): may this alert reach the desk now? --------------------
    # Asked only of a decision that would alert. The answer goes into the
    # decision's own trace, so "why was this not in my queue until 11:00?" is
    # read from the record like every other why.
    wants_alert = result.actionable and row["raise_alerts"] and disposition != tiers.AUTO_CLOSE
    admission = None
    if wants_alert:
        admission = budget.admit(
            conn,
            mandatory=any(s.power == "OVERRIDE" for s in signals),
            machine=disposition == tiers.MACHINE_ACTION,
            rule_driven=apply_policy(0.0, signals, MODEL_SILENT).actionable,
        )
        result.trace.append(admission.as_trace())

    latency_ms = int((time.perf_counter() - started) * 1000)

    # --- decision: always written, written first (D7a) ----------------------
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decisions
                (transaction_id, p_fraud, score_0_100, decision, risk_level,
                 model_version_id, ruleset_id, threshold_set_id, feature_spec_version,
                 features, signals, attributions, policy_trace, rule_only_mode, latency_ms,
                 disposition, disposition_policy_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                disposition,
                disposition_policy.version if disposition_policy else None,
            ),
        )
        dec_row = cur.fetchone()
        decision_id = int(dec_row["id"] if isinstance(dec_row, dict) else dec_row[0])

    # --- directive (WP-07, D74): the decision in a form a switch can act on --
    # Same transaction as the decision. Replayed history gets none: nobody can
    # act on an instruction about a payment from last month.
    if not row["is_replay"]:
        directives.issue(conn, decision_id=decision_id, transaction_id=transaction_id,
                         decision=result.decision)

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
    outcome["disposition"] = disposition
    # AUTO_CLOSE (D80): recorded on the decision, no alert, no case, no label.
    codes = [s.code for s in signals]
    if wants_alert and admission is not None and admission.raised:
        alert_id, case_id = raise_alert(
            conn, sys_uid,
            decision_id=decision_id, transaction_id=transaction_id, transaction_ref=tx.transaction_ref,
            subject_token=tx.subject_token, occurred_at=row["occurred_at"], risk_level=result.risk_level,
            score=result.score_0_100, signal_codes=codes, disposition=disposition, rule_only=rule_only,
        )
        outcome["alert_id"] = alert_id
        outcome["case_id"] = case_id
    elif wants_alert and admission is not None:
        # D86: held back, not dropped. It waits, most serious first, and is
        # raised when the budget has room (release_deferred).
        deferral_id = budget.record_deferral(
            conn, admission, decision_id=decision_id, transaction_id=transaction_id,
            subject_token=tx.subject_token, risk_level=result.risk_level, p_fraud=result.p_fraud,
            score=result.score_0_100, signal_codes=codes,
        )
        outcome["deferred"] = {"id": deferral_id, "reason": admission.reason}
        _publish(conn, "alert_deferred", {"deferral_id": deferral_id, "risk_level": result.risk_level,
                                          "score": result.score_0_100, "reason": admission.reason})
    if admission is not None and budget.overrun_needs_alarm(conn, admission):
        _raise_alarm(
            conn, sys_uid, "ALERT_BUDGET_OVERRUN",
            f"{admission.raised_today} alerts today against a budget of {admission.config.per_day}, from "
            "veto rules and machine actions the guard may not hold back. Retune those rules.",
        )

    return outcome


def raise_alert(
    conn: Any,
    sys_uid: int,
    *,
    decision_id: int,
    transaction_id: int,
    transaction_ref: str,
    subject_token: str,
    occurred_at: datetime,
    risk_level: str,
    score: int,
    signal_codes: list[str],
    disposition: str,
    rule_only: bool,
    released_from: int | None = None,
) -> tuple[int, int]:
    """Attach an alert to its case, audit it, ring the doorbell. Returns (alert, case)."""
    case_id, created = correlation.attach(
        conn,
        subject_token=subject_token,
        risk_level=risk_level,
        at=occurred_at,
    )
    # D80: a case the system can act on is MACHINE-handled; the first alert
    # that needs a person makes it HUMAN, and it never goes back.
    with conn.cursor() as cur:
        if created and disposition == tiers.MACHINE_ACTION:
            cur.execute("UPDATE cases SET handling = 'MACHINE' WHERE id = %s", (case_id,))
        elif disposition == tiers.HUMAN_REVIEW:
            cur.execute("UPDATE cases SET handling = 'HUMAN' WHERE id = %s AND handling <> 'HUMAN'", (case_id,))
    if disposition == tiers.MACHINE_ACTION:
        _machine_action(conn, sys_uid, case_id, signal_codes, transaction_ref)
    elif created:
        # D94: work reaches an analyst rather than waiting to be claimed. Same
        # transaction as the alert that opened the case, so a case never exists
        # unrouted because something failed in between.
        assignment.route(conn, case_id, sys_uid=sys_uid)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alerts
                (decision_id, transaction_id, case_id, subject_token,
                 risk_level, score_0_100)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (decision_id, transaction_id, case_id, subject_token, risk_level, score),
        )
        a_row = cur.fetchone()
        alert_id = int(a_row["id"] if isinstance(a_row, dict) else a_row[0])

    payload = {
        "case_id": case_id,
        "case_created": created,
        "decision_id": decision_id,
        "transaction_ref": transaction_ref,
        "score": score,
        "signals": signal_codes,
        "rule_only_mode": rule_only,
    }
    if released_from is not None:
        payload["released_from_deferral"] = released_from
    chain.append(
        conn,
        actor_user_id=sys_uid,
        action="ALERT_RAISED",
        object_type="alert",
        object_id=alert_id,
        to_state=risk_level,
        payload=payload,
    )
    _publish(conn, "alert", {"alert_id": alert_id, "case_id": case_id, "risk_level": risk_level, "score": score})
    return alert_id, case_id


_DEFERRAL_SQL = """
    SELECT f.id, f.decision_id, f.transaction_id, f.subject_token, f.risk_level::text AS risk_level,
           f.score_0_100, f.signals, f.rule_driven, d.disposition, d.rule_only_mode,
           t.transaction_ref, t.occurred_at
      FROM alert_deferrals f
      JOIN decisions d ON d.id = f.decision_id
      JOIN transactions t ON t.id = f.transaction_id
"""


def _release(conn: Any, sys_uid: int, item: dict[str, Any], *, released_by: int | None, day: Any,
             hour: int, now: datetime) -> dict[str, Any]:
    alert_id, case_id = raise_alert(
        conn, sys_uid,
        decision_id=item["decision_id"], transaction_id=item["transaction_id"],
        transaction_ref=item["transaction_ref"], subject_token=item["subject_token"],
        occurred_at=item["occurred_at"], risk_level=item["risk_level"], score=int(item["score_0_100"]),
        signal_codes=list(item["signals"] or []), disposition=item["disposition"] or tiers.HUMAN_REVIEW,
        rule_only=bool(item["rule_only_mode"]), released_from=int(item["id"]),
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_deferrals SET state = 'RELEASED', resolved_at = %s, alert_id = %s, released_by = %s "
            "WHERE id = %s",
            (now, alert_id, released_by, item["id"]),
        )
    budget.count_release(conn, day, hour, rule_driven=bool(item.get("rule_driven")))
    return {"deferral_id": int(item["id"]), "alert_id": alert_id, "case_id": case_id}


def release_deferred(conn: Any, sys_uid: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Raise deferred alerts while the budget has room; expire those that waited too long (D86).

    Most serious first: CRITICAL before HIGH, then by probability. Runs in the
    caller's transaction and holds the day's ledger lock throughout, so two
    workers releasing at once cannot overspend.
    """
    now = now or datetime.now(timezone.utc)
    config = budget.load_config(conn)
    day, hour = budget.local_day_hour(now)
    row = budget.lock_day(conn, day, config)
    expired = budget.expire_waiting(conn, day, now)
    for e in expired:
        chain.append(
            conn, actor_user_id=sys_uid, action="ALERT_DEFERRAL_EXPIRED", object_type="decision",
            object_id=e["decision_id"], to_state="EXPIRED",
            payload={"deferral_id": e["id"], "risk_level": e["risk_level"], "p_fraud": str(e["p_fraud"]),
                     "note": "held back by the alert budget and never reached a person"},
        )
    room = budget.headroom(row, hour, config)
    released: list[dict[str, Any]] = []
    # D92: each envelope releases its own, most serious first, and the hour's
    # own ceiling still caps the two together.
    for envelope, rule_driven in (("rules", True), ("model", False)):
        left = min(room[envelope], room["total"] - len(released))
        if left <= 0:
            continue
        with conn.cursor() as cur:
            cur.execute(
                _DEFERRAL_SQL + """
                 WHERE f.state = 'WAITING' AND f.rule_driven = %s
                 ORDER BY f.risk_level DESC, f.p_fraud DESC, f.deferred_at
                 LIMIT %s
                   FOR UPDATE OF f SKIP LOCKED
                """,
                (rule_driven, left),
            )
            items = [dict(r) for r in cur.fetchall()]
        for item in items:
            released.append(_release(conn, sys_uid, item, released_by=None, day=day, hour=hour, now=now))
    if expired:
        _raise_alarm(conn, sys_uid, "ALERTS_EXPIRED_UNREVIEWED",
                     f"{len(expired)} alert(s) waited {config.deferral_hours}h behind the budget and were never "
                     "reviewed. Re-derive thresholds or raise the budget.")
    return {"released": released, "expired": len(expired), "room_before": room}


def release_one(conn: Any, sys_uid: int, deferral_id: int, *, user_id: int,
                now: datetime | None = None) -> dict[str, Any] | None:
    """A supervisor pulls one deferred alert in now, over the budget if need be. Counted and audited."""
    now = now or datetime.now(timezone.utc)
    config = budget.load_config(conn)
    day, hour = budget.local_day_hour(now)
    budget.lock_day(conn, day, config)
    with conn.cursor() as cur:
        cur.execute(_DEFERRAL_SQL + " WHERE f.id = %s AND f.state = 'WAITING' FOR UPDATE OF f", (deferral_id,))
        item = cur.fetchone()
    if not item:
        return None
    done = _release(conn, sys_uid, dict(item), released_by=user_id, day=day, hour=hour, now=now)
    chain.append(conn, actor_user_id=user_id, action="ALERT_DEFERRAL_RELEASED", object_type="decision",
                 object_id=item["decision_id"], to_state="RELEASED",
                 payload={"deferral_id": deferral_id, "alert_id": done["alert_id"], "over_budget_allowed": True})
    return done


# ---------------------------------------------------------------------------
# Notification and alarms
# ---------------------------------------------------------------------------


def _publish(conn: Any, event_type: str, payload: dict[str, Any]) -> int:
    """Durable event row plus an id-only doorbell (D14a); see ``riskradar.events``."""
    from ..events import publish

    return publish(conn, event_type, payload)


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


HEARTBEAT_SECONDS = 10.0
RELEASE_SECONDS = 30.0
ROUTE_SECONDS = 60.0


class Worker:
    # D87: small leases. A worker scores its lease one payment after another, so
    # with 64 claimed the last waited ~2.5 s behind the other 63 while other
    # workers sat idle; at 50 a second that was most of a 9.7 s p95. Eight keeps
    # the claim round trip amortised and the wait inside a lease under half a
    # second. RISKRADAR_WORKER_BATCH overrides it; bulk backfills may prefer more.
    def __init__(self, batch_size: int | None = None, idle_sleep: float = 0.1) -> None:
        self.batch_size = batch_size or int(os.environ.get("RISKRADAR_WORKER_BATCH", "8"))
        self.idle_sleep = idle_sleep
        self._stop = False
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.started_at = datetime.now(timezone.utc)
        self.processed = 0
        self.failed = 0
        self._last_beat = 0.0
        self._last_release = 0.0
        self._last_route = 0.0

    def heartbeat(self, *, force: bool = False) -> None:
        """Readiness reads this to tell an idle worker from a dead one."""
        if not force and time.monotonic() - self._last_beat < HEARTBEAT_SECONDS:
            return
        self._last_beat = time.monotonic()
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_heartbeats (worker_id, host, pid, started_at, seen_at, processed, failed)
                VALUES (%s, %s, %s, %s, now(), %s, %s)
                ON CONFLICT (worker_id) DO UPDATE
                   SET seen_at = now(), processed = EXCLUDED.processed, failed = EXCLUDED.failed
                """,
                (self.worker_id, socket.gethostname(), os.getpid(), self.started_at, self.processed, self.failed),
            )

    def warm_up(self) -> None:
        """Load the model and run one prediction before taking any work (D87).

        Loading the artefact and scikit-learn takes about four seconds. Done
        lazily, the first payments a fresh worker claimed waited for it: a
        freshly started fleet showed a 5.8 s end-to-end spike at 50 a second.
        The heartbeat, and so readiness, follows the warm-up.
        """
        started = time.perf_counter()
        try:
            with connection() as conn:
                bundle = registry.load_active(conn)
                bundle.predict([0.0] * len(MODEL_FEATURE_NAMES))
            log.info("model %s warmed in %.1fs", getattr(bundle, "version", "?"), time.perf_counter() - started)
        except registry.ModelUnavailable as exc:
            log.warning("no model to warm (%s); scoring will run rules-only and raise the alarm", exc)
        self.heartbeat(force=True)

    def maybe_route(self) -> None:
        """D94: cases nobody could be routed to are retried, not forgotten."""
        if time.monotonic() - self._last_route < ROUTE_SECONDS:
            return
        self._last_route = time.monotonic()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM cases WHERE assignee_id IS NULL AND state <> 'CLOSED' "
                            "AND handling = 'HUMAN' LIMIT 1")
                if cur.fetchone() is None:
                    return
            done = assignment.sweep(conn, sys_uid=system_user_id(conn))
        if done["assigned"] or done["waiting"]:
            log.info("routing: %d assigned, %d still waiting for an eligible analyst",
                     done["assigned"], done["waiting"])

    def maybe_release(self) -> None:
        """D86: every half minute, raise deferred alerts the budget now has room for."""
        if time.monotonic() - self._last_release < RELEASE_SECONDS:
            return
        self._last_release = time.monotonic()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM alert_deferrals WHERE state = 'WAITING' LIMIT 1")
                if cur.fetchone() is None:
                    return
            done = release_deferred(conn, system_user_id(conn))
        if done["released"] or done["expired"]:
            log.info("budget: released %d deferred alert(s), %d expired", len(done["released"]), done["expired"])

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
                    # D87: no wait for the disk flush at commit. The decision,
                    # its alert and the queue row's deletion commit together, so
                    # a crash that loses the last fraction of a second loses all
                    # of them and the lease brings the payment back to be scored
                    # again: nothing half-done, nothing lost for good. On this
                    # machine the flush was about half of each payment's time.
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL synchronous_commit TO OFF")
                    score_transaction(
                        conn, tx_id, sys_uid=sys_uid,
                        ruleset=ruleset, thresholds=thresholds,
                    )
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM scoring_queue WHERE transaction_id = %s", (tx_id,)
                        )
                processed += 1
                self.processed += 1
            except Exception as exc:  # noqa: BLE001 - must not kill the loop
                self.failed += 1
                log.exception("scoring failed for transaction %s", tx_id)
                try:
                    self._record_failure(tx_id, exc)
                except Exception:  # noqa: BLE001
                    log.exception("could not record failure for %s", tx_id)
        return processed

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.warm_up()
        log.info("scoring worker %s started", self.worker_id)
        while not self._stop:
            try:
                self.heartbeat()
                self.maybe_release()
                self.maybe_route()
                if self.run_once() == 0:
                    time.sleep(self.idle_sleep)
            except Exception:  # noqa: BLE001
                log.exception("worker loop error; backing off")
                time.sleep(1.0)
        try:
            with connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM worker_heartbeats WHERE worker_id = %s", (self.worker_id,))
        except Exception:  # noqa: BLE001 - leaving is best effort; readiness ages it out
            log.exception("could not remove heartbeat")
        pool().close()
        log.info("scoring worker stopped")


def main() -> None:
    from ..logsetup import configure

    configure()
    Worker().run_forever()


if __name__ == "__main__":
    main()

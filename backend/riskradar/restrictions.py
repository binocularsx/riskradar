"""Delivering an approved restriction to the bank, and recording what it did (D97).

Plain English
-------------
When a lead approves a fraud finding that asks the bank to restrict a customer's
account (D93), that ask has to reach the bank and its outcome has to come back.
Three stages, and Risk Radar still never restricts anything itself (D7):

1. **Outbox.** On approval, one *restriction order* per ask is written, and one
   outbox message beside it, in the same transaction as the approval — so an
   approved restriction can never exist without its message.
2. **Execution.** A sweep hands due messages to the bank's restriction connector
   (unconnected by default, so a message waits in the outbox with its reason,
   visibly — never silently). The bank applies the restriction on its side; the
   order records when the bank was first told.
3. **Reconciliation.** The bank acknowledges the outcome — APPLIED, NOT_APPLIED
   or REJECTED — once. Advice nobody confirms is advice nobody should trust, so
   the record shows what was asked, when the bank was told, and what it did.

Everything is tokens (D9c, D9d): the bank maps a token to the real account, card
or destination on its own side. Every step is written to the hash-chained record.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import chain
from .events import publish
from .identity import adapters

log = logging.getLogger("riskradar.restrictions")

MAX_ATTEMPTS = 10
NOT_CONNECTED_RETRY = timedelta(minutes=5)
ACK_OUTCOMES = ("APPLIED", "NOT_APPLIED", "REJECTED")


class RestrictionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. Create the orders on approval (transactional outbox)
# ---------------------------------------------------------------------------


def create_orders(conn: Any, submission: dict[str, Any], approver_id: int) -> list[dict[str, Any]]:
    """Write one order and one outbox message per restriction on an approved
    submission, in the caller's (the approval's) transaction. Idempotent: a
    submission already turned into orders is not turned into them again."""
    restrictions = submission.get("restrictions")
    if isinstance(restrictions, str):
        restrictions = json.loads(restrictions)
    if not restrictions:
        return []

    existing = _rows(
        conn, "SELECT count(*) AS n FROM restriction_orders WHERE submission_id = %s",
        (submission["id"],),
    )[0]["n"]
    if existing:
        return []  # approval replayed; the orders are already there

    created: list[dict[str, Any]] = []
    for item in restrictions:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO restriction_orders
                    (submission_id, case_id, action, account_token, beneficiary_token,
                     channel, transaction_ref, reason, approved_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, restriction_ref, action, account_token, beneficiary_token,
                          channel, transaction_ref, reason, case_id
                """,
                (submission["id"], submission["case_id"], item["action"],
                 item.get("account_token"), item.get("beneficiary_token"),
                 item.get("channel"), item.get("transaction_ref"),
                 item.get("reason"), approver_id),
            )
            order = dict(cur.fetchone())
        payload = {
            "restriction_ref": str(order["restriction_ref"]),
            "action": order["action"],
            "account_token": order["account_token"],
            "beneficiary_token": order["beneficiary_token"],
            "channel": order["channel"],
            "transaction_ref": order["transaction_ref"],
            "reason": order["reason"],
            "case_id": order["case_id"],
        }
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO restriction_outbox (order_id, payload) VALUES (%s, %s)",
                (order["id"], json.dumps(payload)),
            )
        chain.append(
            conn, actor_user_id=approver_id, action="RESTRICTION_ISSUED",
            object_type="restriction_order", object_id=order["id"],
            to_state=order["action"],
            payload={"case_id": order["case_id"], "submission_id": submission["id"],
                     "restriction_ref": str(order["restriction_ref"]),
                     "target": order["account_token"] or order["beneficiary_token"],
                     "channel": order["channel"]},
        )
        created.append(order)

    if created:
        publish(conn, "restrictions_issued",
                {"case_id": submission["case_id"], "count": len(created)})
    return created


# ---------------------------------------------------------------------------
# 2. Dispatch — hand due messages to the bank's connector
# ---------------------------------------------------------------------------


def paused(conn: Any) -> dict[str, Any]:
    """The emergency switch (D103, plan §5.3).

    Outbound delivery can be stopped while detection, investigation and
    approval carry on. Paused means messages **queue**: the outbox is durable,
    so nothing is lost and everything goes out in order when it is lifted.
    """
    rows = _rows(conn, "SELECT key, value FROM app_config WHERE key IN "
                       "('restriction_delivery_paused', 'restriction_pause_reason')")
    values = {r["key"]: r["value"] for r in rows}
    return {"paused": bool(values.get("restriction_delivery_paused", False)),
            "reason": values.get("restriction_pause_reason") or ""}


def set_paused(conn: Any, *, on: bool, reason: str, actor: dict[str, Any]) -> dict[str, Any]:
    before = paused(conn)
    with conn.cursor() as cur:
        for key, value in (("restriction_delivery_paused", json.dumps(on)),
                           ("restriction_pause_reason", json.dumps(reason if on else ""))):
            cur.execute(
                "INSERT INTO app_config (key, value, updated_at, updated_by) VALUES (%s, %s, now(), %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by",
                (key, value, actor["id"]),
            )
    waiting = _rows(conn, "SELECT count(*) AS n FROM restriction_outbox WHERE status = 'PENDING'")[0]["n"]
    chain.append(
        conn, actor_user_id=actor["id"],
        action="RESTRICTION_DELIVERY_PAUSED" if on else "RESTRICTION_DELIVERY_RESUMED",
        object_type="app_config", object_id="restriction_delivery",
        from_state=str(before["paused"]), to_state=str(on),
        payload={"reason": reason, "queued_messages": int(waiting)},
    )
    publish(conn, "restriction_delivery", {"paused": on, "reason": reason, "queued": int(waiting)})
    return {"paused": on, "reason": reason, "queued_messages": int(waiting)}


def dispatch(conn: Any, *, connector: adapters.RestrictionConnector | None = None,
             now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    """Send due outbox messages. Runs inside the caller's transaction."""
    connector = connector or adapters.restriction_connector()
    now = now or datetime.now(timezone.utc)
    if paused(conn)["paused"]:
        # D103: held, not dropped. The queue depth is what the desk sees.
        waiting = _rows(conn, "SELECT count(*) AS n FROM restriction_outbox WHERE status = 'PENDING'")[0]["n"]
        return {"sent": 0, "waiting": int(waiting), "failed": 0, "paused": 1}
    due = _rows(
        conn,
        "SELECT id, order_id, payload, attempts FROM restriction_outbox "
        "WHERE status = 'PENDING' AND next_attempt_at <= %s ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
        (now, limit),
    )
    counts = {"sent": 0, "waiting": 0, "failed": 0}
    for row in due:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        attempts = row["attempts"] + 1
        try:
            ref = connector.publish(payload)
        except adapters.NotConnected as exc:
            _retry(conn, row["id"], attempts=attempts, error=str(exc),
                   next_at=now + NOT_CONNECTED_RETRY)
            counts["waiting"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - a bank outage must not stop the sweep
            final = attempts >= MAX_ATTEMPTS
            _retry(conn, row["id"], attempts=attempts, error=f"{type(exc).__name__}: {exc}",
                   next_at=now + timedelta(seconds=30 * 2 ** min(attempts, 8)),
                   status="FAILED" if final else "PENDING")
            counts["failed" if final else "waiting"] += 1
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE restriction_outbox SET status = 'SENT', attempts = %s, sent_at = %s, "
                "external_ref = %s, last_error = NULL WHERE id = %s",
                (attempts, now, ref, row["id"]),
            )
            # The order records that the bank has been told.
            cur.execute(
                "UPDATE restriction_orders SET first_delivered_at = COALESCE(first_delivered_at, %s), "
                "delivery_count = delivery_count + 1 WHERE id = %s",
                (now, row["order_id"]),
            )
        counts["sent"] += 1
    return counts


def _retry(conn: Any, outbox_id: int, *, attempts: int, error: str,
           next_at: datetime, status: str = "PENDING") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE restriction_outbox SET status = %s, attempts = %s, last_error = %s, "
            "next_attempt_at = %s WHERE id = %s",
            (status, attempts, error[:500], next_at, outbox_id),
        )


# ---------------------------------------------------------------------------
# The bank poll feed, and reconciliation
# ---------------------------------------------------------------------------

_ORDER_COLUMNS = """
    id, restriction_ref, submission_id, case_id, action::text AS action,
    account_token, beneficiary_token, channel, reason, issued_at,
    first_delivered_at, delivery_count, acknowledged_at, ack_outcome::text AS ack_outcome,
    ack_reason, ack_taken_at
"""


def mark_delivered(conn: Any, order_ids: list[int], *, now: datetime | None = None) -> None:
    if not order_ids:
        return
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE restriction_orders SET first_delivered_at = COALESCE(first_delivered_at, %s), "
            "delivery_count = delivery_count + 1 WHERE id = ANY(%s)",
            (now, order_ids),
        )


def feed(conn: Any, *, after_id: int, limit: int) -> list[dict[str, Any]]:
    return _rows(conn, f"SELECT {_ORDER_COLUMNS} FROM restriction_orders "
                       "WHERE id > %s ORDER BY id LIMIT %s", (after_id, limit))


def by_ref(conn: Any, restriction_ref: str) -> dict[str, Any] | None:
    rows = _rows(conn, f"SELECT {_ORDER_COLUMNS} FROM restriction_orders WHERE restriction_ref = %s",
                 (restriction_ref,))
    return rows[0] if rows else None


def for_case(conn: Any, case_id: int) -> list[dict[str, Any]]:
    return _rows(conn, f"SELECT {_ORDER_COLUMNS} FROM restriction_orders "
                       "WHERE case_id = %s ORDER BY issued_at", (case_id,))


def acknowledge(conn: Any, *, restriction_ref: str, outcome: str, reason: str | None,
                taken_at: datetime | None) -> dict[str, Any]:
    """The bank reports what it did. Once; a repeat with the same answer is a
    no-op, a different answer is a 409 (the directive-ack posture, D74)."""
    if outcome not in ACK_OUTCOMES:
        raise RestrictionError(422, f"outcome must be one of {', '.join(ACK_OUTCOMES)}")
    rows = _rows(conn, "SELECT * FROM restriction_orders WHERE restriction_ref = %s FOR UPDATE",
                 (restriction_ref,))
    if not rows:
        raise RestrictionError(404, "unknown restriction_ref")
    order = rows[0]
    now = datetime.now(timezone.utc)
    taken = taken_at or now

    if order["acknowledged_at"] is not None:
        same = (order["ack_outcome"], order["ack_reason"]) == (outcome, reason)
        if not same:
            raise RestrictionError(409, "this restriction is already acknowledged with a different answer")
        return {"status": "duplicate", "restriction_ref": restriction_ref}

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE restriction_orders SET acknowledged_at = %s, ack_outcome = %s, ack_reason = %s, "
            "ack_taken_at = %s, first_delivered_at = COALESCE(first_delivered_at, %s) "
            "WHERE id = %s",
            (now, outcome, reason, taken, now, order["id"]),
        )
    chain.append(
        conn, actor_user_id=_system_user(conn), action="RESTRICTION_ACKNOWLEDGED",
        object_type="restriction_order", object_id=order["id"], to_state=outcome,
        payload={"restriction_ref": restriction_ref, "case_id": order["case_id"], "reason": reason},
    )
    if order["action"] == "NOTIFY_RECEIVING_BANK" and outcome == "APPLIED":
        _stamp_counterparty_notified(conn, order, at=taken)
    publish(conn, "restriction_acknowledged",
            {"case_id": order["case_id"], "outcome": outcome})
    return {"status": "acknowledged", "restriction_ref": restriction_ref, "outcome": outcome}


def _stamp_counterparty_notified(conn: Any, order: dict[str, Any], *, at: datetime) -> None:
    """D109b: support telling the receiving bank is the CBN counterparty-notified
    milestone (D71), once the customer has reported. Stamped once, never moved."""
    case = _rows(conn, "SELECT first_reported_at, counterparty_notified_at FROM cases WHERE id = %s FOR UPDATE",
                 (order["case_id"],))
    if not case or not case[0]["first_reported_at"] or case[0]["counterparty_notified_at"]:
        return
    at = max(at, case[0]["first_reported_at"])
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET counterparty_notified_at = %s, "
                    "counterparty_institution = COALESCE(counterparty_institution, 'receiving bank') WHERE id = %s",
                    (at, order["case_id"]))
    chain.append(conn, actor_user_id=_system_user(conn), action="CLOCK_COUNTERPARTY_NOTIFIED", object_type="case",
                 object_id=order["case_id"], payload={"via": "support acknowledged NOTIFY_RECEIVING_BANK",
                                                      "restriction_ref": str(order["restriction_ref"])})


def order_by_ref(conn: Any, restriction_ref: str) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM restriction_orders WHERE restriction_ref = %s", (restriction_ref,))
    return rows[0] if rows else None


# D109b: requests to the support team that are done once, not a restriction
# left in place. There is nothing to lift afterwards.
ONE_OFF_ACTIONS = frozenset({"CONTACT_CUSTOMER", "VERIFY_IDENTITY", "NOTIFY_RECEIVING_BANK",
                             "TRANSACTION_REVERSAL", "SESSION_TERMINATION", "CREDENTIAL_RESET",
                             "MFA_REENROLMENT"})


def release_blockers(conn: Any, order: dict[str, Any]) -> list[str]:
    """Why this restriction cannot be lifted right now. Empty means it can."""
    blockers = []
    if order["action"] in ONE_OFF_ACTIONS:
        blockers.append("this was a one-off request, not a restriction left in place; there is nothing to lift")
    if order["kind"] != "RESTRICT":
        blockers.append("this is already a release, not a restriction")
    if order["acknowledged_at"] is None:
        blockers.append("the bank has not said what it did yet; a lift of an unapplied restriction is noise")
    elif order["ack_outcome"] != "APPLIED":
        blockers.append(f"the bank did not apply it ({order['ack_outcome']}), so there is nothing to lift")
    existing = _rows(conn, "SELECT id, restriction_ref FROM restriction_orders WHERE releases_order_id = %s",
                     (order["id"],))
    if existing:
        blockers.append(f"a lift already exists ({existing[0]['restriction_ref']})")
    return blockers


def create_release(conn: Any, *, order: dict[str, Any], approver_id: int, reason: str,
                   proposed_by: int | None = None) -> dict[str, Any]:
    """Issue the lift of an applied restriction, with its own outbox message.

    Never an edit of the original: the pair — what was asked, and what lifted
    it — is the record. Runs in the approval's transaction (D103).
    """
    blockers = release_blockers(conn, order)
    if blockers:
        raise RestrictionError(409, "; ".join(blockers))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO restriction_orders
                (submission_id, case_id, action, account_token, beneficiary_token, channel, reason,
                 approved_by, kind, releases_order_id)
            VALUES (%(submission)s, %(case)s, %(action)s, %(account)s, %(beneficiary)s, %(channel)s,
                    %(reason)s, %(approver)s, 'RELEASE', %(original)s)
            RETURNING id, restriction_ref, action, account_token, beneficiary_token, channel, case_id
            """,
            {"submission": order["submission_id"], "case": order["case_id"], "action": order["action"],
             "account": order["account_token"], "beneficiary": order["beneficiary_token"],
             "channel": order["channel"], "reason": reason, "approver": approver_id, "original": order["id"]},
        )
        release = dict(cur.fetchone())
    payload = {
        "restriction_ref": str(release["restriction_ref"]),
        "kind": "RELEASE",
        "lifts": str(order["restriction_ref"]),
        "action": release["action"],
        "account_token": release["account_token"],
        "beneficiary_token": release["beneficiary_token"],
        "channel": release["channel"],
        "reason": reason,
        "case_id": release["case_id"],
    }
    with conn.cursor() as cur:
        cur.execute("INSERT INTO restriction_outbox (order_id, payload) VALUES (%s, %s)",
                    (release["id"], json.dumps(payload)))
    chain.append(
        conn, actor_user_id=approver_id, action="RESTRICTION_RELEASED",
        object_type="restriction_order", object_id=release["id"], to_state="RELEASE",
        payload={"lifts": str(order["restriction_ref"]), "case_id": release["case_id"],
                 "reason": reason, "proposed_by": proposed_by},
    )
    publish(conn, "restriction_released", {"case_id": release["case_id"],
                                           "restriction_ref": str(release["restriction_ref"]),
                                           "lifts": str(order["restriction_ref"])})
    return release


def reconcile(conn: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """What the bank owes us an answer on, and what has outlived its reason.

    Two mismatches the plan asks to own rather than discover (§5.3):

    * **delivered, never acknowledged** past the configured window. The bank was
      told and has not said what it did, so nobody knows whether the customer is
      restricted. It becomes a named exception, not a silence.
    * **expired but still standing**: a temporary restriction whose time has
      passed and which the bank applied. The lift is proposed automatically, and
      still takes a second person to approve — an automatic release would be a
      customer-impacting action nobody authorised.
    """
    now = now or datetime.now(timezone.utc)
    hours = _rows(conn, "SELECT value FROM app_config WHERE key = 'restriction_ack_overdue_hours'")
    overdue_hours = float(hours[0]["value"]) if hours else 24.0
    stale = _rows(
        conn,
        """
        SELECT o.id, o.restriction_ref, o.case_id, o.action, o.kind, o.first_delivered_at,
               extract(epoch FROM now() - o.first_delivered_at)::int AS waiting_seconds
          FROM restriction_orders o
         WHERE o.first_delivered_at IS NOT NULL AND o.acknowledged_at IS NULL
           AND o.first_delivered_at < %s - make_interval(hours => %s::int)
         ORDER BY o.first_delivered_at
        """,
        (now, overdue_hours),
    )
    undelivered = _rows(
        conn,
        """
        SELECT o.id, o.restriction_ref, o.case_id, o.action, b.attempts, b.last_error, b.status
          FROM restriction_orders o JOIN restriction_outbox b ON b.order_id = o.id
         WHERE o.first_delivered_at IS NULL AND b.status <> 'SENT'
         ORDER BY b.created_at
        """,
    )
    expired = _rows(
        conn,
        """
        SELECT o.* FROM restriction_orders o
         WHERE o.kind = 'RESTRICT' AND o.expires_at IS NOT NULL AND o.expires_at <= %s
           AND o.ack_outcome = 'APPLIED'
           AND NOT EXISTS (SELECT 1 FROM restriction_orders r WHERE r.releases_order_id = o.id)
        """,
        (now,),
    )
    return {
        "checked_at": now,
        "ack_overdue_hours": overdue_hours,
        "delivered_not_acknowledged": stale,
        "not_delivered": undelivered,
        "expired_still_standing": expired,
        "clean": not (stale or undelivered or expired),
    }


def _system_user(conn: Any) -> int:
    """The bank's acknowledgement is a machine action; the actor is never null
    (D12c), so it is attributed to the reserved system principal."""
    return _rows(conn, "SELECT id FROM users WHERE is_system LIMIT 1")[0]["id"]


def status(conn: Any) -> dict[str, Any]:
    """What an operator needs: the connector in use, delivery health, and what
    the bank has said about the recommendations sent to it."""
    outbox = _rows(conn, "SELECT status, count(*) AS n, "
                         "max(last_error) FILTER (WHERE last_error IS NOT NULL) AS last_error, "
                         "min(created_at) AS oldest FROM restriction_outbox GROUP BY status ORDER BY status")
    lifecycle = _rows(conn, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE first_delivered_at IS NULL) AS awaiting_delivery,
               count(*) FILTER (WHERE first_delivered_at IS NOT NULL AND acknowledged_at IS NULL) AS awaiting_ack,
               count(*) FILTER (WHERE acknowledged_at IS NOT NULL) AS acknowledged
          FROM restriction_orders
    """)[0]
    by_outcome = _rows(conn, "SELECT ack_outcome::text AS outcome, count(*) AS n FROM restriction_orders "
                             "WHERE acknowledged_at IS NOT NULL GROUP BY 1 ORDER BY 1")
    by_action = _rows(conn, "SELECT action::text AS action, count(*) AS n FROM restriction_orders GROUP BY 1 ORDER BY 1")
    return {
        "connector": adapters.restriction_connector().name,
        "outbox": outbox,
        "lifecycle": {k: int(v) for k, v in lifecycle.items()},
        "by_outcome": by_outcome,
        "by_action": by_action,
    }

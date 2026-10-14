"""Telling the support team (D109; was the account-manager report, D106).

Plain English
-------------
The fraud desk is not customer-facing. It investigates and recommends; the
bank's support team owns every contact with a customer and every action on a
customer's profile. So everything the desk concludes has to reach support, and
this module is how:

* **Report** — written when a lead approves a finding. It carries the outcome,
  the reasons, and the actions recommended to support (each with the reference
  support acknowledges it by, D97). Sent for confirmed fraud always; for a case
  support reported, whatever the outcome, because support asked and is owed an
  answer; and not at all for a case the detector raised and the desk cleared,
  so support only hears about real issues (D109c).
* **Heads-up** — on a Critical case, one immediate "hold, we are investigating"
  before the report, because a lead's approval takes time and money may still
  be leaving (D109d).
* **Contact request** — a customer placed on the 24-hour watch must be
  contacted within the window, and contacting customers is support's job
  (D109f).

Support is an **external party** with no login (D109a), reached through the
same transactional outbox the bank's actions ride on: written in the caller's
transaction, retried until it lands, and visible on the case while in flight.
Risk Radar still does nothing to the customer (D7).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import adapters

MAX_ATTEMPTS = 8
NOT_CONNECTED_RETRY = timedelta(minutes=5)

ADVISORY = ("The fraud desk recommends; support decides and acts on the customer's profile. "
            "Nothing here has been done to the customer by Risk Radar.")


class SupportMessageError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _exposure(conn: Any, case_id: int) -> dict[str, Any]:
    row = _rows(
        conn,
        """
        SELECT coalesce(sum(t.amount_minor), 0) AS exposure_minor,
               count(DISTINCT t.id)             AS transactions,
               min(t.occurred_at)               AS first_seen,
               max(t.occurred_at)               AS last_seen
          FROM alerts a JOIN transactions t ON t.id = a.transaction_id
         WHERE a.case_id = %s
        """,
        (case_id,),
    )[0]
    return {
        "exposure_minor": int(row["exposure_minor"]),
        "transactions": int(row["transactions"]),
        "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
        "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
    }


def _insert(conn: Any, *, kind: str, case: dict[str, Any], payload: dict[str, Any],
            submission_id: int | None = None) -> dict[str, Any]:
    payload = {"kind": kind, "case_id": case["id"], "subject_token": case["subject_token"],
               "support_ticket_ref": case.get("support_ticket_ref"),
               **_exposure(conn, case["id"]), **payload, "advisory": ADVISORY}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO support_reports (kind, case_id, submission_id, subject_token, payload)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, report_ref, kind, case_id, status, created_at
            """,
            (kind, case["id"], submission_id, case["subject_token"], json.dumps(payload)),
        )
        row = dict(cur.fetchone())
    # The recipient-facing handle belongs in the message it identifies.
    payload["report_ref"] = str(row["report_ref"])
    with conn.cursor() as cur:
        cur.execute("UPDATE support_reports SET payload = %s WHERE id = %s",
                    (json.dumps(payload), row["id"]))
    return row


# ---------------------------------------------------------------------------
# 1. Raise them (transactional outbox)
# ---------------------------------------------------------------------------


def should_report(submission: dict[str, Any], case: dict[str, Any]) -> bool:
    """D109c: confirmed fraud always; anything support reported, always; a
    detector-raised case cleared by the desk, never."""
    return (submission["proposed_outcome"] == "CONFIRMED_FRAUD"
            or case.get("first_reported_at") is not None)


def create_report(conn: Any, submission: dict[str, Any], case: dict[str, Any],
                  approver_id: int, orders: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Write the report in the caller's (the approval's) transaction.

    Idempotent by construction. ``UNIQUE (submission_id)`` means an approval
    replayed after a crash finds the row already there rather than telling
    support a second time, which is the failure this pattern exists to prevent.
    """
    if not should_report(submission, case):
        return None
    if _rows(conn, "SELECT 1 FROM support_reports WHERE submission_id = %s", (submission["id"],)):
        return None
    return _insert(conn, kind="REPORT", case=case, submission_id=submission["id"], payload={
        "outcome": submission["proposed_outcome"],
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "rationale": submission.get("rationale"),
        "reported_by_support": case.get("first_reported_at") is not None,
        "actions_recommended": [
            {"action": o["action"], "account_token": o.get("account_token"),
             "beneficiary_token": o.get("beneficiary_token"),
             "transaction_ref": o.get("transaction_ref"),
             "restriction_ref": str(o["restriction_ref"])}
            for o in (orders or [])
        ],
    })


def create_heads_up(conn: Any, case: dict[str, Any], *, sent_by: dict[str, Any],
                    message: str) -> dict[str, Any]:
    """D109d: one immediate "hold, we are investigating" on a Critical case."""
    if case["risk_level"] != "CRITICAL":
        raise SupportMessageError(409, "an urgent heads-up is only for Critical cases; "
                                       "support hears about this one when a lead approves the report")
    if case["state"] == "CLOSED":
        raise SupportMessageError(409, "this case is closed")
    if _rows(conn, "SELECT 1 FROM support_reports WHERE case_id = %s AND kind = 'HEADS_UP'", (case["id"],)):
        raise SupportMessageError(409, "support has already had the urgent heads-up for this case")
    return _insert(conn, kind="HEADS_UP", case=case, payload={
        "message": message, "sent_by": sent_by.get("display_name"),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    })


def create_contact_request(conn: Any, case: dict[str, Any], *, contact_by: datetime,
                           reason: str) -> dict[str, Any]:
    """D109f: the 24-hour watch needs a customer contact, which is support's."""
    return _insert(conn, kind="CONTACT_REQUEST", case=case, payload={
        "contact_by": contact_by.isoformat(), "reason": reason,
    })


# ---------------------------------------------------------------------------
# 2. Deliver them
# ---------------------------------------------------------------------------


def dispatch(conn: Any, *, connector: adapters.SupportConnector | None = None,
             now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    """Send due messages. Runs inside the caller's transaction, like the
    restriction dispatcher, so a failure rolls back with everything else."""
    connector = connector or adapters.support_connector()
    now = now or datetime.now(timezone.utc)
    due = _rows(
        conn,
        "SELECT id, payload, attempts FROM support_reports "
        "WHERE status = 'PENDING' AND next_attempt_at <= %s ORDER BY id LIMIT %s "
        "FOR UPDATE SKIP LOCKED",
        (now, limit),
    )
    counts = {"sent": 0, "waiting": 0, "failed": 0}
    for row in due:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        attempts = row["attempts"] + 1
        try:
            ref = connector.publish(payload)
        except adapters.NotConnected as exc:
            # Not configured is not a failure: the message waits rather than
            # burning its attempts against a connector nobody has set up yet.
            _retry(conn, row["id"], attempts=attempts, error=str(exc),
                   next_at=now + NOT_CONNECTED_RETRY)
            counts["waiting"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad recipient must not stop the sweep
            final = attempts >= MAX_ATTEMPTS
            _retry(conn, row["id"], attempts=attempts, error=f"{type(exc).__name__}: {exc}",
                   next_at=now + timedelta(seconds=30 * 2 ** min(attempts, 8)),
                   status="FAILED" if final else "PENDING")
            counts["failed" if final else "waiting"] += 1
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE support_reports SET status = 'SENT', attempts = %s, sent_at = %s, "
                "external_ref = %s, last_error = NULL WHERE id = %s",
                (attempts, now, ref, row["id"]),
            )
        counts["sent"] += 1
    return counts


def _retry(conn: Any, report_id: int, *, attempts: int, error: str,
           next_at: datetime, status: str = "PENDING") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE support_reports SET status = %s, attempts = %s, last_error = %s, "
            "next_attempt_at = %s WHERE id = %s",
            (status, attempts, error[:500], next_at, report_id),
        )


# ---------------------------------------------------------------------------
# 3. Read them back
# ---------------------------------------------------------------------------


def for_case(conn: Any, case_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT report_ref, kind, status, attempts, last_error, created_at, sent_at, external_ref,
               payload->>'outcome' AS outcome
          FROM support_reports WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    )


def status(conn: Any) -> dict[str, Any]:
    """Delivery health, for the same reason the restrictions have it (FR-305):
    a message nobody received is worse than none, because the desk believes
    support was told."""
    counts = _rows(conn, "SELECT status, count(*) AS n FROM support_reports GROUP BY status")
    oldest = _rows(
        conn, "SELECT min(created_at) AS oldest FROM support_reports WHERE status = 'PENDING'",
    )[0]["oldest"]
    return {
        "by_status": {r["status"]: int(r["n"]) for r in counts},
        "oldest_waiting": oldest,
        "connector": adapters.support_connector().name,
    }

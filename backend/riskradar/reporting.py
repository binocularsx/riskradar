"""Telling the customer's account manager about confirmed fraud (D106).

Plain English
-------------
When a lead approves a fraud finding, three things already happened: the case
took its outcome, the destinations became a deterministic veto for everyone else
(KNOWN_MULE, D11a), and whatever restrictions the finding asked for went to the
bank (D97). Nobody who owns the *customer relationship* was told. The desk knew;
the person who would have to ring that customer found out when the customer rang
them.

This closes that. The account manager is an **external party**, exactly like the
bank: no login and no role inside Risk Radar, so the four-role separation of
duties (D12b) is untouched. They get a message through the same transactional
outbox pattern the restrictions ride on — written in the approval's own
transaction, retried until it lands, and visible on the case while it is in
flight.

Risk Radar still does nothing to the customer (D7). This is a report about a
decision a human made, not an instruction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import adapters

MAX_ATTEMPTS = 8
NOT_CONNECTED_RETRY = timedelta(minutes=5)


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. Raise the report on approval (transactional outbox)
# ---------------------------------------------------------------------------


def create_report(conn: Any, submission: dict[str, Any], case: dict[str, Any],
                  approver_id: int, orders: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Write the report in the caller's (the approval's) transaction.

    Only for confirmed fraud: a false positive or an inconclusive case is not
    news for an account manager, and reporting those would train them to ignore
    the channel that matters.

    Idempotent by construction. ``UNIQUE (submission_id)`` means an approval
    replayed after a crash finds the row already there rather than telling the
    account manager a second time, which is the failure this pattern exists to
    prevent.
    """
    if submission["proposed_outcome"] != "CONFIRMED_FRAUD":
        return None

    existing = _rows(
        conn, "SELECT id, report_ref FROM account_manager_reports WHERE submission_id = %s",
        (submission["id"],),
    )
    if existing:
        return None

    restrictions = submission.get("restrictions")
    if isinstance(restrictions, str):
        restrictions = json.loads(restrictions)

    # What the manager needs to act: whose account, what was decided, what the
    # bank has been asked to do, and how much is exposed. Tokens, not names —
    # identifiers are one-way HMACs everywhere else (D9c) and a report is not a
    # reason to undo that.
    exposure = _rows(
        conn,
        """
        SELECT coalesce(sum(t.amount_minor), 0) AS exposure_minor,
               count(DISTINCT t.id)             AS transactions,
               min(t.occurred_at)               AS first_seen,
               max(t.occurred_at)               AS last_seen
          FROM alerts a JOIN transactions t ON t.id = a.transaction_id
         WHERE a.case_id = %s
        """,
        (case["id"],),
    )[0]

    payload = {
        "case_id": case["id"],
        "subject_token": case["subject_token"],
        "outcome": "CONFIRMED_FRAUD",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "rationale": submission.get("rationale"),
        "exposure_minor": int(exposure["exposure_minor"]),
        "transactions": int(exposure["transactions"]),
        "first_seen": exposure["first_seen"].isoformat() if exposure["first_seen"] else None,
        "last_seen": exposure["last_seen"].isoformat() if exposure["last_seen"] else None,
        "restrictions_recommended": [
            {"action": o["action"], "account_token": o.get("account_token"),
             "beneficiary_token": o.get("beneficiary_token"),
             "restriction_ref": str(o["restriction_ref"])}
            for o in (orders or [])
        ],
        "advisory": ("Risk Radar recommends and records; the bank applies and confirms. "
                     "No restriction here has been enforced by this system."),
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_manager_reports
                (case_id, submission_id, subject_token, payload)
            VALUES (%s, %s, %s, %s)
            RETURNING id, report_ref, case_id, status, created_at
            """,
            (case["id"], submission["id"], case["subject_token"], json.dumps(payload)),
        )
        report = dict(cur.fetchone())
    # The recipient-facing handle belongs in the message it identifies.
    payload["report_ref"] = str(report["report_ref"])
    with conn.cursor() as cur:
        cur.execute("UPDATE account_manager_reports SET payload = %s WHERE id = %s",
                    (json.dumps(payload), report["id"]))
    return report


# ---------------------------------------------------------------------------
# 2. Deliver it
# ---------------------------------------------------------------------------


def dispatch(conn: Any, *, connector: adapters.AccountManagerConnector | None = None,
             now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    """Send due reports. Runs inside the caller's transaction, like the
    restriction dispatcher, so a failure rolls back with everything else."""
    connector = connector or adapters.account_manager_connector()
    now = now or datetime.now(timezone.utc)
    due = _rows(
        conn,
        "SELECT id, payload, attempts FROM account_manager_reports "
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
            # Not configured is not a failure: the report waits rather than
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
                "UPDATE account_manager_reports SET status = 'SENT', attempts = %s, sent_at = %s, "
                "external_ref = %s, last_error = NULL WHERE id = %s",
                (attempts, now, ref, row["id"]),
            )
        counts["sent"] += 1
    return counts


def _retry(conn: Any, report_id: int, *, attempts: int, error: str,
           next_at: datetime, status: str = "PENDING") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE account_manager_reports SET status = %s, attempts = %s, last_error = %s, "
            "next_attempt_at = %s WHERE id = %s",
            (status, attempts, error[:500], next_at, report_id),
        )


# ---------------------------------------------------------------------------
# 3. Read it back
# ---------------------------------------------------------------------------


def for_case(conn: Any, case_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT report_ref, status, attempts, last_error, created_at, sent_at, external_ref
          FROM account_manager_reports WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    )


def status(conn: Any) -> dict[str, Any]:
    """Delivery health, for the same reason the restrictions have it (FR-305):
    a report nobody received is worse than no report, because the desk believes
    the account manager was told."""
    counts = _rows(
        conn,
        "SELECT status, count(*) AS n FROM account_manager_reports GROUP BY status",
    )
    oldest = _rows(
        conn,
        "SELECT min(created_at) AS oldest FROM account_manager_reports WHERE status = 'PENDING'",
    )[0]["oldest"]
    return {
        "by_status": {r["status"]: int(r["n"]) for r in counts},
        "oldest_waiting": oldest,
        "connector": adapters.account_manager_connector().name,
    }

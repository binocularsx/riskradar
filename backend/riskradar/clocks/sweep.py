"""Reading clocks from the database, and acting when one runs out (WP-05).

Plain English
-------------
A past-due label nobody is watching is not a control. When a bank's clock runs
out on a reported case, this records the breach once, writes it into the
hash-chained record, and — if the case is still open — escalates it to Fraud
Ops, so a named lead owns the late obligation rather than a colour on a list.

A breach is recorded **once** per clock per case (``clock_breaches`` primary
key), so running the sweep every minute, or from two processes at once, never
escalates the same miss twice. Closed cases still record a breach — a refund
owed is owed after the investigation closes — but are not reopened.

The customer's own 72-hour reporting window is shown on the case but never
escalated: it is the customer's obligation, not the bank's.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit import chain
from .calendar import Calendar, Holiday
from .engine import EVENTS, evaluate, parse_policy

log = logging.getLogger("riskradar.clocks")

CASE_EVENTS_SQL = """
    SELECT c.id, c.state, c.outcome, c.clock_policy_version,
           c.first_reported_at, c.acknowledged_at, c.counterparty_notified_at,
           c.investigation_concluded_at, c.reimbursed_at,
           (SELECT min(t.occurred_at) FROM alerts a
              JOIN transactions t ON t.id = a.transaction_id
             WHERE a.case_id = c.id) AS fraud_first_at
      FROM cases c
"""


def _fetch(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def load_calendar(conn: Any) -> Calendar:
    rows = _fetch(conn, "SELECT holiday_date, name, confirmed FROM public_holidays")
    return Calendar([Holiday(r["holiday_date"], r["name"], r["confirmed"]) for r in rows])


def active_policy(conn: Any) -> dict[str, Any] | None:
    rows = _fetch(conn, "SELECT version, definitions, source, notes, created_at FROM clock_policies WHERE is_active")
    return rows[0] if rows else None


def policies(conn: Any) -> dict[int, list]:
    """Every policy version, parsed. A case keeps the version it was reported under."""
    rows = _fetch(conn, "SELECT version, definitions FROM clock_policies")
    return {int(r["version"]): parse_policy(r["definitions"]) for r in rows}


def case_events(row: dict[str, Any]) -> dict[str, datetime | None]:
    return {e: row.get(e) for e in EVENTS}


def clocks_for_case(conn: Any, case_id: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    rows = _fetch(conn, CASE_EVENTS_SQL + " WHERE c.id = %s", (case_id,))
    if not rows or not rows[0]["clock_policy_version"]:
        return []
    row = rows[0]
    clocks = policies(conn)[int(row["clock_policy_version"])]
    return evaluate(clocks, case_events(row), outcome=row["outcome"],
                    now=now or datetime.now(timezone.utc), calendar=load_calendar(conn))


def sweep(conn: Any, *, system_user_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Record and escalate every newly breached bank clock. Returns what it did.

    Runs inside the caller's transaction; the caller commits.
    """
    now = now or datetime.now(timezone.utc)
    rows = _fetch(
        conn,
        CASE_EVENTS_SQL + """
         WHERE c.first_reported_at IS NOT NULL
           AND c.clock_policy_version IS NOT NULL
           -- A case with nothing left to deliver cannot breach again.
           AND (c.reimbursed_at IS NULL OR c.investigation_concluded_at IS NULL
                OR c.acknowledged_at IS NULL OR c.counterparty_notified_at IS NULL)
        """,
    )
    if not rows:
        return []
    by_version = policies(conn)
    calendar = load_calendar(conn)
    recorded = {
        (int(r["case_id"]), r["clock_code"])
        for r in _fetch(conn, "SELECT case_id, clock_code FROM clock_breaches WHERE case_id = ANY(%s)",
                        ([int(r["id"]) for r in rows],))
    }

    actions = []
    for row in rows:
        version = int(row["clock_policy_version"])
        states = evaluate(by_version[version], case_events(row), outcome=row["outcome"],
                          now=now, calendar=calendar)
        for s in states:
            if s["state"] != "BREACHED" or s["owner"] != "BANK" or (row["id"], s["code"]) in recorded:
                continue
            escalate = s["escalate"] and row["state"] != "CLOSED"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clock_breaches (case_id, clock_code, policy_version, due_at, escalated)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING case_id
                    """,
                    (row["id"], s["code"], version, s["due_at"], escalate),
                )
                if cur.fetchone() is None:
                    continue  # another sweep got there first
            chain.append(
                conn,
                actor_user_id=system_user_id,
                action="CLOCK_BREACHED",
                object_type="case",
                object_id=row["id"],
                payload={"clock": s["code"], "obligation": s["obligation"], "limit": s["limit"],
                         "due_at": s["due_at"].isoformat(), "policy_version": version},
            )
            if escalate and row["state"] != "ESCALATED":
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET state = 'ESCALATED', escalated_to = 'FRAUD_OPS' WHERE id = %s",
                        (row["id"],),
                    )
                chain.append(
                    conn,
                    actor_user_id=system_user_id,
                    action="CASE_ESCALATED",
                    object_type="case",
                    object_id=row["id"],
                    from_state=row["state"],
                    to_state="ESCALATED",
                    payload={"target": "FRAUD_OPS", "reason": f"clock breached: {s['code']}"},
                )
                row["state"] = "ESCALATED"
            actions.append({"case_id": row["id"], "clock": s["code"], "escalated": escalate})
    return actions


def run_forever(interval_seconds: int = 60) -> None:  # pragma: no cover - process loop
    import psycopg

    from ..config import settings
    from ..worker.scoring import system_user_id
    from .watchlist import expire_due

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    log.info("clock sweep every %ss", interval_seconds)
    while True:
        try:
            with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
                system = system_user_id(conn)
                actions = sweep(conn, system_user_id=system)
                # WP-06: flags end on their own; record it and escalate missed contact.
                expired = expire_due(conn, system_user_id=system)
                conn.commit()
            for e in expired:
                log.warning("watch-list flag %s expired%s", e["flag_id"],
                            " without customer contact" if e["contact_missed"] else "")
            for a in actions:
                log.warning("clock breached: case %s %s%s", a["case_id"], a["clock"],
                            " (escalated to Fraud Ops)" if a["escalated"] else "")
        except Exception:  # keep sweeping; one bad pass must not stop the clocks
            log.exception("clock sweep failed")
        time.sleep(interval_seconds)

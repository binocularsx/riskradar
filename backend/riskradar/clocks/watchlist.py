"""The twenty-four hour flag (WP-06, REG-NG-04).

Plain English
-------------
Since 1 May 2026 a Nigerian bank may put a customer on a temporary watch-list
for **at most 24 hours** while it contacts them to find out whether a suspicious
payment was really theirs. The flag has to end on its own; a restriction nobody
remembers to lift is a customer locked out of their money for no reason.

This file places a flag, records the customer contact the flag exists for,
lifts it early when the customer is cleared, and lets it expire. Every one of
those is written to the hash-chained record.

Three properties it keeps:

* **It ends on its own.** A flag past ``expires_at`` reads as expired in every
  query here, whether or not the sweep has run yet; the sweep then lifts it in
  the record. The 24-hour ceiling is a database constraint, not a default.
* **The contact is the point.** A flag that expires with nobody having reached
  the customer is a missed obligation: recorded, and escalated to Fraud Ops if
  its case is still open.
* **Risk Radar does not restrict anyone.** The flag is what the bank applies to
  the customer's BVN on its side (D7, D9d). It does not change a score or
  raise an alert: it is a restriction, not evidence of fraud (D73b).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..audit import chain

MAX_HOURS = 24
# Mirrors the other clocks: the last quarter of the time is "due".
DUE_FRACTION = 0.25

FLAG_COLUMNS = """
    w.id, w.subject_token, w.case_id, w.reason, w.placed_at, w.expires_at,
    w.customer_contacted_at, w.contact_outcome::text AS contact_outcome,
    w.lifted_at, w.lift_reason::text AS lift_reason,
    (SELECT display_name FROM users WHERE id = w.placed_by) AS placed_by_name,
    (SELECT display_name FROM users WHERE id = w.contacted_by) AS contacted_by_name,
    (SELECT display_name FROM users WHERE id = w.lifted_by) AS lifted_by_name
"""


class WatchlistError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _fetch(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _now(conn: Any) -> datetime:
    return _fetch(conn, "SELECT now() AS ts")[0]["ts"]


def describe(flag: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Add the flag's state and its contact obligation's state. Pure."""
    out = dict(flag)
    if flag["lifted_at"] is not None:
        state = "LIFTED" if flag["lift_reason"] == "CLEARED" else "EXPIRED"
    elif now >= flag["expires_at"]:
        state = "EXPIRED"  # ended on its own; the sweep has not recorded it yet
    else:
        state = "ACTIVE"
    out["state"] = state
    out["remaining_minutes"] = int((flag["expires_at"] - now).total_seconds() // 60) if state == "ACTIVE" else None

    if flag["customer_contacted_at"] is not None:
        contact = "CONTACTED"
    elif state == "ACTIVE":
        total = (flag["expires_at"] - flag["placed_at"]).total_seconds()
        left = (flag["expires_at"] - now).total_seconds()
        contact = "DUE" if left <= total * DUE_FRACTION else "PENDING"
    elif flag["lift_reason"] == "CLEARED":
        contact = "NOT_NEEDED"  # cleared by other evidence before a call was made
    else:
        contact = "MISSED"
    out["contact_state"] = contact
    return out


def flags_for_subject(conn: Any, subject_token: str, *, limit: int = 5) -> list[dict[str, Any]]:
    now = _now(conn)
    rows = _fetch(
        conn,
        f"SELECT {FLAG_COLUMNS} FROM subject_watchlist w WHERE w.subject_token = %s "
        "ORDER BY w.placed_at DESC LIMIT %s",
        (subject_token, limit),
    )
    return [describe(r, now) for r in rows]


def active_subjects(conn: Any, subject_tokens: list[str]) -> set[str]:
    rows = _fetch(
        conn,
        "SELECT subject_token FROM subject_watchlist "
        "WHERE subject_token = ANY(%s) AND lifted_at IS NULL AND expires_at > now()",
        (subject_tokens,),
    )
    return {r["subject_token"] for r in rows}


def _get(conn: Any, flag_id: int, *, lock: bool = False) -> dict[str, Any]:
    rows = _fetch(conn, f"SELECT {FLAG_COLUMNS} FROM subject_watchlist w WHERE w.id = %s"
                        + (" FOR UPDATE OF w" if lock else ""), (flag_id,))
    if not rows:
        raise WatchlistError(404, "flag not found")
    return rows[0]


def place(conn: Any, *, case: dict[str, Any], user_id: int, reason: str, hours: int) -> dict[str, Any]:
    if not 1 <= hours <= MAX_HOURS:
        raise WatchlistError(400, f"a temporary flag lasts 1 to {MAX_HOURS} hours")
    if case["state"] == "CLOSED":
        raise WatchlistError(400, "cannot flag a customer from a closed case")
    now = _now(conn)
    # A flag past its expiry but not yet swept must not block a new one. The
    # expiry is still the system's act, not this user's.
    system = _fetch(conn, "SELECT id FROM users WHERE is_system LIMIT 1")[0]["id"]
    _expire(conn, now=now, system_user_id=system, subject_token=case["subject_token"], by_sweep=False)
    if active_subjects(conn, [case["subject_token"]]):
        raise WatchlistError(409, "this customer already has an active flag")
    expires = now + timedelta(hours=hours)
    flag_id = _fetch(
        conn,
        """
        INSERT INTO subject_watchlist (subject_token, case_id, reason, placed_at, placed_by, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (case["subject_token"], case["id"], reason, now, user_id, expires),
    )[0]["id"]
    chain.append(
        conn, actor_user_id=user_id, action="WATCHLIST_FLAG_PLACED", object_type="case", object_id=case["id"],
        payload={"flag_id": flag_id, "hours": hours, "expires_at": expires.isoformat(), "reason": reason},
    )
    return describe(_get(conn, flag_id), now)


def record_contact(conn: Any, *, flag_id: int, user_id: int, outcome: str) -> dict[str, Any]:
    flag = _get(conn, flag_id, lock=True)
    now = _now(conn)
    if flag["customer_contacted_at"] is not None:
        raise WatchlistError(409, "customer contact is already recorded")
    if describe(flag, now)["state"] != "ACTIVE":
        raise WatchlistError(400, "the flag has ended; record the contact as a case note")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE subject_watchlist SET customer_contacted_at = %s, contacted_by = %s, contact_outcome = %s "
            "WHERE id = %s",
            (now, user_id, outcome, flag_id),
        )
    chain.append(
        conn, actor_user_id=user_id, action="WATCHLIST_CUSTOMER_CONTACTED", object_type="case",
        object_id=flag["case_id"], payload={"flag_id": flag_id, "outcome": outcome},
    )
    return describe(_get(conn, flag_id), now)


def lift(conn: Any, *, flag_id: int, user_id: int, note: str | None) -> dict[str, Any]:
    """Lift early: the customer is cleared. Expiry is the sweep's job, not this."""
    flag = _get(conn, flag_id, lock=True)
    now = _now(conn)
    if describe(flag, now)["state"] != "ACTIVE":
        raise WatchlistError(400, "the flag has already ended")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE subject_watchlist SET lifted_at = %s, lifted_by = %s, lift_reason = 'CLEARED' WHERE id = %s",
            (now, user_id, flag_id),
        )
    chain.append(
        conn, actor_user_id=user_id, action="WATCHLIST_FLAG_LIFTED", object_type="case", object_id=flag["case_id"],
        payload={"flag_id": flag_id, "reason": "CLEARED", "note": note,
                 "customer_contacted": flag["customer_contacted_at"] is not None},
    )
    return describe(_get(conn, flag_id), now)


def _expire(conn: Any, *, now: datetime, system_user_id: int, subject_token: str | None = None,
            by_sweep: bool = True) -> list[dict[str, Any]]:
    params: list[Any] = [now]
    where = "w.lifted_at IS NULL AND w.expires_at <= %s"
    if subject_token:
        where += " AND w.subject_token = %s"
        params.append(subject_token)
    rows = _fetch(
        conn,
        f"SELECT w.id, w.case_id, w.expires_at, w.customer_contacted_at, c.state AS case_state "
        f"FROM subject_watchlist w LEFT JOIN cases c ON c.id = w.case_id WHERE {where} "
        "FOR UPDATE OF w SKIP LOCKED",
        params,
    )
    actions = []
    for r in rows:
        with conn.cursor() as cur:
            # Lifted at the moment it expired, not when the sweep noticed.
            cur.execute(
                "UPDATE subject_watchlist SET lifted_at = expires_at, lifted_by = %s, lift_reason = 'EXPIRED' "
                "WHERE id = %s",
                (system_user_id, r["id"]),
            )
        missed = r["customer_contacted_at"] is None
        chain.append(
            conn, actor_user_id=system_user_id, action="WATCHLIST_FLAG_EXPIRED", object_type="case",
            object_id=r["case_id"],
            payload={"flag_id": r["id"], "expired_at": r["expires_at"].isoformat(), "customer_contacted": not missed},
        )
        escalate = missed and by_sweep and r["case_id"] and r["case_state"] not in (None, "CLOSED", "ESCALATED")
        if escalate:
            with conn.cursor() as cur:
                cur.execute("UPDATE cases SET state = 'ESCALATED', escalated_to = 'FRAUD_OPS' WHERE id = %s",
                            (r["case_id"],))
            chain.append(
                conn, actor_user_id=system_user_id, action="CASE_ESCALATED", object_type="case",
                object_id=r["case_id"], from_state=r["case_state"], to_state="ESCALATED",
                payload={"target": "FRAUD_OPS", "reason": "watch-list flag expired without customer contact"},
            )
        actions.append({"flag_id": r["id"], "case_id": r["case_id"], "contact_missed": missed,
                        "escalated": bool(escalate)})
    return actions


def expire_due(conn: Any, *, system_user_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """The sweep: lift every flag that has run out, and escalate missed contacts."""
    return _expire(conn, now=now or datetime.now(timezone.utc), system_user_id=system_user_id)

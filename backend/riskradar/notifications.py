"""Notifications between the people on the desk (D114).

Plain English
-------------
The live stream (D14) tells every screen that may see a case that something
happened to it. That suits a queue, not a person: a lead may see every case, so
"everything I may see" is every alert of the day. A notification is addressed to
one person, because something needs them or because their own work changed, and
it waits until they have seen it.

The rule for who hears about what:

* **Act now** (``urgent``) — the recipient has to do something: a case was
  assigned to them, their finding was sent back or rejected, an approval is
  waiting and they may give it, a customer reported fraud on their case, a
  regulatory clock ran out, an administrator's change waits for a second
  administrator. These are *resolved* when the work is done, by anyone, and say
  who did it — so the second lead is not left chasing an approval the first
  already gave.
* **Good to know** — their own work moved: a finding approved, a case they
  worked or escalated closed, the bank reporting what it did, a case reassigned
  away from them, a note from someone else on their case. Repeats about the same
  case within a few minutes merge into one line.
* **Never** — individual alerts (the queue streams them), deferred alerts, and
  anything the person did themselves.

Every notification is written in the caller's transaction, so it exists exactly
when the action does. It then rings the stream's doorbell (``notification``),
which the stream delivers only to the people it names.

Not an audit record. audit_log is the authority on who did what (D12c); these
are reminders that get read.
"""

from __future__ import annotations

from typing import Any, Iterable

from .events import publish
from .security.rbac import ROLE_PERMISSIONS, Permission

# Kinds. The console has a plain-English name and an icon for each.
CASE_ASSIGNED = "CASE_ASSIGNED"
CASE_REASSIGNED_AWAY = "CASE_REASSIGNED_AWAY"
APPROVAL_WAITING = "APPROVAL_WAITING"
OUTCOME_APPROVED = "OUTCOME_APPROVED"
OUTCOME_SENT_BACK = "OUTCOME_SENT_BACK"
OUTCOME_REJECTED = "OUTCOME_REJECTED"
ESCALATION_WAITING = "ESCALATION_WAITING"
CASE_HANDED_BACK = "CASE_HANDED_BACK"
CASE_CLOSED = "CASE_CLOSED"
NOTE_ADDED = "NOTE_ADDED"
CUSTOMER_REPORTED = "CUSTOMER_REPORTED"
CLOCK_BREACHED = "CLOCK_BREACHED"
BANK_ACTION_DONE = "BANK_ACTION_DONE"
BANK_ACTION_FAILED = "BANK_ACTION_FAILED"
CHANGE_WAITING = "CHANGE_WAITING"
CHANGE_DECIDED = "CHANGE_DECIDED"

# How long a "good to know" line stays open to absorb repeats about the same case.
MERGE_MINUTES = 15


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ recipients

def holders_of(conn: Any, permission: Permission | str) -> list[int]:
    """Active people whose role carries this permission. Never the SYSTEM user."""
    roles = [role for role, perms in ROLE_PERMISSIONS.items() if str(permission) in {str(p) for p in perms}]
    if not roles:
        return []
    return [r["id"] for r in _rows(
        conn, "SELECT id FROM users WHERE active AND NOT is_system AND role::text = ANY(%s)", (roles,))]


def leads(conn: Any) -> list[int]:
    return holders_of(conn, Permission.CASES_APPROVE_FRAUD)


def case_people(conn: Any, case_id: int) -> dict[str, Any]:
    """The people attached to a case, and the words a notification needs about it."""
    rows = _rows(conn, """
        SELECT c.id, c.assignee_id, c.escalated_by, c.risk_level::text AS risk_level,
               (SELECT t.display_name FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id ORDER BY a.raised_at LIMIT 1) AS customer
          FROM cases c WHERE c.id = %s
    """, (case_id,))
    return rows[0] if rows else {"id": case_id, "assignee_id": None, "escalated_by": None,
                                 "risk_level": None, "customer": None}


def name_of(conn: Any, user_id: int | None) -> str:
    if not user_id:
        return "The system"
    rows = _rows(conn, "SELECT display_name, is_system FROM users WHERE id = %s", (user_id,))
    if not rows or rows[0]["is_system"]:
        return "The system"
    return rows[0]["display_name"] or "A colleague"


def case_label(case: dict[str, Any]) -> str:
    who = f" · {case['customer']}" if case.get("customer") else ""
    return f"CASE-{case['id']}{who}"


# ---------------------------------------------------------------------- writing

def notify(
    conn: Any,
    recipients: Iterable[int | None],
    *,
    kind: str,
    title: str,
    body: str | None = None,
    case_id: int | None = None,
    ref: str | None = None,
    link: str | None = None,
    urgent: bool = False,
    actor_id: int | None = None,
) -> list[int]:
    """Tell these people. Never the person who did it; never the same person twice.

    "Good to know" news about a case merges into that person's unread line for
    the same kind and case if it is less than ``MERGE_MINUTES`` old.
    """
    people = sorted({int(r) for r in recipients if r is not None and int(r) != (actor_id or -1)})
    if not people:
        return []
    if link is None and case_id is not None:
        link = f"/cases/{case_id}"
    written: list[int] = []
    for person in people:
        merged = None
        if not urgent and case_id is not None:
            merged = _rows(conn, """
                UPDATE notifications
                   SET times = times + 1, title = %s, body = %s, actor_id = %s, updated_at = now()
                 WHERE id = (SELECT id FROM notifications
                              WHERE recipient_id = %s AND kind = %s AND case_id = %s
                                AND read_at IS NULL AND NOT urgent
                                AND updated_at > now() - make_interval(mins => %s)
                              ORDER BY updated_at DESC LIMIT 1)
                RETURNING id
            """, (title, body, actor_id, person, kind, case_id, MERGE_MINUTES))
        if merged:
            written.append(merged[0]["id"])
            continue
        row = _rows(conn, """
            INSERT INTO notifications (recipient_id, kind, urgent, case_id, ref, title, body, link, actor_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (person, kind, urgent, case_id, ref, title, body, link, actor_id))
        written.append(row[0]["id"])
    # The doorbell. The stream hands this only to the people it names.
    publish(conn, "notification", {"recipient_ids": people, "kind": kind, "urgent": urgent})
    return written


def resolve(
    conn: Any,
    *,
    kind: str | Iterable[str],
    note: str,
    ref: str | None = None,
    case_id: int | None = None,
    recipients: Iterable[int] | None = None,
) -> int:
    """Mark the open "act now" items for a piece of work as done, saying how.

    Matched by ``ref`` (one approval, one change request) or by ``case_id`` (all
    of a case's open items of that kind), optionally only for some recipients.
    """
    kinds = [kind] if isinstance(kind, str) else list(kind)
    where = ["kind = ANY(%(kinds)s)", "resolved_at IS NULL"]
    params: dict[str, Any] = {"kinds": kinds, "note": note}
    if ref is not None:
        where.append("ref = %(ref)s")
        params["ref"] = str(ref)
    if case_id is not None:
        where.append("case_id = %(case_id)s")
        params["case_id"] = case_id
    if recipients is not None:
        where.append("recipient_id = ANY(%(people)s)")
        params["people"] = list(recipients)
    if ref is None and case_id is None:
        raise ValueError("resolve needs a ref or a case")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE notifications SET resolved_at = now(), resolved_note = %(note)s "
                    f"WHERE {' AND '.join(where)}", params)
        return cur.rowcount


# Everything still asking for action on a case, cleared when the case closes.
OPEN_CASE_WORK = (CASE_ASSIGNED, APPROVAL_WAITING, OUTCOME_SENT_BACK, OUTCOME_REJECTED, ESCALATION_WAITING,
                  CASE_HANDED_BACK, CUSTOMER_REPORTED, CLOCK_BREACHED, BANK_ACTION_FAILED)

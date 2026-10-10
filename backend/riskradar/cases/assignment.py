"""Work reaches an analyst; an analyst does not go and take it (D94).

Plain English
-------------
Until now a case sat in a shared pile and whoever pressed the button first got
it. That is a queue, not an allocation: two people can open the same case,
nobody owns anything until they choose to, and "who was responsible for this
one?" has no answer before someone volunteers.

So a new case is **routed** to one analyst as it is created, in the same
transaction as the alert that opened it. The routing is recorded with its
reason, so the assignment can be explained later like every other decision.

Who gets it: the eligible analyst carrying the least open work, ties broken by
who was assigned longest ago, so a quiet analyst is filled before a busy one
and the same person is not handed everything. Eligible means an active account
with the analyst's permissions — nothing more elaborate, because shift rosters
and skills live in a bank's own rota system, and inventing them here would be
fiction (D7's rule, applied to people rather than payments).

When nobody is eligible — out of hours, everyone deactivated — the case is
**not** quietly dropped: it stays unassigned in a visible exception queue whose
owner is the Fraud Ops lead, its SLA still running, and the next routing pass
picks it up. A machine-handled case (D80) is not routed at all: it needs a
contact, not an investigation.

Reassignment stays possible, and stays a lead's power: it is an audited
exception, never the normal way work moves.
"""

from __future__ import annotations

from typing import Any

from ..audit import chain
from ..events import publish

AUTOMATIC = "automatic: least open work"
NOBODY = "nobody eligible: waiting in the unassigned queue"

# Who may be handed a case automatically. A lead can hold cases, but routing to
# them by default would spend the person who has to approve other people's work.
ELIGIBLE_ROLE = "ANALYST"

ELIGIBLE_SQL = """
    SELECT u.id, u.display_name,
           (SELECT count(*) FROM cases c
             WHERE c.assignee_id = u.id AND c.state <> 'CLOSED' AND c.outcome IS NULL) AS open_cases,
           (SELECT max(c.assigned_at) FROM cases c WHERE c.assignee_id = u.id) AS last_assigned_at
      FROM users u
     WHERE u.active AND u.role = %s
     ORDER BY open_cases, last_assigned_at NULLS FIRST, u.id
"""


def eligible(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(ELIGIBLE_SQL, (ELIGIBLE_ROLE,))
        return [dict(r) for r in cur.fetchall()]


def route(conn: Any, case_id: int, *, sys_uid: int, reason: str = AUTOMATIC) -> dict[str, Any]:
    """Assign one unassigned case. Runs in the caller's transaction.

    Returns what happened, including the exception case where nobody was
    eligible — which is information the desk needs, not an error to swallow.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, assignee_id, handling, state::text AS state FROM cases WHERE id = %s FOR UPDATE",
                    (case_id,))
        row = cur.fetchone()
    if not row:
        return {"assigned": False, "why": "case not found"}
    case = dict(row)
    if case["assignee_id"] is not None:
        return {"assigned": False, "why": "already assigned", "assignee_id": case["assignee_id"]}
    if case["handling"] == "MACHINE":
        return {"assigned": False, "why": "machine-handled: it needs a customer contact, not an investigation"}
    if case["state"] == "CLOSED":
        return {"assigned": False, "why": "closed"}

    candidates = eligible(conn)
    if not candidates:
        chain.append(conn, actor_user_id=sys_uid, action="CASE_UNASSIGNED_EXCEPTION", object_type="case",
                     object_id=case_id, payload={"reason": NOBODY})
        return {"assigned": False, "why": NOBODY, "exception": True}

    chosen = candidates[0]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cases SET assignee_id = %s, assigned_at = now(), assignment_reason = %s, "
            "version = version + 1 WHERE id = %s AND assignee_id IS NULL RETURNING version",
            (chosen["id"], reason, case_id),
        )
        updated = cur.fetchone()
    if not updated:  # somebody else routed it in the same instant
        return {"assigned": False, "why": "already assigned"}
    chain.append(
        conn, actor_user_id=sys_uid, action="CASE_ASSIGNED_AUTOMATICALLY", object_type="case", object_id=case_id,
        to_state=str(chosen["id"]),
        payload={"assignee": chosen["display_name"], "reason": reason,
                 "open_cases_before": int(chosen["open_cases"]),
                 "considered": [{"id": c["id"], "open_cases": int(c["open_cases"])} for c in candidates[:5]]},
    )
    publish(conn, "case_assigned", {"case_id": case_id, "assignee_id": chosen["id"],
                                    "assignee": chosen["display_name"], "reason": reason})
    return {"assigned": True, "assignee_id": chosen["id"], "assignee": chosen["display_name"],
            "case_version": int(dict(updated)["version"])}


def sweep(conn: Any, *, sys_uid: int, limit: int = 200) -> dict[str, Any]:
    """Route whatever is sitting unassigned. Safe to run often; it is the retry."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM cases
             WHERE assignee_id IS NULL AND state <> 'CLOSED' AND handling = 'HUMAN'
             ORDER BY risk_level DESC, opened_at
             LIMIT %s
            """,
            (limit,),
        )
        ids = [int(dict(r)["id"]) for r in cur.fetchall()]
    done = [route(conn, case_id, sys_uid=sys_uid) for case_id in ids]
    return {
        "considered": len(ids),
        "assigned": sum(1 for d in done if d["assigned"]),
        "waiting": sum(1 for d in done if not d["assigned"] and d.get("exception")),
    }

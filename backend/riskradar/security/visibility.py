"""Which cases a person may see, enforced on the server (D94).

Plain English
-------------
Hiding a button is not access control. Until now any account that could read
cases could read *every* case, and the console simply chose what to show. The
implementation plan's §3.1 is blunt about it: the server must return only the
data the caller is allowed to receive, on every read — lists, one case, search,
exports and the live stream alike.

Who sees what:

* **Analyst** — their own cases, and cases they escalated, so they can follow
  what happened to their own work.
* **Fraud Ops lead** — everything: they carry the desk, approve other people's
  findings and own the unassigned exception queue.
* **InfoSec** — escalations sent to InfoSec, and anything they are holding.
  Not the commercial fraud desk's other work.
* **Administrator** — nothing. They hold no case permission at all (D12b), so
  they never reach these checks.

One SQL predicate, used by every list and every single-case read, so a new
endpoint cannot quietly miss it. It is written against the alias ``c`` because
every case query in this system already calls the table ``c``.
"""

from __future__ import annotations

from typing import Any

ALL_CASES = "TRUE"


def predicate(user: dict[str, Any], alias: str = "c") -> tuple[str, dict[str, Any]]:
    """The SQL a case query must AND in, and the parameters it needs."""
    role = user["role"]
    if role == "FRAUD_OPS_LEAD":
        return ALL_CASES, {}
    if role == "INFOSEC_ANALYST":
        return (f"({alias}.assignee_id = %(vis_uid)s OR "
                f"({alias}.state = 'ESCALATED' AND {alias}.escalated_to = 'INFOSEC'))",
                {"vis_uid": user["id"]})
    if role == "ANALYST":
        return (f"({alias}.assignee_id = %(vis_uid)s OR {alias}.escalated_by = %(vis_uid)s)",
                {"vis_uid": user["id"]})
    # Any other role, including ADMIN and SYSTEM: no case content.
    return "FALSE", {}


def sees_everything(user: dict[str, Any]) -> bool:
    return predicate(user)[0] == ALL_CASES


def can_read(conn: Any, user: dict[str, Any], case_id: int) -> bool:
    sql, params = predicate(user)
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM cases c WHERE c.id = %(case_id)s AND {sql}", {**params, "case_id": case_id})
        return cur.fetchone() is not None


def visible_case_ids(conn: Any, user: dict[str, Any], case_ids: list[int]) -> set[int]:
    """Which of these cases this person may see. Used to filter live events."""
    if not case_ids:
        return set()
    sql, params = predicate(user)
    with conn.cursor() as cur:
        cur.execute(f"SELECT c.id FROM cases c WHERE c.id = ANY(%(ids)s) AND {sql}",
                    {**params, "ids": list(case_ids)})
        return {int(dict(r)["id"]) for r in cur.fetchall()}


def subject_predicate(user: dict[str, Any], column: str) -> tuple[str, dict[str, Any]]:
    """The same rule for a table that carries a subject token rather than a case.

    Search runs over transactions, not cases, so "may this person see this
    payment?" becomes "do they hold a case for this customer?".
    """
    if sees_everything(user):
        return ALL_CASES, {}
    sql, params = predicate(user)
    return (f"EXISTS (SELECT 1 FROM cases c WHERE c.subject_token = {column} AND {sql})", params)

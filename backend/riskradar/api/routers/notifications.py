"""The bell: a person's own notifications (D114).

Everyone who signs in has one, administrators included, so these endpoints ask
only that the caller is signed in. Each person reads and marks only their own;
another person's notification is a 404, not a 403, for the same reason a case
outside your scope is (D94).

A notification can point at a case the reader may no longer open — it was
reassigned away from them, say. The line is still theirs to read, but the link
is offered only while the case is visible to them, checked on every read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...security import visibility
from ..deps import current_user, get_conn

router = APIRouter(prefix="/v1", tags=["notifications"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@router.get("/notifications")
def list_notifications(
    limit: int = Query(40, ge=1, le=200),
    user: dict = Depends(current_user),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    items = _rows(conn, """
        SELECT n.id, n.kind, n.urgent, n.case_id, n.title, n.body, n.link, n.times,
               n.created_at, n.updated_at, n.read_at, n.resolved_at, n.resolved_note,
               a.display_name AS actor
          FROM notifications n LEFT JOIN users a ON a.id = n.actor_id AND NOT a.is_system
         WHERE n.recipient_id = %s
         ORDER BY (n.urgent AND n.resolved_at IS NULL) DESC, n.updated_at DESC
         LIMIT %s
    """, (user["id"], limit))
    case_ids = sorted({i["case_id"] for i in items if i["case_id"]})
    may_open = visibility.visible_case_ids(conn, user, case_ids) if case_ids else set()
    for i in items:
        if i["case_id"] and i["case_id"] not in may_open:
            i["link"] = None
    counts = _rows(conn, """
        SELECT count(*) FILTER (WHERE read_at IS NULL) AS unread,
               count(*) FILTER (WHERE urgent AND resolved_at IS NULL) AS to_do
          FROM notifications WHERE recipient_id = %s
    """, (user["id"],))[0]
    return {"items": items, "unread": int(counts["unread"]), "to_do": int(counts["to_do"])}


@router.post("/notifications/{notification_id}/read")
def mark_read(
    notification_id: int,
    user: dict = Depends(current_user),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("UPDATE notifications SET read_at = coalesce(read_at, now()) "
                    "WHERE id = %s AND recipient_id = %s", (notification_id, user["id"]))
        if cur.rowcount == 0:
            raise HTTPException(404, "notification not found")
    return {"id": notification_id, "read": True}


@router.post("/notifications/read-all")
def mark_all_read(
    user: dict = Depends(current_user),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Reading is not doing: an urgent item stays on the to-do count until the work is done."""
    with conn.cursor() as cur:
        cur.execute("UPDATE notifications SET read_at = now() WHERE recipient_id = %s AND read_at IS NULL",
                    (user["id"],))
        return {"marked": cur.rowcount}

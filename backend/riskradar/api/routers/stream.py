"""Server-Sent Events (FR-030, FR-031, D14).

Plain English
-------------
Pushes new alerts to the dashboard the moment they happen, without the browser
having to keep asking.

It uses a web standard called Server-Sent Events, which has one property that
mattered here: if the connection drops, the browser reconnects on its own and
tells the server the last thing it received, so nothing is missed. Chat-style
WebSockets do not do that, and bank networks tend to break them anyway.

Only alerts are streamed. Transactions are not — there are thousands an hour,
and a live wall of every payment is something nobody can actually read. Those
are summarised on the metrics page instead.

SSE rather than WebSockets. Traffic is overwhelmingly server-to-client — analyst
actions are ordinary request/response REST — bank proxies routinely break the
WebSocket upgrade, and WebSocket reconnection has no replay. SSE gives gap replay
*at the protocol level*: the browser reconnects on its own and sends
``Last-Event-ID``, and we resume from there.

D14a: ``LISTEN``/``NOTIFY`` carries the event id only. The 8000-byte payload cap
and its non-durability mean the table is the record and the notification is just
a doorbell — a client disconnected at the instant of a ``NOTIFY`` would otherwise
never learn it happened. Here, it reconnects and replays the gap from
``stream_events``.

D14b: **alerts stream, transactions do not.** Alert volume is low by
construction — 120 a day against the alert budget — while a live feed of every
transaction would be a scrolling wall nobody can read.

**Why this uses a synchronous connection on a worker thread.** psycopg's async
mode refuses to run on asyncio's ``ProactorEventLoop``, which is the default on
Windows — and the demo runs on Windows (D26: local only, one machine). The two
ways out were to force ``WindowsSelectorEventLoopPolicy`` process-wide, or to
keep the driver synchronous and hand the blocking wait to a thread. The second
is portable, changes nothing about how the rest of the server runs, and keeps
``LISTEN``/``NOTIFY`` exactly as D14a specifies. A platform quirk should not get
to reshape the whole event loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...config import settings
from ...security.rbac import Permission
from ..deps import requires_streaming

router = APIRouter(prefix="/v1", tags=["stream"])

CHANNEL = "riskradar_events"
HEARTBEAT_SECONDS = 15.0

# Replay is bounded. A client that has been away for a week does not want six
# thousand alerts replayed into its face; it wants to reload the queue.
MAX_REPLAY = 500


def _format(event_id: int, event_type: str, payload: dict[str, Any]) -> str:
    """One SSE frame.

    The ``id:`` line is what makes ``Last-Event-ID`` work — the browser records
    it and returns it on reconnect without any client code.
    """
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, default=str)}\n\n"
    )


# --- blocking helpers, always called through asyncio.to_thread ---------------


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        settings().app_dsn, autocommit=True, row_factory=psycopg.rows.dict_row
    )


def _fetch_since(conn: psycopg.Connection, since_id: int, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_type, payload
              FROM stream_events
             WHERE id > %s
             ORDER BY id
             LIMIT %s
            """,
            (since_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def _latest_id(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(id), 0) AS id FROM stream_events")
        return int(cur.fetchone()["id"])


def _wait_for_notify(conn: psycopg.Connection, timeout: float) -> bool:
    """Block until a notification arrives or the timeout elapses.

    One notification is enough to wake us; we then drain by id rather than
    trusting the notification's contents, because the table is the record.
    """
    try:
        for _ in conn.notifies(timeout=timeout, stop_after=1):
            return True
    except Exception:  # noqa: BLE001 - the connection went away underneath us
        raise
    return False


async def _event_source(request: Request, last_event_id: int) -> AsyncIterator[str]:
    conn = await asyncio.to_thread(_connect)
    try:
        cursor_id = last_event_id

        # FR-031: replay the gap first, before subscribing, so nothing that
        # arrived during the disconnect is lost between the catch-up query and
        # the LISTEN taking effect.
        if cursor_id:
            for row in await asyncio.to_thread(_fetch_since, conn, cursor_id, MAX_REPLAY):
                cursor_id = row["id"]
                yield _format(row["id"], row["event_type"], row["payload"])
        else:
            cursor_id = await asyncio.to_thread(_latest_id, conn)

        await asyncio.to_thread(conn.execute, f"LISTEN {CHANNEL}")
        yield ": connected\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                woken = await asyncio.to_thread(_wait_for_notify, conn, HEARTBEAT_SECONDS)
            except Exception:  # noqa: BLE001
                break

            rows = await asyncio.to_thread(_fetch_since, conn, cursor_id, MAX_REPLAY)
            for row in rows:
                cursor_id = row["id"]
                yield _format(row["id"], row["event_type"], row["payload"])

            if not woken and not rows:
                # The timeout fired with nothing to send. A comment frame keeps
                # proxies from reaping an idle connection.
                yield ": heartbeat\n\n"
    finally:
        await asyncio.to_thread(conn.close)


@router.get("/stream")
async def stream(
    request: Request,
    user: dict = Depends(requires_streaming(Permission.CASES_READ)),
) -> StreamingResponse:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        last_event_id = int(raw) if raw else 0
    except ValueError:
        last_event_id = 0

    return StreamingResponse(
        _event_source(request, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx and most bank proxies buffer by default, which turns a live
            # stream into a batch delivered at disconnect.
            "X-Accel-Buffering": "no",
        },
    )

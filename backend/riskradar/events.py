"""The live stream's doorbell (D14a), shared by everything that has news for the desk.

A durable ``stream_events`` row is the record; ``NOTIFY`` carries only its id.
The SSE endpoint reads the row, and ``Last-Event-ID`` replays whatever a
disconnected browser missed.

Event types (the console listens by name):

* ``alert`` — a new alert on a case; reload the worklist.
* ``alarm`` — something is wrong with the system; show a banner.
* ``alert_deferred`` — the budget held an alert back (D86).
* ``case_reported`` — a customer reported fraud on a case (D90); tell the
  assignee and the leads, it has moved to the top of the queue.
* ``clock_breached`` — a CBN clock ran out on a reported case (D71, D90).
"""

from __future__ import annotations

import json
from typing import Any


def publish(conn: Any, event_type: str, payload: dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stream_events (event_type, payload) VALUES (%s, %s) RETURNING id",
            (event_type, json.dumps(payload, default=str)),
        )
        row = cur.fetchone()
        event_id = int(row["id"] if isinstance(row, dict) else row[0])
        cur.execute("SELECT pg_notify('riskradar_events', %s)", (str(event_id),))
    return event_id

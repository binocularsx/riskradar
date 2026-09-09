"""Alert-to-case correlation (D13a).

An account takeover producing reconnaissance, a device change and six rapid
transfers generates eight alerts. Without correlation that is eight cases for one
incident, investigated eight times, and every queue metric becomes fiction.

The subject is the **customer** (``subject_token``), not the account (D19). An
attacker draining a victim's current, savings and domiciliary accounts is one
incident, and correlating on the account would reproduce the same fragmentation
one level up.

This is also what makes incident-level recall the honest headline metric (D24a):
one alert is enough to open the case, and the case then shows the analyst the
subject's whole timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DEFAULT_WINDOW_HOURS = 24
OPEN_STATES = ("OPEN", "UNDER_REVIEW", "ESCALATED")

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def _worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


def correlation_window_hours(conn: Any) -> int:
    """Configuration, not a constant (D13a: "tunable configuration")."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config WHERE key = 'correlation_window_hours'")
        row = cur.fetchone()
    if not row:
        return DEFAULT_WINDOW_HOURS
    value = row["value"] if isinstance(row, dict) else row[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_HOURS


def attach(
    conn: Any,
    *,
    subject_token: str,
    risk_level: str,
    at: datetime,
) -> tuple[int, bool]:
    """Find or create the case this alert belongs to.

    Returns ``(case_id, created)``. Must run inside the caller's transaction —
    the advisory lock is transaction-scoped, and correlating in a separate
    transaction from the alert insert would let two workers each decide they were
    first.
    """
    hours = correlation_window_hours(conn)

    with conn.cursor() as cur:
        # Serialise correlation per subject. Two workers scoring two transactions
        # for the same customer in the same instant would otherwise both find no
        # open case and both create one — the exact duplicate this module exists
        # to prevent.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (subject_token,))

        cur.execute(
            """
            SELECT id, risk_level
              FROM cases
             WHERE subject_token = %s
               AND state = ANY(%s)
               AND correlation_expires_at > %s
             ORDER BY last_alert_at DESC
             LIMIT 1
            """,
            (subject_token, list(OPEN_STATES), at),
        )
        row = cur.fetchone()

        if row:
            case = dict(row) if not isinstance(row, dict) else row
            cur.execute(
                """
                UPDATE cases
                   SET last_alert_at = GREATEST(last_alert_at, %s),
                       alert_count   = alert_count + 1,
                       risk_level    = %s
                 WHERE id = %s
                """,
                (at, _worse(case["risk_level"], risk_level), case["id"]),
            )
            return int(case["id"]), False

        cur.execute(
            """
            INSERT INTO cases
                (subject_token, state, risk_level, opened_at, last_alert_at,
                 correlation_expires_at, alert_count)
            VALUES (%s, 'OPEN', %s, %s, %s, %s, 1)
            RETURNING id
            """,
            (subject_token, risk_level, at, at, at + timedelta(hours=hours)),
        )
        new_row = cur.fetchone()
        case_id = new_row["id"] if isinstance(new_row, dict) else new_row[0]
        return int(case_id), True

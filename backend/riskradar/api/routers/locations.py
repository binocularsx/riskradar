"""Where things happen: the bank-wide location analysis and a case's geo survey.

``transactions.ip_region`` is the Nigerian state (ISO 3166-2) that the payment's
**session** appeared to come from. It is not where the customer lives, and every
response here says so.

What the data shows, measured before building this (7 days, Oct 2026): customers
switching state between payments within minutes is normal — about 30,000
switches a week, because mobile IP addresses resolve loosely — so an
"impossible travel" alarm would only add noise. A state the customer has
**not used in the previous 30 days** is the useful signal: about 3.5% of those
payments raise an alert, against about 0.1% of the rest. That is the same test
the detector already uses (``region_is_new_to_subject``), so this counts it from
the stored decision rather than recomputing it a different way.

Two audiences (D113):

* **Location analysis** (``/v1/locations/...``) is the whole bank by state and
  over time. It is the administrator's: aggregates only, never a case or a
  customer, so it fits a role that holds no case permission (D12b).
* **Geo survey** (``/v1/cases/{id}/geo``) is one case's activity on the map and
  the customers and cases tied to it. It is case content, so it is scoped by
  the case visibility rule like every other case read (D94).

What the bank cannot see, said rather than drawn: payment destinations are
accounts at other banks (none of the 155,000 destinations in the last 30 days
is an account here), so where money goes *after* it leaves is not in the data.
The survey shows how far it can see — who else paid the same account and from
where — and stops there.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...security import visibility
from ...security.rbac import Permission
from ..deps import get_conn, requires

router = APIRouter(prefix="/v1", tags=["locations"])

NOTE = ("Location is the state the payment's session appeared to come from (its IP address), "
        "not the customer's registered address.")

CHANNELS = {"MOBILE_APP", "WEB", "USSD", "POS", "ATM", "AGENT", "BRANCH"}
NEW_REGION = "(d.features->>'region_is_new_to_subject') = '1.0'"
CACHE_SECONDS = 120
MAX_HOURS = 2160  # 90 days

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _cached(key: tuple, build):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = build()
    value["computed_at"] = datetime.now(timezone.utc).isoformat()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _channel(channel: str | None) -> str | None:
    if channel is None or channel == "":
        return None
    if channel not in CHANNELS:
        raise HTTPException(422, "Unknown channel")
    return channel


def _filters(hours: int, channel: str | None, region: str | None = None) -> tuple[str, dict[str, Any]]:
    where = ["t.occurred_at > now() - make_interval(hours => %(hours)s)", "t.ip_region IS NOT NULL",
             "t.direction <> 'INBOUND'"]
    params: dict[str, Any] = {"hours": hours}
    if channel:
        where.append("t.channel = %(channel)s")
        params["channel"] = channel
    if region:
        where.append("t.ip_region = %(region)s")
        params["region"] = region
    return " AND ".join(where), params


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 5) if whole else 0.0


def _ints(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    for r in rows:
        for k in keys:
            r[k] = int(r[k] or 0)
    return rows


# --------------------------------------------------------------------------
# Location analysis: the whole bank, by state and over time (administrator)
# --------------------------------------------------------------------------

@router.get("/locations/overview")
def overview(
    hours: int = Query(168, ge=1, le=MAX_HOURS),
    channel: str | None = None,
    user: dict = Depends(requires(Permission.ANALYSIS_GEOGRAPHY)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Every state's volume, alerts and new-to-customer activity, with lift."""
    channel = _channel(channel)

    def build() -> dict[str, Any]:
        where, params = _filters(hours, channel)
        rows = _ints(_rows(
            conn,
            f"""
            SELECT t.ip_region,
                   count(*)                                                     AS transactions,
                   count(DISTINCT t.subject_token)                              AS customers,
                   count(*) FILTER (WHERE t.auth_result <> 'APPROVED')          AS declined,
                   coalesce(sum(t.amount_minor) FILTER (WHERE t.auth_result = 'APPROVED'), 0)
                                                                                AS approved_value_minor,
                   count(a.id)                                                  AS alerts,
                   count(a.id) FILTER (WHERE a.risk_level IN ('HIGH','CRITICAL')) AS serious_alerts,
                   coalesce(sum(t.amount_minor) FILTER (WHERE a.id IS NOT NULL), 0) AS flagged_value_minor,
                   count(*) FILTER (WHERE {NEW_REGION})                         AS new_region,
                   count(a.id) FILTER (WHERE {NEW_REGION})                      AS new_region_alerts
              FROM transactions t
              LEFT JOIN decisions d ON d.transaction_id = t.id
              LEFT JOIN alerts a    ON a.transaction_id = t.id
             WHERE {where}
             GROUP BY 1
            """,
            params,
        ), "transactions", "customers", "declined", "alerts", "serious_alerts", "new_region",
            "new_region_alerts", "approved_value_minor", "flagged_value_minor")
        tx = sum(r["transactions"] for r in rows)
        al = sum(r["alerts"] for r in rows)
        nr = sum(r["new_region"] for r in rows)
        nra = sum(r["new_region_alerts"] for r in rows)
        national_rate = _rate(al, tx)
        for r in rows:
            r["alert_rate"] = _rate(r["alerts"], r["transactions"])
            # How many times the national alert rate. 1.0 is ordinary; the
            # number a reader actually compares states on.
            r["lift"] = round(r["alert_rate"] / national_rate, 2) if national_rate else None
            r["new_region_share_of_alerts"] = _rate(r["new_region_alerts"], r["alerts"])
        rows.sort(key=lambda r: r["transactions"], reverse=True)

        last = _rows(conn, "SELECT max(occurred_at) AS at FROM transactions")[0]["at"]
        return {
            "window_hours": hours,
            "channel": channel,
            "states": rows,
            "national": {
                "transactions": tx,
                "alerts": al,
                "alert_rate": national_rate,
                "new_region": nr,
                "new_region_alerts": nra,
                "new_region_alert_rate": _rate(nra, nr),
                "usual_region_alert_rate": _rate(al - nra, tx - nr),
            },
            "last_activity_at": last,
            "note": NOTE,
        }

    return _cached(("overview", hours, channel), build)


@router.get("/locations/moves")
def moves(
    hours: int = Query(168, ge=1, le=MAX_HOURS),
    channel: str | None = None,
    user: dict = Depends(requires(Permission.ANALYSIS_GEOGRAPHY)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Customers paying from a state new to them: where they usually pay from.

    "Usually" is the state they used most in the 30 days before they first
    appeared somewhere new in this window, skipping the final day, which is the
    same look-back the detector uses. Customers with no earlier located payment
    are counted as having no usual state rather than guessed at.
    """
    channel = _channel(channel)

    def build() -> dict[str, Any]:
        where, params = _filters(hours, channel)
        rows = _ints(_rows(
            conn,
            f"""
            WITH moved AS MATERIALIZED (
                SELECT t.subject_token, t.ip_region, t.occurred_at, a.id AS alert_id
                  FROM transactions t
                  JOIN decisions d   ON d.transaction_id = t.id
                  LEFT JOIN alerts a ON a.transaction_id = t.id
                 WHERE {where} AND {NEW_REGION}
            ),
            movers AS (
                SELECT subject_token, min(occurred_at) AS first_at FROM moved GROUP BY 1
            ),
            usual AS (
                SELECT m.subject_token, u.ip_region
                  FROM movers m
                  CROSS JOIN LATERAL (
                      SELECT p.ip_region
                        FROM transactions p
                       WHERE p.subject_token = m.subject_token
                         AND p.occurred_at > m.first_at - interval '30 days'
                         AND p.occurred_at < m.first_at - interval '1 day'
                         AND p.ip_region IS NOT NULL
                       GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1
                  ) u
            )
            SELECT u.ip_region AS from_region, m.ip_region AS to_region,
                   count(*) AS transactions, count(m.alert_id) AS alerts,
                   count(DISTINCT m.subject_token) AS customers
              FROM moved m JOIN usual u USING (subject_token)
             WHERE u.ip_region <> m.ip_region
             GROUP BY 1, 2
             ORDER BY alerts DESC, transactions DESC
            """,
            params,
        ), "transactions", "alerts", "customers")
        for r in rows:
            r["alert_rate"] = _rate(r["alerts"], r["transactions"])
        return {"window_hours": hours, "channel": channel, "moves": rows, "note": NOTE}

    return _cached(("moves", hours, channel), build)


@router.get("/locations/{region}")
def region_detail(
    region: str,
    hours: int = Query(168, ge=1, le=MAX_HOURS),
    channel: str | None = None,
    user: dict = Depends(requires(Permission.ANALYSIS_GEOGRAPHY)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """One state over time: by day against the bank, by hour, by channel, and why it alerted.

    No cases and no customers: this is the administrator's view (D12b).
    """
    channel = _channel(channel)
    if not region.startswith("NG-") or len(region) > 8:
        raise HTTPException(404, "Unknown state")
    where, params = _filters(hours, channel, region)
    bank_where, bank_params = _filters(hours, channel)

    def build() -> dict[str, Any]:
        by_day = _ints(_rows(
            conn,
            f"""
            SELECT (t.occurred_at AT TIME ZONE 'Africa/Lagos')::date AS day,
                   count(*) FILTER (WHERE t.ip_region = %(region)s)       AS transactions,
                   count(a.id) FILTER (WHERE t.ip_region = %(region)s)    AS alerts,
                   count(*)                                              AS bank_transactions,
                   count(a.id)                                           AS bank_alerts
              FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
             WHERE {bank_where}
             GROUP BY 1 ORDER BY 1
            """,
            {**bank_params, "region": region},
        ), "transactions", "alerts", "bank_transactions", "bank_alerts")
        for r in by_day:
            r["day"] = r["day"].isoformat()
            r["alert_rate"] = _rate(r["alerts"], r["transactions"])
            r["bank_alert_rate"] = _rate(r["bank_alerts"], r["bank_transactions"])
        by_hour = _rows(
            conn,
            f"""
            SELECT extract(hour FROM t.occurred_at AT TIME ZONE 'Africa/Lagos')::int AS hour,
                   count(*) AS transactions, count(a.id) AS alerts
              FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
             WHERE {where}
             GROUP BY 1 ORDER BY 1
            """,
            params,
        )
        hours_full = {h: {"hour": h, "transactions": 0, "alerts": 0} for h in range(24)}
        for r in by_hour:
            hours_full[r["hour"]] = {"hour": r["hour"], "transactions": int(r["transactions"]),
                                     "alerts": int(r["alerts"])}
        by_channel = _ints(_rows(
            conn,
            f"""
            SELECT t.channel::text AS channel, count(*) AS transactions, count(a.id) AS alerts
              FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
             WHERE {where}
             GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ), "transactions", "alerts")
        reasons = _ints(_rows(
            conn,
            f"""
            SELECT s->>'code' AS code, s->>'power' AS power, count(*) AS n
              FROM transactions t
              JOIN alerts a    ON a.transaction_id = t.id
              JOIN decisions d ON d.id = a.decision_id,
                   jsonb_array_elements(d.signals) s
             WHERE {where}
             GROUP BY 1, 2 ORDER BY n DESC
            """,
            params,
        ), "n")
        return {"by_day": by_day, "by_hour": list(hours_full.values()), "by_channel": by_channel,
                "reasons": reasons}

    detail = dict(_cached(("region", region, hours, channel), build))
    detail.update(region=region, window_hours=hours, channel=channel, note=NOTE)
    return detail


# --------------------------------------------------------------------------
# Geo survey: one case on the map (analysts and leads, scoped like the case)
# --------------------------------------------------------------------------

MAX_DESTINATIONS = 10
MAX_LINKED = 40
MAX_NEARBY = 30


@router.get("/cases/{case_id}/geo")
def case_geo(
    case_id: int,
    days: int = Query(30, ge=7, le=90),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Where this case's activity happened, and who and what it is tied to.

    The window is anchored on the case, not on today: from ``days`` before the
    case opened to a week after its last alert (or now, if sooner), so an old
    case still shows the activity around it.
    """
    scope_sql, scope_params = visibility.predicate(user)
    found = _rows(conn, f"""
        SELECT c.id, c.subject_token, c.opened_at, c.last_alert_at, c.state::text AS state,
               c.outcome::text AS outcome, c.risk_level::text AS risk_level
          FROM cases c WHERE c.id = %(case_id)s AND {scope_sql}
    """, {**scope_params, "case_id": case_id})
    if not found:
        raise HTTPException(404, "case not found")
    case = found[0]
    subject = case["subject_token"]
    now = datetime.now(timezone.utc)
    start = case["opened_at"] - timedelta(days=days)
    end = min(now, case["last_alert_at"] + timedelta(days=7))
    p = {"subject": subject, "start": start, "end": end, "case_id": case_id}
    window = "t.occurred_at >= %(start)s AND t.occurred_at <= %(end)s"

    # 1. The alerts on this case, in order: the events the survey is about.
    events = _rows(conn, f"""
        SELECT t.id, t.transaction_ref, t.occurred_at, t.amount_minor, t.channel::text AS channel,
               t.ip_region, t.direction, t.auth_result::text AS auth_result, t.remitter_bank_code,
               t.beneficiary_token, t.device_token, a.risk_level::text AS risk_level, coalesce({NEW_REGION}, false) AS new_region,
               coalesce((SELECT array_agg(s->>'code') FROM jsonb_array_elements(d.signals) s), '{{}}') AS codes
          FROM alerts a
          JOIN transactions t ON t.id = a.transaction_id
          JOIN decisions d    ON d.id = a.decision_id
         WHERE a.case_id = %(case_id)s
         ORDER BY t.occurred_at
    """, p)

    # 2. Where the customer usually pays from: the detector's own look-back,
    #    30 days before the case opened, skipping the final day.
    usual = _rows(conn, """
        SELECT ip_region, count(*) AS n FROM transactions
         WHERE subject_token = %(subject)s AND ip_region IS NOT NULL
           AND occurred_at > %(opened)s - interval '31 days' AND occurred_at < %(opened)s - interval '1 day'
         GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3
    """, {"subject": subject, "opened": case["opened_at"]})

    # 3. The customer's footprint in the window, by state.
    footprint = _ints(_rows(conn, f"""
        SELECT t.ip_region,
               count(*) AS payments,
               count(a.id) AS alerts,
               coalesce(sum(t.amount_minor) FILTER (WHERE t.direction = 'OUTBOUND' AND t.auth_result = 'APPROVED'), 0)
                   AS value_out_minor,
               min(t.occurred_at) AS first_at, max(t.occurred_at) AS last_at
          FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
         WHERE t.subject_token = %(subject)s AND {window}
         GROUP BY 1 ORDER BY 2 DESC
    """, p), "payments", "alerts", "value_out_minor")

    # 4. Money in, and how much of what left followed money arriving within a day.
    money_in = _ints(_rows(conn, f"""
        SELECT t.remitter_bank_code AS bank, count(*) AS credits, sum(t.amount_minor) AS value_minor
          FROM transactions t
         WHERE t.subject_token = %(subject)s AND t.direction = 'INBOUND' AND {window}
         GROUP BY 1 ORDER BY 3 DESC
    """, p), "credits", "value_minor")
    flow = _rows(conn, f"""
        SELECT coalesce(sum(t.amount_minor), 0) AS out_minor,
               coalesce(sum(t.amount_minor) FILTER (WHERE EXISTS (
                   SELECT 1 FROM transactions i
                    WHERE i.subject_token = %(subject)s AND i.direction = 'INBOUND'
                      AND i.occurred_at BETWEEN t.occurred_at - interval '24 hours' AND t.occurred_at)), 0)
                   AS passed_on_minor
          FROM transactions t
         WHERE t.subject_token = %(subject)s AND t.direction = 'OUTBOUND' AND t.auth_result = 'APPROVED'
           AND {window}
    """, p)[0]

    # 5. Where the money went: this customer's destinations, and who else paid them from where.
    destinations = _ints(_rows(conn, f"""
        WITH mine AS (
            SELECT t.beneficiary_token AS token, count(*) AS payments, sum(t.amount_minor) AS value_minor,
                   count(a.id) AS alerts,
                   coalesce(array_agg(DISTINCT t.ip_region) FILTER (WHERE t.ip_region IS NOT NULL), '{{}}') AS from_states,
                   min(t.occurred_at) AS first_paid, max(t.occurred_at) AS last_paid
              FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
             WHERE t.subject_token = %(subject)s AND t.direction = 'OUTBOUND'
               AND t.beneficiary_token IS NOT NULL AND {window}
             GROUP BY 1
        )
        SELECT m.*,
               (SELECT count(DISTINCT t.subject_token) FROM transactions t
                 WHERE t.beneficiary_token = m.token AND t.subject_token <> %(subject)s AND {window}) AS other_customers,
               (SELECT count(DISTINCT c.id) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                  JOIN cases c ON c.id = a.case_id
                 WHERE t.beneficiary_token = m.token AND c.outcome = 'CONFIRMED_FRAUD') AS confirmed_fraud_cases,
               (SELECT coalesce(array_agg(DISTINCT kind::text), '{{}}') FROM beneficiary_lists l
                 WHERE l.token = m.token AND l.kind IN ('KNOWN_MULE', 'SANCTIONED')) AS lists
          FROM mine m
         ORDER BY other_customers DESC, value_minor DESC
         LIMIT {MAX_DESTINATIONS}
    """, p), "payments", "value_minor", "alerts", "other_customers", "confirmed_fraud_cases")
    tokens = [d["token"] for d in destinations]

    payers = _ints(_rows(conn, f"""
        SELECT t.beneficiary_token AS token, t.ip_region, count(DISTINCT t.subject_token) AS customers,
               count(*) AS payments, sum(t.amount_minor) AS value_minor, count(a.id) AS alerts
          FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
         WHERE t.beneficiary_token = ANY(%(tokens)s) AND t.subject_token <> %(subject)s
           AND t.ip_region IS NOT NULL AND {window}
         GROUP BY 1, 2
    """, {**p, "tokens": tokens}), "customers", "payments", "value_minor", "alerts") if tokens else []
    by_token: dict[str, list] = {}
    for r in payers:
        by_token.setdefault(r.pop("token"), []).append(r)
    for i, d in enumerate(destinations):
        d["label"] = f"Account {chr(65 + i)}"
        d["ending"] = d["token"][-4:]
        d["payers_by_state"] = sorted(by_token.get(d["token"], []), key=lambda r: -r["customers"])

    # 6. The linked customers: others who paid the same accounts or used the same devices.
    device_rows = _ints(_rows(conn, f"""
        SELECT t.device_token AS token, count(*) AS uses, min(t.occurred_at) AS first_used,
               max(t.occurred_at) AS last_used,
               (SELECT count(DISTINCT o.subject_token) FROM transactions o
                 WHERE o.device_token = t.device_token AND o.subject_token <> %(subject)s
                   AND o.occurred_at >= %(start)s AND o.occurred_at <= %(end)s) AS other_customers
          FROM transactions t
         WHERE t.subject_token = %(subject)s AND t.device_token IS NOT NULL AND {window}
         GROUP BY 1 ORDER BY count(*) DESC LIMIT 10
    """, p), "uses", "other_customers")
    devices = [r["token"] for r in device_rows]
    linked = _rows(conn, f"""
        WITH shared AS (
            SELECT t.subject_token, 'destination' AS via, t.beneficiary_token AS token, t.ip_region, t.amount_minor
              FROM transactions t
             WHERE t.beneficiary_token = ANY(%(tokens)s) AND t.subject_token <> %(subject)s AND {window}
            UNION ALL
            SELECT t.subject_token, 'device', t.device_token, t.ip_region, 0
              FROM transactions t
             WHERE t.device_token = ANY(%(devices)s) AND t.subject_token <> %(subject)s AND {window}
        )
        SELECT subject_token,
               array_agg(DISTINCT via) AS via,
               array_agg(DISTINCT token) FILTER (WHERE via = 'destination') AS tokens,
               count(*) FILTER (WHERE via = 'destination') AS payments,
               coalesce(sum(amount_minor), 0) AS value_minor,
               mode() WITHIN GROUP (ORDER BY ip_region) AS home_region,
               coalesce(array_agg(DISTINCT ip_region) FILTER (WHERE ip_region IS NOT NULL), '{{}}') AS states
          FROM shared
         GROUP BY 1
         ORDER BY count(*) FILTER (WHERE via = 'destination') DESC, count(*) DESC
         LIMIT {MAX_LINKED}
    """, {**p, "tokens": tokens, "devices": devices}) if (tokens or devices) else []
    subjects = [r["subject_token"] for r in linked]
    names = {r["subject_token"]: r["name"] for r in _rows(conn, f"""
        SELECT t.subject_token, max(t.display_name) AS name FROM transactions t
         WHERE t.subject_token = ANY(%(subjects)s) AND {window} GROUP BY 1
    """, {**p, "subjects": subjects})} if subjects else {}
    latest = {r["subject_token"]: r for r in _rows(conn, """
        SELECT DISTINCT ON (subject_token) subject_token, id, state::text AS state, outcome::text AS outcome
          FROM cases WHERE subject_token = ANY(%(subjects)s)
         ORDER BY subject_token, opened_at DESC
    """, {"subjects": subjects})} if subjects else {}
    can_open = visibility.visible_case_ids(conn, user, [r["id"] for r in latest.values()])
    label_of = {d["token"]: d["label"] for d in destinations}
    linked_out = []
    for r in linked:
        c = latest.get(r["subject_token"])
        linked_out.append({
            "name": names.get(r["subject_token"]) or "Another customer",
            "via": r["via"],
            "accounts": sorted(label_of[t] for t in (r["tokens"] or []) if t in label_of),
            "payments": int(r["payments"]),
            "value_minor": int(r["value_minor"]),
            "home_region": r["home_region"],
            "states": r["states"],
            "case_id": c["id"] if c else None,
            "case_state": c["state"] if c else None,
            "case_outcome": c["outcome"] if c else None,
            "case_visible": bool(c and c["id"] in can_open),
        })

    # 7. Other cases nearby with the same warning signs: same state, within two
    #    days of this case's alerts, sharing a reason (or, for alerts the model
    #    raised alone, simply alerting there).
    regions = sorted({e["ip_region"] for e in events if e["ip_region"]})
    codes = sorted({c for e in events for c in (e["codes"] or [])})
    nearby: list[dict[str, Any]] = []
    if regions:
        span = _rows(conn, "SELECT min(t.occurred_at) AS lo, max(t.occurred_at) AS hi FROM alerts a "
                           "JOIN transactions t ON t.id = a.transaction_id WHERE a.case_id = %(case_id)s", p)[0]
        nearby = _ints(_rows(conn, """
            SELECT a.case_id, t.ip_region, count(DISTINCT a.id) AS alerts,
                   coalesce(array_agg(DISTINCT s->>'code') FILTER (WHERE s->>'code' = ANY(%(codes)s)), '{}') AS shared,
                   max(a.raised_at) AS last_alert_at,
                   max(c.state::text) AS state, max(c.outcome::text) AS outcome, max(c.risk_level::text) AS risk_level
              FROM alerts a
              JOIN transactions t ON t.id = a.transaction_id
              JOIN decisions d    ON d.id = a.decision_id
              JOIN cases c        ON c.id = a.case_id
              LEFT JOIN LATERAL jsonb_array_elements(d.signals) s ON true
             WHERE a.case_id <> %(case_id)s
               AND t.ip_region = ANY(%(regions)s)
               AND t.occurred_at BETWEEN %(lo)s - interval '48 hours' AND %(hi)s + interval '48 hours'
             GROUP BY a.case_id, t.ip_region
            HAVING cardinality(%(codes)s::text[]) = 0
                OR bool_or(s->>'code' = ANY(%(codes)s))
             ORDER BY count(DISTINCT a.id) DESC, max(a.raised_at) DESC
             LIMIT %(limit)s
        """, {**p, "regions": regions, "codes": codes, "lo": span["lo"], "hi": span["hi"],
              "limit": MAX_NEARBY}), "alerts")
        open_nearby = visibility.visible_case_ids(conn, user, [r["case_id"] for r in nearby])
        for r in nearby:
            r["case_visible"] = r["case_id"] in open_nearby

    name = _rows(conn, f"SELECT max(t.display_name) AS n FROM transactions t "
                       f"WHERE t.subject_token = %(subject)s AND {window}", p)[0]["n"]
    for e in events:
        e["destination"] = label_of.get(e.pop("beneficiary_token") or "", None)
    # The screen names accounts A, B, C and shows the last four characters of
    # the token; the token itself has no use on screen.
    graph = _network(conn, p, window, case=case, name=name, events=events, usual=usual,
                     destinations=destinations, device_rows=device_rows, linked=linked,
                     linked_out=linked_out, nearby=nearby)
    for e in events:
        e.pop("device_token", None)  # the raw token never leaves the server
    for d in destinations:
        d.pop("token")

    return {
        "graph": graph,
        "case": {k: case[k] for k in ("id", "opened_at", "state", "outcome", "risk_level")},
        "customer_name": name,
        "window": {"days": days, "start": start, "end": end},
        "events": events,
        "usual_regions": _ints(usual, "n"),
        "footprint": footprint,
        "money_in": money_in,
        "flow": {k: int(v) for k, v in flow.items()},
        "destinations": destinations,
        "linked": linked_out,
        "nearby": nearby,
        "nearby_codes": codes,
        "note": NOTE,
        "limits": ("Destinations are accounts at other banks. Where money goes after it reaches them "
                   "is not visible to this bank, so the survey stops at the receiving account."),
    }


# How much a node deserves a look, for ordering the list beside the graph. A
# ranking aid built from what is recorded — never a model score — and every
# point it gives is said in words beside it.
_CASE_WEIGHT = {"CRITICAL": 35, "HIGH": 28, "MEDIUM": 20, "LOW": 10}
_OUTCOME_WORDS = {"CONFIRMED_FRAUD": "Fraud confirmed", "FALSE_POSITIVE": "No fraud found",
                  "INCONCLUSIVE": "More review needed"}


def _attention(points: list[tuple[int, str]]) -> tuple[int, list[str], list[dict[str, Any]]]:
    """The total (capped at 100), the reasons in order, and each reason with its points."""
    kept = sorted([(n, why) for n, why in points if n > 0], key=lambda x: -x[0])
    return (min(100, sum(n for n, _ in kept)), [why for _, why in kept],
            [{"why": why, "points": n} for n, why in kept])


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _network(conn: Any, p: dict[str, Any], window: str, *, case: dict[str, Any], name: str | None,
             events: list[dict[str, Any]], usual: list[dict[str, Any]], destinations: list[dict[str, Any]],
             device_rows: list[dict[str, Any]], linked: list[dict[str, Any]], linked_out: list[dict[str, Any]],
             nearby: list[dict[str, Any]]) -> dict[str, Any]:
    """One case's people, accounts, devices and similar cases as nodes and timed links.

    Every link carries the moment it first happened in the window, so the
    console can play the network forward in time. Nodes carry a state where one
    is known, so the same graph can be laid over the map. Identifiers stay
    inside: accounts are A, B, C; devices 1, 2, 3; other customers P1, P2.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    alert_states = sorted({e["ip_region"] for e in events if e["ip_region"]})
    first_event = min((e["occurred_at"] for e in events), default=None)
    nodes.append({
        "id": "me", "kind": "customer", "label": name or "This customer",
        "state": (alert_states or [None])[0], "home": usual[0]["ip_region"] if usual else None,
        "alert_states": alert_states, "time": first_event, "risk_level": case["risk_level"],
        "detail": f"{_plural(len(events), 'flagged payment')} on this case",
        "attention": 100, "reasons": ["The customer on this case"],
    })

    acc_id: dict[str, str] = {}
    for d in destinations:
        nid = f"acc:{d['label'][-1]}"
        acc_id[d["token"]] = nid
        score, reasons, breakdown = _attention([
            (40 if d["confirmed_fraud_cases"] else 0,
             f"Tied to {_plural(d['confirmed_fraud_cases'], 'confirmed fraud case')}"),
            (35 if "KNOWN_MULE" in (d["lists"] or []) else 0, "On the known mule list"),
            (35 if "SANCTIONED" in (d["lists"] or []) else 0, "On the sanctions list"),
            (min(55, d["other_customers"] * 5), f"Also paid by {_plural(d['other_customers'], 'other customer')}"),
            (10 if d["alerts"] else 0, f"{_plural(d['alerts'], 'payment')} to it from this customer alerted"),
        ])
        nodes.append({
            "id": nid, "kind": "account", "label": d["label"], "ending": d["ending"], "state": None,
            "payer_states": [x["ip_region"] for x in d["payers_by_state"]], "time": d["first_paid"],
            "detail": f"Account at another bank, ending {d['ending']}",
            "payments": d["payments"], "value_minor": d["value_minor"], "others": d["other_customers"],
            "lists": d["lists"], "confirmed_fraud_cases": d["confirmed_fraud_cases"],
            "attention": score, "reasons": reasons, "breakdown": breakdown,
        })
        edges.append({"from": "me", "to": nid, "kind": "paid", "time": d["first_paid"],
                      "count": d["payments"], "value_minor": d["value_minor"]})

    dev_id: dict[str, str] = {}
    # When this customer first ever used each device, not only in the window: a
    # phone used for a year is not new because the window starts last week.
    first_ever = {r["device_token"]: r["first_at"] for r in _rows(conn, """
        SELECT device_token, min(occurred_at) AS first_at FROM transactions
         WHERE subject_token = %(subject)s AND device_token = ANY(%(devices)s) GROUP BY 1
    """, {**p, "devices": [d["token"] for d in device_rows]})} if device_rows else {}
    flagged_from: dict[str, int] = {}
    for e in events:
        if e.get("device_token"):
            flagged_from[e["device_token"]] = flagged_from.get(e["device_token"], 0) + 1
    for i, d in enumerate(device_rows, start=1):
        nid = f"dev:{i}"
        dev_id[d["token"]] = nid
        began = first_ever.get(d["token"], d["first_used"])
        flagged = flagged_from.get(d["token"], 0)
        # New: first used within a week before this customer's first flagged
        # payment, or after it. The detector's own test (device_is_new_to_subject)
        # is stricter — one earlier payment the same day makes a device "known" —
        # which is exactly how an account takeover's handset slips past it.
        days_before = (first_event - began).total_seconds() / 86400 if first_event and began else None
        new = days_before is not None and days_before <= 7
        if new:
            when_words = ("after the first flagged payment" if days_before < 0
                          else "on the day of the flagged payments" if days_before < 1
                          else f"{_plural(round(days_before), 'day')} before the flagged payments")
        score, reasons, breakdown = _attention([
            (min(60, d["other_customers"] * 20), f"Shared with {_plural(d['other_customers'], 'other customer')}"),
            (45 if new else 0, f"New to this customer: first used {when_words}" if new else ""),
            (min(25, flagged * 10), f"{_plural(flagged, 'flagged payment')} came from it"),
        ])
        nodes.append({"id": nid, "kind": "device", "label": f"Device {i}", "ending": d["token"][-4:],
                      "state": None, "time": d["first_used"], "first_ever": began,
                      "new_device": new, "flagged": flagged, "others": d["other_customers"],
                      "routine": not new and not d["other_customers"],
                      "detail": f"Used {_plural(d['uses'], 'time')} by this customer in the period",
                      "attention": score, "reasons": reasons or ["One of this customer's usual devices"],
                      "breakdown": breakdown})
        edges.append({"from": "me", "to": nid, "kind": "used", "time": d["first_used"], "count": d["uses"],
                      "flagged": flagged})

    # Who else touched which account or device, and when they first did.
    subjects = [r["subject_token"] for r in linked]
    pairs = _rows(conn, f"""
        SELECT t.subject_token, 'paid' AS kind, t.beneficiary_token AS token, min(t.occurred_at) AS first_at,
               count(*) AS n, coalesce(sum(t.amount_minor), 0) AS value_minor
          FROM transactions t
         WHERE t.subject_token = ANY(%(subjects)s) AND t.beneficiary_token = ANY(%(tokens)s) AND {window}
         GROUP BY 1, 3
        UNION ALL
        SELECT t.subject_token, 'used', t.device_token, min(t.occurred_at), count(*), 0
          FROM transactions t
         WHERE t.subject_token = ANY(%(subjects)s) AND t.device_token = ANY(%(devices)s) AND {window}
         GROUP BY 1, 3
    """, {**p, "subjects": subjects, "tokens": list(acc_id), "devices": list(dev_id)}) if subjects else []
    person_id = {s: f"p:{i}" for i, s in enumerate(subjects, start=1)}
    first_seen: dict[str, Any] = {}
    for r in pairs:
        target = acc_id.get(r["token"]) if r["kind"] == "paid" else dev_id.get(r["token"])
        if not target:
            continue
        who = person_id[r["subject_token"]]
        first_seen[who] = min(first_seen.get(who, r["first_at"]), r["first_at"])
        edges.append({"from": who, "to": target, "kind": r["kind"], "time": r["first_at"],
                      "count": int(r["n"]), "value_minor": int(r["value_minor"])})
    for r, out in zip(linked, linked_out):
        nid = person_id[r["subject_token"]]
        links = sum(1 for e in edges if e["from"] == nid)
        if out["case_outcome"] == "CONFIRMED_FRAUD":
            first = (70, f"Fraud confirmed on CASE-{out['case_id']}")
        elif out["case_id"] and out["case_state"] != "CLOSED":
            first = (40, f"Has an open case, CASE-{out['case_id']}")
        elif out["case_id"]:
            first = (10, f"Earlier case closed: {_OUTCOME_WORDS.get(out['case_outcome'], 'closed').lower()}")
        else:
            first = (0, "")
        score, reasons, breakdown = _attention([
            first,
            (min(30, links * 12), f"Shares {_plural(links, 'account or device', 'accounts or devices')} with this customer"),
            (15 if "device" in out["via"] else 0, "Shares a device with this customer"),
        ])
        nodes.append({
            "id": nid, "kind": "person", "label": out["name"], "state": out["home_region"],
            "states": out["states"], "time": first_seen.get(nid),
            "detail": "Mostly pays from an unrecorded state" if not out["home_region"] else None,
            "case_id": out["case_id"], "case_state": out["case_state"], "case_outcome": out["case_outcome"],
            "case_visible": out["case_visible"], "payments": out["payments"], "value_minor": out["value_minor"],
            "attention": score, "reasons": reasons, "breakdown": breakdown,
        })

    for n in nearby:
        nid = f"case:{n['case_id']}"
        if any(x["id"] == nid for x in nodes):
            continue
        score, reasons, breakdown = _attention([
            (_CASE_WEIGHT.get(n["risk_level"], 10), f"{(n['risk_level'] or '').title()} risk case"),
            (min(10, n["alerts"] * 2), f"{_plural(n['alerts'], 'alert')} in the same state within two days"),
            (5 if n["shared"] else 0, "Shares a warning sign with this case"),
            (25 if n["outcome"] == "CONFIRMED_FRAUD" else 0, "Fraud confirmed"),
        ])
        nodes.append({
            "id": nid, "kind": "case", "label": f"CASE-{n['case_id']}", "state": n["ip_region"],
            "time": n["last_alert_at"], "detail": "Alerted in the same state with the same signs",
            "case_id": n["case_id"], "case_state": n["state"], "case_outcome": n["outcome"],
            "case_visible": n["case_visible"], "risk_level": n["risk_level"], "shared": n["shared"],
            "attention": score, "reasons": reasons, "breakdown": breakdown,
        })
        edges.append({"from": "me", "to": nid, "kind": "similar", "time": n["last_alert_at"],
                      "count": n["alerts"]})

    times = [x["time"] for x in nodes + edges if x.get("time")]
    return {
        "nodes": nodes, "edges": edges,
        "span": {"start": min(times) if times else None, "end": max(times) if times else None},
    }

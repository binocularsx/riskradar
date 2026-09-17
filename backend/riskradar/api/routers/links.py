"""Who else is connected to this case, and what is known about each identifier (D84).

Plain English
-------------
A scam collection account, a mule, a phone used to take over several accounts:
none of them is visible from one customer's transactions alone. They show up
when you ask who *else* touched the same destination or the same device.

``GET /v1/cases/{id}/links`` answers that for one case, over the last 30 days:

* the customer, their accounts, the devices they used and the destinations they paid;
* for every destination and device, the *other* customers who used it, whether
  any of those customers has a case, and how that case ended;
* a reputation line per identifier: first seen, customers in the last day and
  the last 30, alerts on payments to it, confirmed fraud, and whether it is on
  the known-mule or sanctions list.

Tokens only (D9c): the graph links hashed identifiers, and names come from the
display name the bank stamped on each transaction. Nothing here changes a
score; it is for the analyst's eyes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...security.rbac import Permission
from ..deps import get_conn, requires

router = APIRouter(prefix="/v1", tags=["links"])

MAX_NEIGHBOURS = 12


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


@router.get("/cases/{case_id}/links")
def case_links(
    case_id: int,
    days: int = Query(30, ge=1, le=90),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    case = _rows(conn, "SELECT id, subject_token, outcome, state FROM cases WHERE id = %s", (case_id,))
    if not case:
        raise HTTPException(404, "case not found")
    subject = case[0]["subject_token"]
    params = {"subject": subject, "days": days, "limit": MAX_NEIGHBOURS}

    customer = _rows(conn, """
        SELECT max(display_name) AS name, count(*) AS transactions,
               count(DISTINCT account_token) AS accounts
          FROM transactions WHERE subject_token = %(subject)s
           AND occurred_at > now() - make_interval(days => %(days)s)
    """, params)[0]

    destinations = _rows(conn, """
        WITH mine AS (
            SELECT beneficiary_token AS token, count(*) AS payments, sum(amount_minor)::bigint AS amount_minor,
                   max(occurred_at) AS last_paid
              FROM transactions
             WHERE subject_token = %(subject)s AND direction = 'OUTBOUND' AND beneficiary_token IS NOT NULL
               AND occurred_at > now() - make_interval(days => %(days)s)
             GROUP BY 1
        )
        SELECT m.*,
               (SELECT min(occurred_at) FROM transactions t WHERE t.beneficiary_token = m.token) AS first_seen,
               (SELECT count(DISTINCT subject_token) FROM transactions t WHERE t.beneficiary_token = m.token
                   AND t.subject_token <> %(subject)s AND t.occurred_at > now() - interval '24 hours') AS other_customers_24h,
               (SELECT count(DISTINCT subject_token) FROM transactions t WHERE t.beneficiary_token = m.token
                   AND t.subject_token <> %(subject)s AND t.occurred_at > now() - make_interval(days => %(days)s)) AS other_customers,
               (SELECT count(*) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE t.beneficiary_token = m.token) AS alerts,
               (SELECT count(DISTINCT c.id) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                  JOIN cases c ON c.id = a.case_id
                 WHERE t.beneficiary_token = m.token AND c.outcome = 'CONFIRMED_FRAUD') AS confirmed_fraud_cases,
               (SELECT array_agg(DISTINCT kind::text) FROM beneficiary_lists l
                 WHERE l.token = m.token AND l.kind IN ('KNOWN_MULE', 'SANCTIONED')) AS lists
          FROM mine m
         ORDER BY other_customers DESC, amount_minor DESC
         LIMIT 25
    """, params)

    devices = _rows(conn, """
        WITH mine AS (
            SELECT device_token AS token, count(*) AS uses, max(occurred_at) AS last_used
              FROM transactions
             WHERE subject_token = %(subject)s AND device_token IS NOT NULL
               AND occurred_at > now() - make_interval(days => %(days)s)
             GROUP BY 1
        )
        SELECT m.*,
               (SELECT min(occurred_at) FROM transactions t WHERE t.device_token = m.token
                   AND t.subject_token = %(subject)s) AS first_used_by_customer,
               (SELECT count(DISTINCT subject_token) FROM transactions t WHERE t.device_token = m.token
                   AND t.subject_token <> %(subject)s AND t.occurred_at > now() - make_interval(days => %(days)s)) AS other_customers
          FROM mine m ORDER BY other_customers DESC, uses DESC LIMIT 10
    """, params)

    # The other customers behind shared destinations and devices, with their case history.
    neighbours = _rows(conn, """
        WITH shared AS (
            SELECT t.subject_token, 'destination' AS via, t.beneficiary_token AS token
              FROM transactions t
             WHERE t.beneficiary_token IN (SELECT beneficiary_token FROM transactions
                                            WHERE subject_token = %(subject)s AND beneficiary_token IS NOT NULL
                                              AND occurred_at > now() - make_interval(days => %(days)s))
               AND t.subject_token <> %(subject)s AND t.occurred_at > now() - make_interval(days => %(days)s)
            UNION
            SELECT t.subject_token, 'device', t.device_token
              FROM transactions t
             WHERE t.device_token IN (SELECT device_token FROM transactions
                                       WHERE subject_token = %(subject)s AND device_token IS NOT NULL
                                         AND occurred_at > now() - make_interval(days => %(days)s))
               AND t.subject_token <> %(subject)s AND t.occurred_at > now() - make_interval(days => %(days)s)
        )
        SELECT s.subject_token, array_agg(DISTINCT s.via) AS via, array_agg(DISTINCT s.token) AS tokens,
               (SELECT max(display_name) FROM transactions t WHERE t.subject_token = s.subject_token) AS name,
               (SELECT c.id FROM cases c WHERE c.subject_token = s.subject_token ORDER BY c.opened_at DESC LIMIT 1) AS latest_case_id,
               (SELECT c.state::text FROM cases c WHERE c.subject_token = s.subject_token ORDER BY c.opened_at DESC LIMIT 1) AS latest_case_state,
               (SELECT c.outcome::text FROM cases c WHERE c.subject_token = s.subject_token ORDER BY c.opened_at DESC LIMIT 1) AS latest_case_outcome
          FROM shared s
         GROUP BY s.subject_token
         ORDER BY (SELECT count(*) FROM cases c WHERE c.subject_token = s.subject_token
                     AND c.outcome = 'CONFIRMED_FRAUD') DESC, count(*) DESC
         LIMIT %(limit)s
    """, params)

    nodes = [{"id": "customer", "kind": "customer", "label": customer["name"] or "This customer",
              "detail": f"{customer['transactions']} transactions, {customer['accounts']} accounts in {days} days"}]
    edges = []
    for d in destinations:
        nid = f"dest:{d['token']}"
        risky = bool(d["confirmed_fraud_cases"] or d["lists"] or (d["other_customers_24h"] or 0) >= 2)
        nodes.append({"id": nid, "kind": "destination", "token": d["token"], "risky": risky, **{
            k: d[k] for k in ("payments", "amount_minor", "first_seen", "other_customers", "other_customers_24h",
                              "alerts", "confirmed_fraud_cases", "lists", "last_paid")}})
        edges.append({"from": "customer", "to": nid, "kind": "paid", "weight": d["payments"]})
    for d in devices:
        nid = f"device:{d['token']}"
        nodes.append({"id": nid, "kind": "device", "token": d["token"], "risky": (d["other_customers"] or 0) >= 2,
                      **{k: d[k] for k in ("uses", "first_used_by_customer", "other_customers", "last_used")}})
        edges.append({"from": "customer", "to": nid, "kind": "used", "weight": d["uses"]})
    shown = {n["id"] for n in nodes}
    for n in neighbours:
        nid = f"other:{n['subject_token']}"
        nodes.append({"id": nid, "kind": "other_customer", "label": n["name"] or "Another customer",
                      "risky": n["latest_case_outcome"] == "CONFIRMED_FRAUD",
                      "case_id": n["latest_case_id"], "case_state": n["latest_case_state"],
                      "case_outcome": n["latest_case_outcome"], "via": n["via"]})
        for via, token in ((v, t) for v in n["via"] for t in n["tokens"]):
            target = f"{'dest' if via == 'destination' else 'device'}:{token}"
            if target in shown:
                edges.append({"from": nid, "to": target, "kind": "paid" if via == "destination" else "used"})
    edges = [e for i, e in enumerate(edges) if e not in edges[:i]]

    summary = {
        "destinations": len(destinations),
        "shared_destinations": sum(1 for d in destinations if d["other_customers"]),
        "devices": len(devices),
        "shared_devices": sum(1 for d in devices if d["other_customers"]),
        "linked_customers": len(neighbours),
        "linked_confirmed_fraud": sum(1 for n in neighbours if n["latest_case_outcome"] == "CONFIRMED_FRAUD"),
        "destinations_on_lists": sum(1 for d in destinations if d["lists"]),
    }
    return {"case_id": case_id, "days": days, "summary": summary, "nodes": nodes, "edges": edges}

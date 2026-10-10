"""The directive contract (WP-07, D74, base PRD FR-301 seam, FR-305).

Plain English
-------------
How a bank's payment switch reads what Risk Radar decided, and tells it what it
did about it. Machine callers use the API key, like ingestion (FR-001).

* ``GET /v1/directives/{transaction_ref}`` — the directive for one payment.
  The first fetch is recorded as the moment the bank was told. A payment not
  yet scored returns 202 with the fail-open action, so the switch never waits
  on Risk Radar longer than its own deadline.
* ``GET /v1/directives?after_id=`` — the same, as a feed to poll.
* ``POST /v1/directives/{directive_ref}/ack`` — what the bank did. Once; a
  repeat with the same answer is a no-op, a different answer is a 409.

Staff see the policy in force and how delivery is going; only an administrator
may publish a new policy version, and LIVE needs a signature (D69a).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ...audit import chain
from ...policy import directives as contract
from ...security.rbac import Permission
from ..deps import get_conn, require_api_key, requires
from ..schemas import DirectiveAckIn, EnforcementPolicyIn

router = APIRouter(prefix="/v1/directives", tags=["directives"])
admin_router = APIRouter(prefix="/v1", tags=["directives"])

DIRECTIVE_SQL = """
    SELECT dr.*, dr.action::text AS action, dr.mode::text AS mode,
           dr.fail_open_action::text AS fail_open_action, dr.ack_action::text AS ack_action,
           t.transaction_ref, d.decision::text AS decision, d.risk_level::text AS risk_level, d.signals
      FROM directives dr
      JOIN transactions t ON t.id = dr.transaction_id
      JOIN decisions d    ON d.id = dr.decision_id
"""


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _now(conn: Any):
    return _rows(conn, "SELECT now() AS ts")[0]["ts"]


def _active_policy(conn: Any) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT version, mode::text AS mode, ttl_seconds, fail_open_action::text AS fail_open_action, "
                       "policy_text, signed_by, signed_at, created_at FROM enforcement_policies WHERE is_active")
    return rows[0] if rows else None


def _mark_delivered(conn: Any, ids: list[int]) -> None:
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE directives SET first_delivered_at = COALESCE(first_delivered_at, now()), "
            "delivery_count = delivery_count + 1 WHERE id = ANY(%s)",
            (ids,),
        )


@router.get("/{transaction_ref}")
def get_directive(
    transaction_ref: str,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    rows = _rows(conn, DIRECTIVE_SQL + " WHERE t.transaction_ref = %s", (transaction_ref,))
    if not rows:
        tx = _rows(conn, "SELECT t.is_replay, (d.id IS NOT NULL) AS decided FROM transactions t "
                         "LEFT JOIN decisions d ON d.transaction_id = t.id WHERE t.transaction_ref = %s",
                   (transaction_ref,))
        policy = _active_policy(conn)
        fail_open = policy["fail_open_action"] if policy else "APPROVE"
        if not tx:
            raise HTTPException(404, "unknown transaction_ref")
        if not tx[0]["decided"]:
            response.status_code = status.HTTP_202_ACCEPTED
            return {"transaction_ref": transaction_ref, "status": "PENDING",
                    "effective_action": fail_open, "enforce": False}
        # Decided, but no directive: replayed history or no active policy.
        return {"transaction_ref": transaction_ref, "status": "NO_DIRECTIVE",
                "effective_action": fail_open, "enforce": False}
    directive = rows[0]
    _mark_delivered(conn, [directive["id"]])
    fresh = _rows(conn, DIRECTIVE_SQL + " WHERE dr.id = %s", (directive["id"],))[0]
    return {"status": "ISSUED", **contract.as_contract(fresh, _now(conn))}


@router.get("")
def directive_feed(
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    """Directives issued after ``after_id``, oldest first. Poll with the last ``id``."""
    rows = _rows(conn, DIRECTIVE_SQL + " WHERE dr.id > %s ORDER BY dr.id LIMIT %s", (after_id, limit))
    _mark_delivered(conn, [r["id"] for r in rows])
    now = _now(conn)
    return {
        "items": [{"id": r["id"], **contract.as_contract(r, now)} for r in rows],
        "next_after_id": rows[-1]["id"] if rows else after_id,
    }


@router.post("/{directive_ref}/ack")
def acknowledge(
    directive_ref: UUID,
    body: DirectiveAckIn,
    response: Response,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    rows = _rows(conn, DIRECTIVE_SQL + " WHERE dr.directive_ref = %s FOR UPDATE OF dr", (str(directive_ref),))
    if not rows:
        raise HTTPException(404, "unknown directive_ref")
    d = rows[0]
    if d["acknowledged_at"] is not None:
        same = (d["ack_action"], d["ack_taken_at"], d["ack_reason"]) == (body.action_taken, body.taken_at, body.reason)
        if not same:
            raise HTTPException(409, "this directive is already acknowledged with a different answer")
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate", "directive_ref": str(directive_ref)}
    if d["mode"] == "SHADOW" and body.action_taken == "APPLIED":
        # A shadow directive that a bank says it enforced is a contract breach
        # worth refusing loudly rather than recording quietly.
        raise HTTPException(400, "a SHADOW directive cannot be APPLIED; record SHADOW_RECORDED")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE directives SET acknowledged_at = now(), ack_action = %s, ack_taken_at = %s, ack_reason = %s "
            "WHERE id = %s",
            (body.action_taken, body.taken_at, body.reason, d["id"]),
        )
    return {"status": "acknowledged", "directive_ref": str(directive_ref)}


# ---------------------------------------------------------------------------
# Staff: the policy in force, publishing a version, and delivery health
# ---------------------------------------------------------------------------


@admin_router.get("/admin/enforcement-policy")
def enforcement_policy(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    versions = _rows(conn, "SELECT version, mode::text AS mode, ttl_seconds, fail_open_action::text AS fail_open_action, "
                           "signed_by, signed_at, created_at, is_active FROM enforcement_policies ORDER BY version DESC")
    return {"active": _active_policy(conn), "versions": versions}


@admin_router.post("/admin/enforcement-policy")
def publish_enforcement_policy(
    body: EnforcementPolicyIn,
    user: dict = Depends(requires(Permission.ADMIN_THRESHOLDS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Publish a new version and make it active. A running directive keeps its version."""
    if body.mode == "LIVE" and not (body.signed_by and body.signed_at):
        raise HTTPException(400, "LIVE enforcement needs a policy signed by the bank sponsor (D69a)")
    current = _active_policy(conn)
    version = _rows(conn, "SELECT coalesce(max(version), 0) + 1 AS v FROM enforcement_policies")[0]["v"]
    with conn.cursor() as cur:
        cur.execute("UPDATE enforcement_policies SET is_active = false WHERE is_active")
        cur.execute(
            """
            INSERT INTO enforcement_policies (version, mode, ttl_seconds, fail_open_action, policy_text,
                                              signed_by, signed_at, created_by, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)
            """,
            (version, body.mode, body.ttl_seconds, body.fail_open_action, body.policy_text,
             body.signed_by, body.signed_at, user["id"]),
        )
    chain.append(
        conn, actor_user_id=user["id"], action="ENFORCEMENT_POLICY_PUBLISHED", object_type="enforcement_policy",
        object_id=version, from_state=current["mode"] if current else None, to_state=body.mode,
        payload={"ttl_seconds": body.ttl_seconds, "fail_open_action": body.fail_open_action,
                 "signed_by": body.signed_by},
    )
    return {"active": _active_policy(conn)}


@admin_router.get("/metrics/directives")
def directive_metrics(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Is the bank being told in time, and what does it say it did? (FR-305)"""
    window = f"{int(days)} days"
    by_action = _rows(conn, f"""
        SELECT action::text AS action, mode::text AS mode, count(*) AS issued,
               count(*) FILTER (WHERE first_delivered_at IS NOT NULL) AS delivered,
               count(*) FILTER (WHERE first_delivered_at IS NOT NULL AND first_delivered_at < expires_at)
                   AS delivered_in_time,
               count(*) FILTER (WHERE acknowledged_at IS NOT NULL) AS acknowledged
          FROM directives WHERE issued_at > now() - interval '{window}'
         GROUP BY 1, 2 ORDER BY 1, 2
    """)
    timing = _rows(conn, f"""
        SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM first_delivered_at - issued_at))
                   AS median_seconds_to_delivery,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM first_delivered_at - issued_at))
                   AS p95_seconds_to_delivery
          FROM directives
         WHERE issued_at > now() - interval '{window}' AND first_delivered_at IS NOT NULL
    """)[0]
    acks = _rows(conn, f"""
        SELECT action::text AS action, ack_action::text AS ack_action, count(*) AS n
          FROM directives
         WHERE issued_at > now() - interval '{window}' AND acknowledged_at IS NOT NULL
         GROUP BY 1, 2 ORDER BY 1, 2
    """)
    for r in by_action:
        for k in ("issued", "delivered", "delivered_in_time", "acknowledged"):
            r[k] = int(r[k])
    return {"window_days": days, "policy": _active_policy(conn), "by_action": by_action,
            "timing": timing, "acknowledgements": acks}

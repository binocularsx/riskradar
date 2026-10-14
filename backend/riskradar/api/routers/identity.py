"""Industry watch-list doors and integration status (D75).

* ``POST /v1/industry-watchlist/inbound`` — a bank-side connector posts the
  flags other institutions placed, as the hub delivered them (raw BVN, which is
  tokenised here). API key, like ingestion.
* ``GET /v1/industry-watchlist`` — flags in force from other institutions, for
  the desk (no BVN, no token: which case they touch is on the case itself).
* ``GET /v1/admin/integrations`` — which adapter is in use for the core, the
  registry and the hub, what is waiting in the outbox and why, and how many
  customers have a known BVN.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ...identity import industry_sync
from ...security.rbac import Permission
from ..deps import get_conn, require_api_key, requires
from ..schemas import IndustryInboundIn

router = APIRouter(prefix="/v1", tags=["identity"])


@router.post("/industry-watchlist/inbound")
def inbound(
    body: IndustryInboundIn,
    conn: Any = Depends(get_conn),
    api_key: dict = Depends(require_api_key),
) -> dict[str, Any]:
    return industry_sync.receive(conn, [e.model_dump() for e in body.entries], source="INBOUND_API")


@router.get("/industry-watchlist")
def industry_watchlist(
    active: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    where = "iw.lifted_at IS NULL AND iw.expires_at > now()" if active else "true"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT iw.id, iw.external_ref, iw.institution_code, iw.reason_code, iw.flagged_at,
                   iw.expires_at, iw.lifted_at, iw.received_at, iw.source,
                   (SELECT count(*) FROM customer_identities ci WHERE ci.bvn_token = iw.bvn_token)
                       AS our_customer_records
              FROM industry_watchlist iw WHERE {where}
             ORDER BY iw.flagged_at DESC LIMIT %s
            """,
            (limit,),
        )
        items = [dict(r) for r in cur.fetchall()]
    return {"items": items}


@router.get("/admin/integrations")
def integrations(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return industry_sync.status(conn)

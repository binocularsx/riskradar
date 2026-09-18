"""Customer reports, and what the detector missed (D90).

* ``POST /v1/reports`` — the customer names the payments. Whatever Risk Radar
  alerted on joins its case; whatever it missed gets an alert raised by the
  report. The clocks start, the desk is told, the case goes to the top.
* ``GET /v1/metrics/missed`` — of the fraud customers reported, how much the
  detector had already flagged and how much it missed, by channel and money.
  The one detection figure a bank can compute from its own traffic without a
  labelled dataset, and the one that says where the model is blind.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...cases import reports
from ...clocks import sweep as clock_sweep
from ...security.rbac import Permission
from ...worker.scoring import system_user_id
from ..deps import get_conn, requires
from ..schemas import CustomerReportIn

router = APIRouter(prefix="/v1", tags=["reports"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@router.post("/reports")
def report(
    body: CustomerReportIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    try:
        done = reports.report_payments(
            conn, user=user, sys_uid=system_user_id(conn), transaction_refs=body.transaction_refs,
            reported_at=body.reported_at, channel=body.channel,
            counterparty_institution=body.counterparty_institution, note=body.note,
        )
    except reports.ReportError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    done["clocks"] = {cid: clock_sweep.clocks_for_case(conn, cid) for cid in done["cases"]}
    return done


@router.get("/metrics/missed")
def missed(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    rows = _rows(
        conn,
        """
        SELECT a.source, t.channel::text AS channel, count(*) AS payments,
               coalesce(sum(t.amount_minor) FILTER (WHERE t.auth_result = 'APPROVED'), 0)::bigint AS value_minor
          FROM alerts a
          JOIN cases c ON c.id = a.case_id
          JOIN transactions t ON t.id = a.transaction_id
         WHERE c.first_reported_at > now() - make_interval(days => %s)
         GROUP BY 1, 2
        """,
        (days,),
    )
    flagged = sum(r["payments"] for r in rows if r["source"] == "DETECTOR")
    missed_n = sum(r["payments"] for r in rows if r["source"] == "CUSTOMER_REPORT")
    flagged_v = sum(r["value_minor"] for r in rows if r["source"] == "DETECTOR")
    missed_v = sum(r["value_minor"] for r in rows if r["source"] == "CUSTOMER_REPORT")
    by_channel: dict[str, dict[str, int]] = {}
    for r in rows:
        c = by_channel.setdefault(r["channel"], {"flagged": 0, "missed": 0})
        c["flagged" if r["source"] == "DETECTOR" else "missed"] += r["payments"]
    total = flagged + missed_n
    return {
        "window_days": days,
        "reported_payments": total,
        "flagged_before_report": flagged,
        "missed_by_detector": missed_n,
        "share_flagged": round(flagged / total, 3) if total else None,
        "value_flagged_minor": flagged_v,
        "value_missed_minor": missed_v,
        "by_channel": by_channel,
        "note": "Counts payments on cases a customer reported. Reported fraud is a floor on all fraud: "
                "some is never reported.",
    }

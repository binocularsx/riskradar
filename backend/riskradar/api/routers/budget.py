"""The alert budget, as the desk and its lead see it (D86).

Plain English
-------------
* ``GET /v1/budget`` — today against the budget: raised, held back, released,
  expired; this hour against its share; whether the day is on pace.
* ``GET /v1/budget/deferred`` — alerts waiting behind the budget, most serious
  first, with when each would expire unread.
* ``POST /v1/budget/deferred/{id}/release`` — a lead pulls one in now, over the
  budget if need be. Counted, and written to the audit chain with their name.
* ``POST /v1/budget/release`` — raise what the budget has room for now, rather
  than waiting for a worker's half-minute pass.
* ``GET /v1/budget/calibration`` — would today's thresholds still fit the
  budget on the traffic just seen, and what would re-derived ones be?
* ``PUT /v1/admin/budget`` and ``POST /v1/admin/thresholds/derive`` — change the
  budget, or publish thresholds solved from recent traffic. Administrators only,
  audited, never an in-place edit of a threshold set.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...audit import chain
from ...policy import budget, calibration
from ...security.rbac import Permission
from ...worker import scoring
from ..deps import get_conn, requires
from ..schemas import BudgetConfigIn, DeriveThresholdsIn

router = APIRouter(prefix="/v1", tags=["budget"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _config_dict(cfg: budget.BudgetConfig) -> dict[str, Any]:
    return {"per_day": cfg.per_day, "hourly_burst": cfg.hourly_burst, "hour_ceiling": cfg.hour_ceiling,
            "enforced": cfg.enforced, "deferral_hours": cfg.deferral_hours}


def budget_status(conn: Any, now: datetime | None = None) -> dict[str, Any]:
    """Shared with /v1/system/status, so both screens read the same numbers."""
    now = now or datetime.now(timezone.utc)
    cfg = budget.load_config(conn)
    day, hour = budget.local_day_hour(now)
    rows = _rows(conn, "SELECT * FROM alert_budget_days WHERE day = %s", (day,))
    today = rows[0] if rows else None
    waiting = _rows(
        conn,
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE risk_level = 'CRITICAL') AS critical,
               min(deferred_at) AS oldest,
               min(expires_at) AS next_expiry
          FROM alert_deferrals WHERE state = 'WAITING'
        """,
    )[0]
    return {
        "local_day": day.isoformat(),
        "local_hour": hour,
        "config": _config_dict(cfg),
        "today": {
            "raised": int(today["raised"]) if today else 0,
            "mandatory": int(today["mandatory"]) if today else 0,
            "machine": int(today["machine"]) if today else 0,
            "deferred": int(today["deferred"]) if today else 0,
            "released": int(today["released"]) if today else 0,
            "expired": int(today["expired"]) if today else 0,
            "hourly": list(today["hourly"]) if today else [0] * 24,
        },
        "pace": budget.pace(today, cfg, now),
        "waiting": {
            "count": int(waiting["n"]),
            "critical": int(waiting["critical"]),
            "oldest_deferred_at": waiting["oldest"],
            "next_expiry_at": waiting["next_expiry"],
        },
    }


@router.get("/budget")
def get_budget(
    days: int = Query(14, ge=1, le=90),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    status = budget_status(conn)
    status["history"] = _rows(
        conn,
        """
        SELECT day, budget, raised, mandatory, machine, deferred, released, expired,
               overrun_alarmed_at IS NOT NULL AS overrun
          FROM alert_budget_days
         WHERE day > %s::date - %s
         ORDER BY day DESC
        """,
        (status["local_day"], days),
    )
    return status


@router.get("/budget/deferred")
def list_deferred(
    state: str = Query("WAITING", pattern="^(WAITING|RELEASED|EXPIRED|ALL)$"),
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    items = _rows(
        conn,
        """
        SELECT f.id, f.decision_id, f.transaction_id, f.risk_level::text AS risk_level,
               f.p_fraud, f.score_0_100, f.signals, f.reason, f.state,
               f.deferred_at, f.expires_at, f.resolved_at, f.alert_id,
               a.case_id, u.display_name AS released_by,
               t.transaction_ref, t.occurred_at, t.amount_minor, t.currency, t.channel::text AS channel,
               t.display_name,
               extract(epoch FROM now() - f.deferred_at)::int AS waited_seconds
          FROM alert_deferrals f
          JOIN transactions t ON t.id = f.transaction_id
          LEFT JOIN alerts a ON a.id = f.alert_id
          LEFT JOIN users u ON u.id = f.released_by
         WHERE (%(state)s = 'ALL' OR f.state = %(state)s)
         ORDER BY CASE WHEN f.state = 'WAITING' THEN 0 ELSE 1 END,
                  f.risk_level DESC, f.p_fraud DESC, f.deferred_at DESC
         LIMIT %(limit)s
        """,
        {"state": state, "limit": limit},
    )
    for item in items:
        item["p_fraud"] = float(item["p_fraud"])
    return {"state": state, "items": items}


@router.post("/budget/deferred/{deferral_id}/release")
def release_deferred_one(
    deferral_id: int,
    user: dict = Depends(requires(Permission.CASES_REASSIGN)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    done = scoring.release_one(conn, scoring.system_user_id(conn), deferral_id, user_id=user["id"])
    if done is None:
        raise HTTPException(409, "not waiting: already released, expired, or unknown")
    return {"released": done, "budget": budget_status(conn)}


@router.post("/budget/release")
def release_now(
    user: dict = Depends(requires(Permission.CASES_REASSIGN)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    done = scoring.release_deferred(conn, scoring.system_user_id(conn))
    return {**done, "budget": budget_status(conn)}


@router.get("/budget/calibration")
def calibration_check(
    last_days: float = Query(7.0, gt=0, le=90),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Do the thresholds in force still fit the budget on the traffic just seen?"""
    cfg = budget.load_config(conn)
    active = scoring.active_thresholds(conn)
    sample = calibration.load_sample(conn, last_days=last_days)
    current = calibration.implied(sample, active)
    try:
        proposed = calibration.derive(sample, cfg.per_day, min_sample=500)
        refused = None
    except calibration.CalibrationRefused as exc:
        proposed, refused = None, str(exc)
    per_day = current.get("alerts_per_day")
    if per_day is None:
        verdict = "NO_DATA"
    elif per_day > cfg.per_day * 1.2:
        verdict = "OVER"      # the guard is deferring; the thresholds need re-deriving
    elif per_day < cfg.per_day * 0.5:
        verdict = "UNDER"     # capacity unspent: the desk could read more
    else:
        verdict = "FITS"
    return {
        "budget_per_day": cfg.per_day,
        "window_days": last_days,
        "active_thresholds": {"version": active.version, "p_monitor": active.p_monitor,
                              "p_review": active.p_review, "p_hold": active.p_hold},
        "current": current,
        "verdict": verdict,
        "proposed": proposed,
        "refused": refused,
        "note": "Implied volumes are before the budget guard; the guard defers whatever exceeds the budget.",
    }


@router.put("/admin/budget")
def set_budget(
    body: BudgetConfigIn,
    user: dict = Depends(requires(Permission.ADMIN_THRESHOLDS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    before = _config_dict(budget.load_config(conn))
    values = {
        "alert_budget_per_day": body.per_day,
        "alert_budget_hourly_burst": body.hourly_burst,
        "alert_budget_enforced": body.enforced,
        "alert_deferral_hours": body.deferral_hours,
    }
    with conn.cursor() as cur:
        for key, value in values.items():
            cur.execute(
                """
                INSERT INTO app_config (key, value, updated_at, updated_by) VALUES (%s, %s, now(), %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
                """,
                (key, json.dumps(value), user["id"]),
            )
    after = _config_dict(budget.load_config(conn))
    chain.append(
        conn, actor_user_id=user["id"], action="ALERT_BUDGET_CHANGED", object_type="app_config",
        object_id="alert_budget", from_state=json.dumps(before), to_state=json.dumps(after),
        payload={"reason": body.reason},
    )
    return {"config": after, "note": "Thresholds were derived for the old budget; check /v1/budget/calibration."}


@router.post("/admin/thresholds/derive")
def derive_thresholds(
    body: DeriveThresholdsIn,
    user: dict = Depends(requires(Permission.ADMIN_THRESHOLDS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    cfg = budget.load_config(conn)
    sample = calibration.load_sample(conn, last_days=body.last_days)
    try:
        result = calibration.derive(sample, cfg.per_day, min_sample=body.min_sample)
    except calibration.CalibrationRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body.publish:
        return {"published": False, "derived": result}

    old = _rows(conn, "SELECT version, p_monitor, p_review, p_hold FROM threshold_sets WHERE is_active")
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM threshold_sets")
        version = int(cur.fetchone()["v"])
        cur.execute("UPDATE threshold_sets SET is_active = false WHERE is_active")
        cur.execute(
            """
            INSERT INTO threshold_sets (version, p_monitor, p_review, p_hold, alert_min_level, notes, created_by, is_active)
            VALUES (%s, %s, %s, %s, 'MEDIUM', %s, %s, true)
            RETURNING id, version
            """,
            (version, result["p_monitor"], result["p_review"], result["p_hold"],
             body.notes or f"derived from a {cfg.per_day}/day budget over {result['sample']} decisions "
                           f"({body.last_days:g} days)", user["id"]),
        )
        created = dict(cur.fetchone())
    chain.append(
        conn, actor_user_id=user["id"], action="THRESHOLDS_CHANGED", object_type="threshold_set",
        object_id=version, from_state=json.dumps(old[0], default=str) if old else None,
        to_state=json.dumps({k: result[k] for k in ("p_monitor", "p_review", "p_hold")}),
        payload={"via": "POST /v1/admin/thresholds/derive", "alert_budget_per_day": cfg.per_day,
                 "sample_size": result["sample"], "implied_volume": result["implied"]},
    )
    return {"published": True, "threshold_set": created, "derived": result}

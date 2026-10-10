"""Is the service alive, is it ready, and how is it doing (D87).

Plain English
-------------
* ``GET /health/live`` — the process answers. No database call: an orchestrator
  restarting the API because Postgres is slow would make things worse.
* ``GET /health/ready`` — it can do its job: the database answers, a model, a
  ruleset and thresholds are active, at least one scoring worker has been seen
  recently, and the live queue is not so far behind that scores arrive late.
  ``503`` with the failing checks when not, so a load balancer stops routing.
* ``GET /v1/system/status`` — one read for the console's header: versions in
  force, workers, queue and lag, today's alert budget, the stream's rate, and
  the last alarms. Anyone signed in may read it.

``GET /health`` is kept as it was, for existing callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...config import settings
from ...db import pool
from ...security.tokens import hash_api_key
from ..deps import current_user, get_conn
from .budget import budget_status

router = APIRouter(tags=["ops"])

API_VERSION = "1.1.0"
DEV_API_KEY = "rr_dev_simulator_key_do_not_use_in_production"
# D109g: the seeded support-team key is just as public.
DEV_SUPPORT_API_KEY = "rr_dev_support_key_do_not_use_in_production"


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _workers(conn: Any) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT worker_id, host, pid, started_at, seen_at, processed, failed,
               extract(epoch FROM now() - seen_at)::int AS seen_seconds_ago
          FROM worker_heartbeats ORDER BY worker_id
        """,
    )


def _queue(conn: Any) -> dict[str, Any]:
    q = _rows(
        conn,
        """
        SELECT count(*) FILTER (WHERE priority = 0) AS live,
               count(*) FILTER (WHERE priority > 0) AS replay,
               coalesce(extract(epoch FROM now() - min(enqueued_at) FILTER (WHERE priority = 0)), 0)::int
                   AS oldest_live_seconds,
               count(*) FILTER (WHERE attempts > 1) AS retrying
          FROM scoring_queue
        """,
    )[0]
    return {k: int(v) for k, v in q.items()}


def readiness(conn: Any) -> dict[str, Any]:
    s = settings()
    checks: dict[str, dict[str, Any]] = {}
    active = _rows(
        conn,
        """
        SELECT (SELECT count(*) FROM model_versions WHERE is_active) AS model,
               (SELECT count(*) FROM rulesets WHERE is_active) AS ruleset,
               (SELECT count(*) FROM threshold_sets WHERE is_active) AS thresholds
        """,
    )[0]
    checks["database"] = {"ok": True}
    checks["model"] = {"ok": active["model"] == 1,
                       "note": None if active["model"] == 1 else "no active model: scoring runs rules-only (FR-017)"}
    checks["ruleset"] = {"ok": active["ruleset"] == 1}
    checks["thresholds"] = {"ok": active["thresholds"] == 1}
    workers = _workers(conn)
    alive = [w for w in workers if w["seen_seconds_ago"] <= s.worker_stale_seconds]
    checks["workers"] = {"ok": bool(alive), "alive": len(alive), "known": len(workers),
                         "note": None if alive else "no scoring worker seen recently; transactions queue unscored"}
    queue = _queue(conn)
    lag_ok = queue["oldest_live_seconds"] < 120 and queue["live"] < s.queue_hard_limit
    checks["queue"] = {"ok": lag_ok, **queue}
    # The model is not required to be ready: rules-only is a degraded mode the
    # system reports, not an outage. Everything else is.
    required = ["database", "ruleset", "thresholds", "workers", "queue"]
    if s.is_production:
        # The seeded development key is public (it is in this repository). A
        # production service that still accepts it is not ready for traffic.
        dev_key = _rows(conn, "SELECT count(*) AS n FROM api_keys WHERE key_hash = ANY(%s) AND active",
                        ([hash_api_key(DEV_API_KEY), hash_api_key(DEV_SUPPORT_API_KEY)],))[0]["n"]
        checks["dev_credentials"] = {"ok": dev_key == 0,
                                     "note": None if dev_key == 0 else "revoke the seeded development API keys"}
        required.append("dev_credentials")
    ready = all(checks[k]["ok"] for k in required)
    return {"ready": ready, "checks": checks}


@router.get("/health/live")
def live() -> dict[str, Any]:
    return {"status": "alive", "version": API_VERSION, "time": datetime.now(timezone.utc).isoformat()}


@router.get("/health/ready")
def ready() -> JSONResponse:
    try:
        with pool().connection() as conn:
            body = readiness(conn)
    except Exception as exc:  # noqa: BLE001 - the answer is "not ready", with the reason
        body = {"ready": False, "checks": {"database": {"ok": False, "note": str(exc)[:300]}}}
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


@router.get("/v1/system/status")
def system_status(
    user: dict = Depends(current_user),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    s = settings()
    in_force = _rows(
        conn,
        """
        SELECT (SELECT json_build_object('id', id, 'name', name, 'version', version, 'feature_spec_version',
                                         feature_spec_version, 'promoted_at', promoted_at)
                  FROM model_versions WHERE is_active) AS model,
               (SELECT json_build_object('id', id, 'version', version) FROM rulesets WHERE is_active) AS ruleset,
               (SELECT json_build_object('id', id, 'version', version, 'p_monitor', p_monitor::float,
                                         'p_review', p_review::float, 'p_hold', p_hold::float)
                  FROM threshold_sets WHERE is_active) AS thresholds,
               (SELECT json_build_object('version', version, 'review_capacity_per_day', review_capacity_per_day)
                  FROM disposition_policies WHERE is_active) AS disposition_policy
        """,
    )[0]
    stream = _rows(
        conn,
        """
        SELECT count(*) FILTER (WHERE ingested_at > now() - interval '1 minute' AND NOT is_replay) AS last_minute,
               count(*) FILTER (WHERE ingested_at > now() - interval '15 minutes' AND NOT is_replay) AS last_15_minutes,
               max(ingested_at) FILTER (WHERE NOT is_replay) AS last_live_at
          FROM transactions
         WHERE ingested_at > now() - interval '15 minutes'
        """,
    )[0]
    latency = _rows(
        conn,
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
               count(*) AS n
          FROM decisions WHERE decided_at > now() - interval '15 minutes'
        """,
    )[0]
    alarms = _rows(
        conn,
        """
        SELECT id, payload->>'code' AS code, payload->>'detail' AS detail, created_at
          FROM stream_events WHERE event_type = 'alarm'
           AND created_at > now() - interval '24 hours'
         ORDER BY id DESC LIMIT 10
        """,
    )
    ready = readiness(conn)
    return {
        "version": API_VERSION,
        "env": s.env,
        "time": datetime.now(timezone.utc).isoformat(),
        "ready": ready["ready"],
        "checks": ready["checks"],
        "in_force": in_force,
        "workers": _workers(conn),
        "stream": {
            "per_minute": int(stream["last_minute"] or 0),
            "per_minute_15m_avg": round(int(stream["last_15_minutes"] or 0) / 15.0, 1),
            "last_live_at": stream["last_live_at"],
        },
        "scoring_latency_ms_15m": {
            "p50": float(latency["p50"]) if latency["p50"] is not None else None,
            "p95": float(latency["p95"]) if latency["p95"] is not None else None,
            "decisions": int(latency["n"]),
        },
        "budget": budget_status(conn),
        "limits": {
            "ingest_rate_per_key": s.ingest_rate,
            "ingest_burst_per_key": s.ingest_burst,
            "queue_soft_limit": s.queue_soft_limit,
            "queue_hard_limit": s.queue_hard_limit,
            "replay_queue_limit": s.replay_queue_limit,
        },
        "recent_alarms": alarms,
    }

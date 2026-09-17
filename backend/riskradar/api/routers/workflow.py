"""Following a case to resolution, and watching work arrive (D83).

Plain English
-------------
Five things the desk could not do from the dashboard:

* ``GET /v1/workflow/catalog`` — every action that can be recorded, and when and
  why to escalate, so the screen explains itself from one source.
* ``GET /v1/cases/{id}/workflow`` — where this case is (its stage), what has
  happened to it and by whom, and which recommended steps are done.
* ``POST /v1/cases/{id}/actions`` — "I blocked the card", "customer reached,
  denied it": a step done in the bank's systems, recorded here with its result.
* ``POST /v1/cases/{id}/return`` — InfoSec or a lead hands an escalated case back
  to the analyst who raised it, with findings.
* ``GET /v1/workflow/pipeline`` and ``GET /v1/metrics/intake`` — the desk as a
  pipeline (how many cases in each stage, how long they have waited) and the
  stream as it arrives (payments a minute, what is waiting to be scored, what was
  flagged, what to expect in the next hour). Readable by anyone who works cases,
  not only a lead: an analyst deciding whether to take a break needs to know
  what is coming.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...audit import chain
from ...cases import workflow
from ...security.rbac import Permission
from ..deps import get_conn, requires
from ..schemas import CaseActionIn, ReturnIn
from . import triage as triage_router

router = APIRouter(prefix="/v1", tags=["workflow"])


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _case(conn: Any, case_id: int) -> dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM cases WHERE id = %s", (case_id,))
    if not rows:
        raise HTTPException(404, "case not found")
    return rows[0]


@router.get("/workflow/catalog")
def catalog(user: dict = Depends(requires(Permission.CASES_READ))) -> dict[str, Any]:
    return workflow.catalog()


@router.get("/cases/{case_id}/workflow")
def case_workflow(
    case_id: int,
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Stage, lifecycle and steps for one case."""
    case = _case(conn, case_id)
    rows = _rows(conn, triage_router.WORKLIST_SQL.format(where="c.id = %(id)s"), {"id": case_id})
    rec = triage_router._enrich(rows[0], triage_router._clock_context(conn))["recommendation"] if rows else {}

    actions = _rows(
        conn,
        """
        SELECT a.id, a.action_code, a.result, a.detail, a.created_at, u.display_name AS actor
          FROM case_actions a JOIN users u ON u.id = a.actor_id
         WHERE a.case_id = %s ORDER BY a.created_at
        """,
        (case_id,),
    )
    events = _rows(
        conn,
        """
        SELECT occurred_at, action, from_state, to_state, payload,
               (SELECT display_name FROM users WHERE id = actor_user_id) AS actor
          FROM audit_log
         WHERE object_type = 'case' AND object_id = %s AND action <> 'CASE_VIEWED'
         ORDER BY occurred_at
        """,
        (str(case_id),),
    )
    names = _rows(conn, "SELECT id, display_name FROM users WHERE id = ANY(%s)",
                  ([x for x in (case["assignee_id"], case["escalated_by"], case["closed_by"]) if x],))
    name = {r["id"]: r["display_name"] for r in names}

    done = {a["action_code"] for a in actions}
    steps = []
    for s in workflow.steps_for(rec.get("action")):
        code = s["action"]
        status = ("done" if (code in done
                             or (code == "OUTCOME" and case["outcome"])
                             or (code == "ESCALATE" and case["escalated_at"]))
                  else "info" if not code else "todo")
        steps.append({**s, "status": status,
                      "label": workflow.ACTIONS.get(code, {}).get("label")})

    milestones = [
        {"key": "OPENED", "label": "Alert opened the case", "at": case["opened_at"], "by": "Risk Radar"},
    ]
    for e in events:
        label = {
            "CASE_ASSIGNED_FROM_WORKLIST": "Taken from the queue",
            "CASE_REVIEW_STARTED": "Review started",
            "CASE_ESCALATED": f"Escalated to {((e['payload'] or {}).get('target') or '').replace('_', ' ').title()}",
            "CASE_RETURNED": "Handed back with findings",
            "CASE_ACTION_RECORDED": f"{workflow.ACTIONS.get((e['payload'] or {}).get('action_code'), {}).get('label', 'Action')}",
            "CASE_OUTCOME_SET": f"Outcome recorded: {(e['to_state'] or '').replace('_', ' ').lower()}",
            "CASE_CLOSED": "Closed",
            "CASE_REASSIGNED": "Reassigned",
        }.get(e["action"])
        if label:
            milestones.append({"key": e["action"], "label": label, "at": e["occurred_at"], "by": e["actor"],
                               "detail": (e["payload"] or {}).get("result_label") or (e["payload"] or {}).get("reason")})

    current = workflow.stage(case)
    return {
        "case_id": case_id,
        "stage": current,
        "stage_label": workflow.STAGE_LABEL[current],
        "stages": [{"key": s, "label": workflow.STAGE_LABEL[s],
                    "state": "done" if workflow.STAGES.index(s) < workflow.STAGES.index(current)
                    else "current" if s == current else "later"}
                   for s in workflow.STAGES if s != "ESCALATED" or case["escalated_at"] or current == "ESCALATED"],
        "assignee": name.get(case["assignee_id"]),
        "assignee_id": case["assignee_id"],
        "escalation": ({"to": case["escalated_to"], "by": name.get(case["escalated_by"]),
                        "by_id": case["escalated_by"], "at": case["escalated_at"],
                        "reason": case["escalation_reason"]} if case["escalated_at"] else None),
        "recommendation": rec,
        "steps": steps,
        "actions": actions,
        "lifecycle": milestones,
        "next": _next_for(case, user),
    }


def _next_for(case: dict[str, Any], user: dict[str, Any]) -> str:
    """One sentence: what happens to this case next, and who does it."""
    st = workflow.stage(case)
    perms = user["permissions"]
    if st == "CLOSED":
        return "Closed. Nothing more to do; the outcome is now a training label."
    if st == "AWAITING_CLOSE":
        return ("Close it: the outcome is recorded." if "cases:close" in perms
                else "Nothing more for you: a Fraud Ops lead reviews the outcome and closes it.")
    if st == "ESCALATED":
        team = (case["escalated_to"] or "").replace("_", " ").title()
        return f"Waiting in {team}'s escalated queue. They take it, investigate, and hand it back or close it."
    if st == "NEW":
        return "Nobody has taken it. Take it to start the review."
    if case["assignee_id"] == user["id"]:
        return "Yours: work the steps, then record an outcome."
    return "Someone else is working it."


@router.post("/cases/{case_id}/actions")
def record_action(
    case_id: int,
    body: CaseActionIn,
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """A step done in the bank's systems, recorded here with its result.

    Only the person working the case records steps on it, so the record says who
    did what. Risk Radar does not perform the step (D7); it remembers it.
    """
    case = _case(conn, case_id)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "the case is closed")
    if case["assignee_id"] != user["id"]:
        raise HTTPException(403, "take the case before recording steps on it")
    spec = workflow.ACTIONS.get(body.action_code)
    if not spec:
        raise HTTPException(422, f"unknown action {body.action_code}")
    if body.result not in spec["results"]:
        raise HTTPException(422, f"{body.action_code} results are {sorted(spec['results'])}")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO case_actions (case_id, action_code, result, detail, actor_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (case_id, body.action_code, body.result, body.detail, user["id"]),
        )
        row = cur.fetchone()
        action_id = row["id"] if isinstance(row, dict) else row[0]
    chain.append(
        conn, actor_user_id=user["id"], action="CASE_ACTION_RECORDED", object_type="case", object_id=case_id,
        payload={"action_code": body.action_code, "result": body.result,
                 "result_label": spec["results"][body.result], "action_id": action_id},
    )
    stamped = None
    # Telling the receiving bank is also a regulatory milestone once the customer has reported.
    if (body.action_code == "RECEIVING_BANK_NOTIFIED" and body.result == "DONE"
            and case["first_reported_at"] and not case["counterparty_notified_at"]):
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET counterparty_notified_at = now(), "
                        "counterparty_institution = COALESCE(counterparty_institution, %s) WHERE id = %s",
                        (body.detail or "receiving bank", case_id))
        chain.append(conn, actor_user_id=user["id"], action="CLOCK_COUNTERPARTY_NOTIFIED", object_type="case",
                     object_id=case_id, payload={"via": "case action", "action_id": action_id})
        stamped = "COUNTERPARTY_NOTIFIED"
    return {"action_id": action_id, "milestone_stamped": stamped, "workflow": case_workflow(case_id, user, conn)}


@router.post("/cases/{case_id}/return")
def return_case(
    case_id: int,
    body: ReturnIn,
    user: dict = Depends(requires(Permission.CASES_ESCALATE)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Hand an escalated case back to the analyst who raised it, with findings."""
    case = _case(conn, case_id)
    if case["state"] == "CLOSED":
        raise HTTPException(400, "the case is closed")
    if not case["escalated_by"]:
        raise HTTPException(400, "this case was never escalated, so there is nobody to hand it back to")
    if case["escalated_by"] == user["id"]:
        raise HTTPException(400, "you escalated this case; the team it went to hands it back")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                    (case_id, user["id"], f"Findings on return: {body.findings}"))
        cur.execute("UPDATE cases SET state = 'UNDER_REVIEW', assignee_id = %s, escalated_to = NULL WHERE id = %s",
                    (case["escalated_by"], case_id))
    chain.append(conn, actor_user_id=user["id"], action="CASE_RETURNED", object_type="case", object_id=case_id,
                 from_state=case["state"], to_state="UNDER_REVIEW",
                 payload={"returned_to": case["escalated_by"], "reason": body.findings[:500]})
    return case_workflow(case_id, user, conn)


@router.get("/workflow/pipeline")
def pipeline(
    limit: int = Query(40, ge=1, le=200),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Every case in its stage, oldest first, with how long it has waited there."""
    rows = _rows(
        conn,
        """
        SELECT c.id, c.state, c.outcome, c.risk_level, c.assignee_id, c.escalated_to, c.escalated_at,
               c.opened_at, c.closed_at, c.handling,
               u.display_name AS assignee_name,
               (SELECT t.display_name FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                 WHERE a.case_id = c.id ORDER BY a.raised_at LIMIT 1) AS customer_name,
               COALESCE((SELECT sum(t.amount_minor) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                          WHERE a.case_id = c.id AND t.auth_result = 'APPROVED'), 0)::bigint AS exposure_minor,
               (SELECT max(occurred_at) FROM audit_log l WHERE l.object_type = 'case'
                   AND l.object_id = c.id::text AND l.action <> 'CASE_VIEWED') AS last_change_at,
               (SELECT count(*) FROM case_actions x WHERE x.case_id = c.id) AS actions_recorded
          FROM cases c LEFT JOIN users u ON u.id = c.assignee_id
         WHERE c.state <> 'CLOSED' OR c.closed_at > now() - interval '24 hours'
        """,
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stages: dict[str, list[dict[str, Any]]] = {s: [] for s in workflow.STAGES}
    for r in rows:
        s = workflow.stage(r)
        since = {"ESCALATED": r["escalated_at"], "CLOSED": r["closed_at"]}.get(s) or r["last_change_at"] or r["opened_at"]
        r["stage"] = s
        r["minutes_in_stage"] = round((now - since).total_seconds() / 60.0, 1)
        r["age_minutes"] = round((now - r["opened_at"]).total_seconds() / 60.0, 1)
        stages[s].append(r)
    out = []
    for s in workflow.STAGES:
        items = sorted(stages[s], key=lambda r: -r["minutes_in_stage"]) if s != "CLOSED" else \
            sorted(stages[s], key=lambda r: r["minutes_in_stage"])
        out.append({
            "key": s, "label": workflow.STAGE_LABEL[s], "count": len(items),
            "oldest_minutes": max((r["minutes_in_stage"] for r in items), default=None) if s != "CLOSED" else None,
            "exposure_minor": sum(int(r["exposure_minor"]) for r in items),
            "by_outcome": ({o: sum(1 for r in items if r["outcome"] == o)
                            for o in ("CONFIRMED_FRAUD", "FALSE_POSITIVE", "INCONCLUSIVE")} if s in ("AWAITING_CLOSE", "CLOSED") else None),
            "by_team": ({t: sum(1 for r in items if r["escalated_to"] == t) for t in ("INFOSEC", "FRAUD_OPS")}
                        if s == "ESCALATED" else None),
            "items": items[:limit],
        })
    return {"stages": out, "window": "open cases, and cases closed in the last 24 hours"}


@router.get("/metrics/intake")
def intake(
    minutes: int = Query(60, ge=10, le=720),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The stream as it arrives: volume, waiting, flagged, and what to expect."""
    series = _rows(
        conn,
        """
        WITH buckets AS (
            SELECT generate_series(date_trunc('minute', now()) - make_interval(mins => %(m)s - 1),
                                   date_trunc('minute', now()), interval '1 minute') AS minute
        )
        SELECT b.minute,
               (SELECT count(*) FROM transactions t WHERE t.ingested_at >= b.minute
                   AND t.ingested_at < b.minute + interval '1 minute') AS received,
               (SELECT count(*) FROM decisions d WHERE d.decided_at >= b.minute
                   AND d.decided_at < b.minute + interval '1 minute') AS scored,
               (SELECT count(*) FROM decisions d WHERE d.decided_at >= b.minute
                   AND d.decided_at < b.minute + interval '1 minute' AND d.risk_level IN ('HIGH','CRITICAL')) AS high_risk,
               (SELECT count(*) FROM alerts a WHERE a.raised_at >= b.minute
                   AND a.raised_at < b.minute + interval '1 minute') AS alerts
          FROM buckets b ORDER BY b.minute
        """,
        {"m": minutes},
    )
    window = _rows(
        conn,
        """
        SELECT
          (SELECT count(*) FROM transactions WHERE ingested_at > now() - make_interval(mins => %(m)s)) AS received,
          (SELECT coalesce(sum(amount_minor), 0) FROM transactions
            WHERE ingested_at > now() - make_interval(mins => %(m)s))::bigint AS received_value_minor,
          (SELECT count(*) FROM transactions WHERE ingested_at > now() - make_interval(mins => %(m)s)
              AND direction = 'INBOUND') AS credits,
          (SELECT count(*) FROM decisions WHERE decided_at > now() - make_interval(mins => %(m)s)) AS scored,
          (SELECT count(*) FROM alerts WHERE raised_at > now() - make_interval(mins => %(m)s)) AS alerts,
          (SELECT count(*) FROM cases WHERE opened_at > now() - make_interval(mins => %(m)s)) AS cases_opened,
          (SELECT count(*) FROM cases WHERE closed_at > now() - make_interval(mins => %(m)s)) AS cases_closed,
          (SELECT coalesce(sum(t.amount_minor), 0) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
            WHERE a.raised_at > now() - make_interval(mins => %(m)s))::bigint AS flagged_value_minor
        """,
        {"m": minutes},
    )[0]
    queue = _rows(
        conn,
        """
        SELECT count(*) AS waiting,
               EXTRACT(EPOCH FROM (now() - min(enqueued_at))) AS oldest_seconds
          FROM scoring_queue
        """,
    )[0]
    lag = _rows(
        conn,
        """
        SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (d.decided_at - t.ingested_at))) AS p50,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (d.decided_at - t.ingested_at))) AS p95
          FROM decisions d JOIN transactions t ON t.id = d.transaction_id
         WHERE d.decided_at > now() - interval '10 minutes'
        """,
    )[0]
    by_level = _rows(
        conn,
        """
        SELECT risk_level::text AS level, count(*) AS n FROM decisions
         WHERE decided_at > now() - make_interval(mins => %(m)s) GROUP BY 1
        """,
        {"m": minutes},
    )
    by_disposition = _rows(
        conn,
        """
        SELECT coalesce(disposition, 'NONE') AS disposition, count(*) AS n FROM decisions
         WHERE decided_at > now() - make_interval(mins => %(m)s) GROUP BY 1
        """,
        {"m": minutes},
    )
    signals = _rows(
        conn,
        """
        SELECT s->>'code' AS code, s->>'power' AS power, count(*) AS n
          FROM decisions d, jsonb_array_elements(d.signals) s
         WHERE d.decided_at > now() - make_interval(mins => %(m)s)
         GROUP BY 1, 2 ORDER BY 3 DESC
        """,
        {"m": minutes},
    )
    by_channel = _rows(
        conn,
        """
        SELECT t.channel::text AS channel, count(*) AS n,
               count(*) FILTER (WHERE a.id IS NOT NULL) AS alerted
          FROM transactions t LEFT JOIN alerts a ON a.transaction_id = t.id
         WHERE t.ingested_at > now() - make_interval(mins => %(m)s)
         GROUP BY 1 ORDER BY 2 DESC
        """,
        {"m": minutes},
    )
    recent = _rows(
        conn,
        """
        SELECT t.id, t.transaction_ref, t.ingested_at, t.occurred_at, t.amount_minor, t.channel::text AS channel,
               t.direction, t.auth_result::text AS auth_result, t.display_name, t.ip_region,
               d.risk_level::text AS risk_level, d.score_0_100, d.disposition, d.rule_only_mode,
               COALESCE((SELECT array_agg(s->>'code') FROM jsonb_array_elements(d.signals) s), ARRAY[]::text[]) AS signals,
               a.case_id
          FROM transactions t
          LEFT JOIN decisions d ON d.transaction_id = t.id
          LEFT JOIN alerts a ON a.transaction_id = t.id
         ORDER BY t.ingested_at DESC, t.id DESC
         LIMIT 40
        """,
    )
    today = _rows(
        conn,
        """
        SELECT (SELECT count(*) FROM alerts WHERE raised_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Lagos')
                                                          AT TIME ZONE 'Africa/Lagos') AS alerts_today,
               (SELECT value FROM app_config WHERE key = 'alert_budget_per_day') AS budget,
               EXTRACT(EPOCH FROM (now() - (date_trunc('day', now() AT TIME ZONE 'Africa/Lagos')
                                            AT TIME ZONE 'Africa/Lagos'))) / 3600.0 AS hours_elapsed
        """,
    )[0]
    budget = float(today["budget"]) if today["budget"] is not None else None
    hours_elapsed = max(float(today["hours_elapsed"]), 0.25)

    # Expected next hour: the pace of the last 15 minutes, which follows a
    # changing stream faster than the whole window does, beside the window's pace.
    recent15 = series[-15:] if len(series) >= 15 else series
    per_min = lambda key, rows: sum(int(r[key]) for r in rows) / max(len(rows), 1)  # noqa: E731
    expected = {
        "received": round(per_min("received", recent15) * 60),
        "alerts": round(per_min("alerts", recent15) * 60, 1),
        "basis": "the last 15 minutes' pace",
        "alerts_window_pace": round(int(window["alerts"]) / minutes * 60, 1),
    }
    open_stages = pipeline(limit=1, user=user, conn=conn)["stages"]
    return {
        "minutes": minutes,
        "series": series,
        "window": window,
        "queue": {"waiting": int(queue["waiting"]), "oldest_seconds": round(float(queue["oldest_seconds"] or 0), 1),
                  "scoring_lag_seconds_p50": round(float(lag["p50"] or 0), 2),
                  "scoring_lag_seconds_p95": round(float(lag["p95"] or 0), 2)},
        "by_risk_level": {r["level"]: int(r["n"]) for r in by_level},
        "by_disposition": {r["disposition"]: int(r["n"]) for r in by_disposition},
        "signals": signals,
        "by_channel": by_channel,
        "expected_next_hour": expected,
        "budget": {"alerts_per_day": budget, "alerts_today": int(today["alerts_today"]),
                   "hours_elapsed_today": round(hours_elapsed, 2),
                   "projected_today": round(int(today["alerts_today"]) / hours_elapsed * 24, 1)},
        "pipeline": [{k: s[k] for k in ("key", "label", "count", "oldest_minutes")} for s in open_stages],
        "recent": recent,
    }

"""The worklist: what to work next, and recording what you decided.

Plain English
-------------
The queue screen in the first version listed cases. That is not what a fraud
analyst does all day — they work through a pile, one at a time, and the pile has
to be ordered by *what matters*, not by what happened most recently.

These endpoints supply that:

* ``/v1/worklist`` — cases with the money at risk, how close they are to their
  clock running out, and a recommended action in a sentence.
* ``/v1/worklist/next`` — hand me the next case and assign it to me. No
  browsing, no choosing, no two analysts opening the same case.
* ``/v1/cases/{id}/disposition`` — one call that opens the case, records the
  outcome, adds the note and closes it if the analyst is allowed to. In the
  first version that was three separate requests and three separate clicks.

Everything here is advice and workflow. The decision to act on a case is still
entirely the analyst's, and Risk Radar still never moves money (D7).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...audit import chain
from ...cases import triage
from ...clocks import engine as clock_engine
from ...clocks import sweep as clock_sweep
from ...security.rbac import Permission
from ..deps import get_conn, requires
from ..schemas import DispositionIn

router = APIRouter(prefix="/v1", tags=["worklist"])

OPEN_STATES = ("OPEN", "UNDER_REVIEW", "ESCALATED")

# One query does all the per-case arithmetic. Doing it in Python would mean
# pulling every alert of every case across the wire to add up twelve numbers.
WORKLIST_SQL = """
    SELECT c.id,
           c.subject_token,
           c.state,
           c.outcome,
           c.risk_level,
           c.opened_at,
           c.last_alert_at,
           c.alert_count,
           c.assignee_id,
           c.escalated_to,
           c.first_reported_at, c.acknowledged_at, c.counterparty_notified_at,
           c.investigation_concluded_at, c.reimbursed_at, c.clock_policy_version,
           (SELECT min(t.occurred_at) FROM alerts a JOIN transactions t ON t.id = a.transaction_id
             WHERE a.case_id = c.id) AS fraud_first_at,
           u.display_name AS assignee_name,
           EXTRACT(EPOCH FROM (now() - c.opened_at)) / 60.0 AS age_minutes,

           COALESCE((SELECT max(a.score_0_100) FROM alerts a WHERE a.case_id = c.id), 0)
               AS max_score,

           -- Money that actually moved.
           COALESCE((SELECT sum(t.amount_minor)
                       FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                      WHERE a.case_id = c.id AND t.auth_result = 'APPROVED'), 0)::bigint
               AS exposure_minor,

           -- Money they tried to move, including what the bank refused.
           COALESCE((SELECT sum(t.amount_minor)
                       FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                      WHERE a.case_id = c.id), 0)::bigint
               AS attempted_minor,

           COALESCE((SELECT count(*)
                       FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                      WHERE a.case_id = c.id
                        AND t.auth_result IN ('DECLINED', 'FAILED')), 0)::int
               AS declined_count,

           COALESCE((SELECT count(DISTINCT t.beneficiary_token)
                       FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                      WHERE a.case_id = c.id AND t.beneficiary_token IS NOT NULL), 0)::int
               AS distinct_beneficiaries,

           COALESCE((SELECT bool_or((d.features->>'device_is_new_to_subject')::float = 1)
                       FROM alerts a JOIN decisions d ON d.id = a.decision_id
                      WHERE a.case_id = c.id), false)
               AS new_device,

           COALESCE((SELECT array_agg(DISTINCT s->>'code')
                       FROM alerts a
                       JOIN decisions d ON d.id = a.decision_id,
                            jsonb_array_elements(d.signals) s
                      WHERE a.case_id = c.id), ARRAY[]::text[])
               AS signal_codes,

           COALESCE((SELECT array_agg(DISTINCT t.channel::text)
                       FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                      WHERE a.case_id = c.id), ARRAY[]::text[])
               AS channels,

           (SELECT t.display_name
              FROM alerts a JOIN transactions t ON t.id = a.transaction_id
             WHERE a.case_id = c.id ORDER BY a.raised_at LIMIT 1)
               AS customer_name
      FROM cases c
      LEFT JOIN users u ON u.id = c.assignee_id
     WHERE {where}
"""


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _enrich(row: dict[str, Any], clock_context: dict[str, Any]) -> dict[str, Any]:
    """Attach exposure, clocks and recommendation to one case."""
    age = float(row.pop("age_minutes") or 0)
    state, remaining = triage.sla_state(age, row["risk_level"])

    # WP-05: on a case the customer reported, the regulator's clock that runs
    # out first. It feeds lateness in the priority too, so a refund about to
    # breach is not ordered as if only the response clock mattered.
    events = {e: row.pop(e, None) for e in clock_engine.EVENTS}
    version = row.pop("clock_policy_version", None)
    regulatory = None
    if version and events["first_reported_at"]:
        states = clock_engine.evaluate(
            clock_context["policies"][int(version)], events, outcome=row["outcome"],
            now=clock_context["now"], calendar=clock_context["calendar"],
        )
        regulatory = clock_engine.most_urgent(states)
    row["reported"] = events["first_reported_at"] is not None
    row["regulatory_clock"] = regulatory
    signals = list(row.get("signal_codes") or [])

    rec = triage.recommend(
        risk_level=row["risk_level"],
        signals=signals,
        exposure_minor=int(row["exposure_minor"] or 0),
        declined_count=int(row["declined_count"] or 0),
        alert_count=int(row["alert_count"] or 0),
        distinct_beneficiaries=int(row["distinct_beneficiaries"] or 0),
        new_device=bool(row["new_device"]),
    )

    row["age_minutes"] = round(age, 1)
    row["sla_state"] = state
    row["sla_remaining_minutes"] = remaining
    row["recommendation"] = rec.as_dict()
    row["priority"] = triage.priority_score(
        risk_level=row["risk_level"],
        exposure_minor=int(row["exposure_minor"] or 0),
        sla_remaining=min(remaining, regulatory["remaining_minutes"]) if regulatory else remaining,
        alert_count=int(row["alert_count"] or 0),
    )
    return row


def _clock_context(conn: Any) -> dict[str, Any]:
    return {"policies": clock_sweep.policies(conn), "calendar": clock_sweep.load_calendar(conn),
            "now": datetime.now(timezone.utc)}


@router.get("/worklist")
def worklist(
    scope: str = Query("all", pattern="^(all|mine|unassigned|breaching)$"),
    risk_level: str | None = None,
    limit: int = Query(60, ge=1, le=200),
    user: dict = Depends(requires(Permission.CASES_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The pile, ordered by what should be worked first.

    Ordering happens in Python rather than SQL because the priority formula
    combines money, a clock and severity — expressing that in an ORDER BY would
    hide the one piece of logic somebody will want to argue with.
    """
    where = ["c.state = ANY(%(open_states)s)"]
    params: dict[str, Any] = {"open_states": list(OPEN_STATES)}

    if scope == "mine":
        where.append("c.assignee_id = %(uid)s")
        params["uid"] = user["id"]
    elif scope == "unassigned":
        where.append("c.assignee_id IS NULL")
    if risk_level:
        where.append("c.risk_level = %(risk_level)s")
        params["risk_level"] = risk_level

    context = _clock_context(conn)
    rows = [_enrich(r, context) for r in _rows(conn, WORKLIST_SQL.format(where=" AND ".join(where)), params)]

    if scope == "breaching":
        rows = [r for r in rows if r["sla_state"] in ("DUE", "BREACHED")]

    rows.sort(key=lambda r: r["priority"], reverse=True)

    total_exposure = sum(int(r["exposure_minor"] or 0) for r in rows)
    return {
        "items": rows[:limit],
        "total": len(rows),
        "summary": {
            "open_cases": len(rows),
            "total_exposure_minor": total_exposure,
            "breaching": sum(1 for r in rows if r["sla_state"] == "BREACHED"),
            "due_soon": sum(1 for r in rows if r["sla_state"] == "DUE"),
            "regulatory_breached": sum(
                1 for r in rows if (r["regulatory_clock"] or {}).get("state") == "BREACHED"),
            "unassigned": sum(1 for r in rows if not r["assignee_id"]),
            "mine": sum(1 for r in rows if r["assignee_id"] == user["id"]),
        },
    }


@router.post("/worklist/next")
def next_case(
    user: dict = Depends(requires(Permission.CASES_REVIEW)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Hand this analyst the next case and put their name on it.

    Assigning on hand-out is what stops two analysts working the same case, and
    it is also what makes "cases per analyst per day" a real number rather than
    an estimate. A case already assigned to *this* analyst is returned first —
    finish what you started before taking something new.
    """
    context = _clock_context(conn)
    mine = [
        _enrich(r, context)
        for r in _rows(
            conn,
            WORKLIST_SQL.format(where="c.state = 'UNDER_REVIEW' AND c.assignee_id = %(uid)s"),
            {"uid": user["id"]},
        )
    ]
    if mine:
        mine.sort(key=lambda r: r["priority"], reverse=True)
        return {"case": mine[0], "resumed": True}

    available = [
        _enrich(r, context)
        for r in _rows(
            conn,
            WORKLIST_SQL.format(
                where="c.state = ANY(%(open_states)s) AND c.assignee_id IS NULL"
            ),
            {"open_states": list(OPEN_STATES)},
        )
    ]
    if not available:
        return {"case": None, "resumed": False}

    available.sort(key=lambda r: r["priority"], reverse=True)
    chosen = available[0]

    with conn.cursor() as cur:
        # Only claim it if nobody took it between the read and the write.
        cur.execute(
            """
            UPDATE cases
               SET assignee_id = %s,
                   state = CASE WHEN state = 'OPEN' THEN 'UNDER_REVIEW' ELSE state END
             WHERE id = %s AND assignee_id IS NULL
            RETURNING id
            """,
            (user["id"], chosen["id"]),
        )
        if not cur.fetchone():
            raise HTTPException(409, "another analyst took that case — ask again")

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="CASE_ASSIGNED_FROM_WORKLIST",
        object_type="case",
        object_id=chosen["id"],
        from_state=chosen["state"],
        to_state="UNDER_REVIEW",
        payload={"priority": round(chosen["priority"], 1)},
    )
    chosen["state"] = "UNDER_REVIEW"
    chosen["assignee_id"] = user["id"]
    return {"case": chosen, "resumed": False}


@router.post("/cases/{case_id}/disposition")
def disposition(
    case_id: int,
    body: DispositionIn,
    user: dict = Depends(requires(Permission.CASES_SET_OUTCOME)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Record the verdict in one action.

    Previously this was three requests: take for review, set outcome, close. An
    analyst working forty cases a day should press one key, not three buttons —
    and the state machine (D13b) is still respected underneath, with every
    transition audited separately.

    Closing still requires ``cases:close``. An analyst records the outcome and
    the case waits for a lead; a lead does both in the same keystroke.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    case = dict(row) if not isinstance(row, dict) else row

    if case["state"] == "CLOSED":
        raise HTTPException(400, "case is already closed")

    steps: list[str] = []

    # 1. Make sure it is under review, and owned.
    if case["state"] == "OPEN":
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cases SET state = 'UNDER_REVIEW', "
                "assignee_id = COALESCE(assignee_id, %s) WHERE id = %s",
                (user["id"], case_id),
            )
        chain.append(
            conn, actor_user_id=user["id"], action="CASE_REVIEW_STARTED",
            object_type="case", object_id=case_id,
            from_state="OPEN", to_state="UNDER_REVIEW",
        )
        steps.append("opened for review")

    # 2. The note, if there is one.
    if body.note:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                (case_id, user["id"], body.note),
            )
        steps.append("note added")

    # 3. The verdict. This is training data (D13c), not a status field.
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET outcome = %s WHERE id = %s", (body.outcome, case_id))
    chain.append(
        conn, actor_user_id=user["id"], action="CASE_OUTCOME_SET",
        object_type="case", object_id=case_id,
        from_state=case["outcome"], to_state=body.outcome,
        payload={"followed_recommendation": body.followed_recommendation},
    )
    steps.append(f"outcome {body.outcome}")

    # 4. Close, if they are allowed and asked.
    closed = False
    promoted: list[str] = []
    if body.close:
        if "cases:close" not in user["permissions"]:
            steps.append("left open — closing is a Fraud Ops Lead action")
        else:
            if body.outcome == "CONFIRMED_FRAUD":
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO beneficiary_lists (kind, token, note, added_by)
                        SELECT DISTINCT 'KNOWN_MULE', t.beneficiary_token,
                               'auto: confirmed fraud on case ' || %s, %s
                          FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                         WHERE a.case_id = %s AND t.beneficiary_token IS NOT NULL
                        ON CONFLICT DO NOTHING
                        RETURNING token
                        """,
                        (case_id, user["id"], case_id),
                    )
                    promoted = [
                        (r["token"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()
                    ]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cases SET state = 'CLOSED', closed_at = now(), closed_by = %s "
                    "WHERE id = %s",
                    (user["id"], case_id),
                )
            chain.append(
                conn, actor_user_id=user["id"], action="CASE_CLOSED",
                object_type="case", object_id=case_id,
                from_state="UNDER_REVIEW", to_state="CLOSED",
                payload={"outcome": body.outcome, "known_mule_tokens_added": promoted},
            )
            closed = True
            steps.append("closed")

    return {
        "case_id": case_id,
        "outcome": body.outcome,
        "closed": closed,
        "known_mule_tokens_added": promoted,
        "steps": steps,
    }


@router.get("/metrics/operations")
def operations(
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The view a Fraud Ops Lead actually needs, and the one to show a stakeholder.

    Not "how good is the model" — that is the QA report. This answers "is the
    desk coping": how much is open, how old is the oldest, who is carrying what,
    and what proportion of what we raised turned out to be nothing.
    """
    backlog = _rows(
        conn,
        """
        SELECT c.risk_level,
               count(*) AS cases,
               COALESCE(sum((SELECT sum(t.amount_minor)
                               FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                              WHERE a.case_id = c.id AND t.auth_result = 'APPROVED')), 0)::bigint
                   AS exposure_minor,
               round(avg(EXTRACT(EPOCH FROM (now() - c.opened_at)) / 60.0)) AS avg_age_minutes
          FROM cases c
         WHERE c.state = ANY(%s)
         GROUP BY 1
        """,
        (list(OPEN_STATES),),
    )

    ageing = _rows(
        conn,
        """
        SELECT CASE
                 WHEN now() - opened_at < interval '30 minutes'  THEN 'under 30m'
                 WHEN now() - opened_at < interval '2 hours'     THEN '30m - 2h'
                 WHEN now() - opened_at < interval '8 hours'     THEN '2h - 8h'
                 WHEN now() - opened_at < interval '24 hours'    THEN '8h - 24h'
                 ELSE 'over 24h'
               END AS bucket,
               count(*) AS cases
          FROM cases WHERE state = ANY(%s) GROUP BY 1
        """,
        (list(OPEN_STATES),),
    )

    analysts = _rows(
        conn,
        """
        SELECT u.display_name, u.role,
               count(*) FILTER (WHERE c.state = ANY(%s))          AS open_cases,
               count(*) FILTER (WHERE c.state = 'CLOSED'
                                 AND c.closed_at > now() - interval '24 hours') AS closed_24h
          FROM users u LEFT JOIN cases c ON c.assignee_id = u.id
         WHERE NOT u.is_system AND u.role <> 'ADMIN'
         GROUP BY 1, 2 ORDER BY 1
        """,
        (list(OPEN_STATES),),
    )

    outcomes = _rows(
        conn,
        """
        SELECT outcome, count(*) AS n
          FROM cases WHERE outcome IS NOT NULL GROUP BY 1
        """,
    )
    decided = sum(int(o["n"]) for o in outcomes) or 1
    false_positives = sum(int(o["n"]) for o in outcomes if o["outcome"] == "FALSE_POSITIVE")

    # D69f (base PRD §10, O5): time to resolve, from opening to closing. The
    # median, because one case left over a weekend would drag a mean around.
    resolution = _rows(
        conn,
        """
        SELECT count(*) AS closed_7d,
               percentile_disc(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (closed_at - opened_at)) / 60.0) AS median_minutes,
               round(avg(EXTRACT(EPOCH FROM (closed_at - opened_at)) / 60.0)) AS mean_minutes
          FROM cases
         WHERE state = 'CLOSED' AND closed_at > now() - interval '7 days'
        """,
    )[0]
    if resolution["median_minutes"] is not None:
        resolution["median_minutes"] = round(float(resolution["median_minutes"]))

    return {
        "backlog": backlog,
        "ageing": ageing,
        "analysts": analysts,
        "outcomes": outcomes,
        "false_positive_rate": round(false_positives / decided, 3),
        "decided": decided,
        "sla_minutes": triage.SLA_MINUTES,
        "resolution": resolution,
    }

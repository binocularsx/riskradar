"""A customer reports fraud (D90).

Plain English
-------------
When a customer rings the contact centre to say "I did not make these
payments", three things must happen at once, and until now only the first did:

1. **The regulator's clocks start** (D71): acknowledge, notify the receiving
   bank, conclude, refund. The report's time is what they count from.
2. **The desk hears about it.** A ``case_reported`` event goes to the stream,
   so the analyst holding the case and the leads see it immediately rather
   than the next time they happen to reload the worklist.
3. **It goes to the top of the queue.** A customer telling us it was fraud is
   the strongest signal the desk ever gets, and money can only be recalled for
   a few hours (see ``triage.priority_score``).

A report may name payments Risk Radar never alerted on. Those are the frauds
the detector missed, and until now they had no way in. Each such payment gets
an alert raised by the report (``alerts.source = 'CUSTOMER_REPORT'``), so it
joins or opens the customer's case like any other alert, its timeline and
clocks work, and the outcome an analyst records becomes a training label for
exactly the kind of fraud the model needs to learn (D89). Counted against the
alert budget as mandatory: a person must work it today, whatever the budget.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..audit import chain
from ..clocks import sweep as clock_sweep
from ..events import publish
from ..policy import budget
from . import assignment, correlation


class ReportError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def start_clocks(conn: Any, *, case_id: int, user: dict[str, Any], reported_at: datetime, channel: str,
                 counterparty_institution: str | None, note: str | None,
                 customer_found: list[str] | None = None) -> dict[str, Any]:
    """Record the first report on a case, start its clocks, and tell the desk. Once only."""
    case = _rows(conn, "SELECT * FROM cases WHERE id = %s FOR UPDATE", (case_id,))
    if not case:
        raise ReportError(404, "case not found")
    case = case[0]
    if case["first_reported_at"]:
        raise ReportError(409, "the customer's first report is already recorded")
    now = _rows(conn, "SELECT now() AS ts")[0]["ts"]
    if reported_at > now:
        raise ReportError(400, "a report cannot be in the future")
    policy = clock_sweep.active_policy(conn)
    if not policy:
        raise ReportError(503, "no active clock policy")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cases SET first_reported_at = %s, report_channel = %s,
                   counterparty_institution = COALESCE(%s, counterparty_institution),
                   clock_policy_version = %s, handling = 'HUMAN'
             WHERE id = %s
            """,
            (reported_at, channel, counterparty_institution, policy["version"], case_id),
        )
        if note:
            cur.execute("INSERT INTO case_notes (case_id, author_id, body) VALUES (%s, %s, %s)",
                        (case_id, user["id"], note))
    chain.append(
        conn, actor_user_id=user["id"], action="CUSTOMER_REPORT_RECORDED", object_type="case", object_id=case_id,
        payload={"reported_at": reported_at.isoformat(), "channel": channel,
                 "counterparty_institution": counterparty_institution, "clock_policy_version": policy["version"],
                 "missed_by_detector": customer_found or []},
    )
    publish(conn, "case_reported", {
        "case_id": case_id, "assignee_id": case["assignee_id"], "risk_level": case["risk_level"],
        "channel": channel, "missed_by_detector": len(customer_found or []),
    })
    return {"case_id": case_id}


def report_payments(conn: Any, *, user: dict[str, Any], sys_uid: int, transaction_refs: list[str],
                    reported_at: datetime, channel: str, counterparty_institution: str | None,
                    note: str | None) -> dict[str, Any]:
    """A report naming payments. Opens or joins the customer's case, raising alerts for any missed."""
    txs = _rows(
        conn,
        """
        SELECT t.id, t.transaction_ref, t.subject_token, t.occurred_at, t.direction,
               d.id AS decision_id, d.score_0_100, d.risk_level::text AS risk_level,
               a.id AS alert_id, a.case_id
          FROM transactions t
          LEFT JOIN decisions d ON d.transaction_id = t.id
          LEFT JOIN alerts a ON a.transaction_id = t.id
         WHERE t.transaction_ref = ANY(%s)
        """,
        (transaction_refs,),
    )
    found = {t["transaction_ref"] for t in txs}
    unknown = [r for r in transaction_refs if r not in found]
    if unknown:
        raise ReportError(404, f"no such transaction: {', '.join(unknown[:5])}")
    subjects = {t["subject_token"] for t in txs}
    if len(subjects) > 1:
        raise ReportError(400, "a report is one customer's; these payments belong to more than one")
    unscored = [t["transaction_ref"] for t in txs if t["decision_id"] is None]
    if unscored:
        raise ReportError(409, f"not scored yet, retry in a moment: {', '.join(unscored[:5])}")

    subject = subjects.pop()
    missed = [t for t in txs if t["alert_id"] is None]
    case_ids = {t["case_id"] for t in txs if t["case_id"] is not None}
    raised: list[dict[str, Any]] = []
    for t in sorted(missed, key=lambda x: x["occurred_at"]):
        # Mandatory for the budget: counted, never deferred (D86).
        budget.admit(conn, mandatory=True, machine=False)
        case_id, created = correlation.attach(conn, subject_token=subject, risk_level="CRITICAL",
                                              at=t["occurred_at"])
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (decision_id, transaction_id, case_id, subject_token, risk_level,
                                    score_0_100, source)
                VALUES (%s, %s, %s, %s, 'CRITICAL', %s, 'CUSTOMER_REPORT')
                RETURNING id
                """,
                (t["decision_id"], t["id"], case_id, subject, int(t["score_0_100"] or 0)),
            )
            alert_id = int(cur.fetchone()["id"])
            cur.execute("UPDATE cases SET handling = 'HUMAN' WHERE id = %s", (case_id,))
        chain.append(
            conn, actor_user_id=user["id"], action="ALERT_RAISED", object_type="alert", object_id=alert_id,
            to_state="CRITICAL",
            payload={"source": "CUSTOMER_REPORT", "case_id": case_id, "case_created": created,
                     "decision_id": t["decision_id"], "transaction_ref": t["transaction_ref"],
                     "detector_risk_level": t["risk_level"], "detector_score": t["score_0_100"]},
        )
        publish(conn, "alert", {"alert_id": alert_id, "case_id": case_id, "risk_level": "CRITICAL",
                                "score": int(t["score_0_100"] or 0), "source": "CUSTOMER_REPORT"})
        # D94: a customer's report opens a case like any other, and it is
        # routed — including when it joined a case that was machine-handled
        # until this report made it human work a moment ago. route() leaves a
        # case that already has an owner alone.
        assignment.route(conn, case_id, sys_uid=sys_uid)
        case_ids.add(case_id)
        raised.append({"transaction_ref": t["transaction_ref"], "alert_id": alert_id, "case_id": case_id,
                       "detector_risk_level": t["risk_level"]})

    # The report starts the clocks on each case it touches that has not been reported yet.
    started = []
    for case_id in sorted(case_ids):
        already = _rows(conn, "SELECT first_reported_at FROM cases WHERE id = %s", (case_id,))[0]
        if already["first_reported_at"] is None:
            start_clocks(conn, case_id=case_id, user=user, reported_at=reported_at, channel=channel,
                         counterparty_institution=counterparty_institution, note=note,
                         customer_found=[r["transaction_ref"] for r in raised if r["case_id"] == case_id])
            started.append(case_id)
    return {"cases": sorted(case_ids), "clocks_started": started, "missed_by_detector": raised,
            "already_alerted": [t["transaction_ref"] for t in txs if t["alert_id"] is not None]}

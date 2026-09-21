"""An analyst proposes a fraud finding; a different lead decides it (D93).

Plain English
-------------
A confirmed fraud finding is two things at once: the bank's record that a
customer was defrauded, and the label the model will learn from (D13c). One
person should not be able to create both alone, and — since a finding can now
ask for a customer's account to be restricted — one person should not be able
to start a customer-impacting action alone either.

So the analyst's verdict is a **submission**: what they propose, why, the
evidence they read, the accounts and restrictions they are asking for, and the
version of the case they read it at. A lead who is **not** the submitter then
approves, rejects, or returns it with instructions. Only an approval writes the
outcome onto the case.

Four rules, each enforced here rather than by convention:

1. **Maker is never checker.** The database refuses a decision by the
   submitter (``decider_is_not_submitter``), and so does this module, so the
   rule holds through the API, a direct call, or a replayed request.
2. **One pending proposal per case.** A second submission supersedes the first;
   a lead is never handed two live versions of the same question.
3. **The version is quoted.** A submission names the case version it read, and
   an approval names the submission version it read. If the case moved —
   another alert, an escalation, a returned finding — the stale one is refused
   and the person is told what changed.
4. **Nothing is deleted.** A rejection, a return and a supersede are all
   recorded states of the original proposal, not edits to it.
"""

from __future__ import annotations

import json
from typing import Any

from ..audit import chain
from ..events import publish

PENDING, APPROVED, REJECTED, RETURNED, SUPERSEDED = "PENDING", "APPROVED", "REJECTED", "RETURNED", "SUPERSEDED"
DECISIONS = {"APPROVE": APPROVED, "REJECT": REJECTED, "RETURN": RETURNED}

# What a submission may ask the bank to do. Free text is not an action (plan §5.1).
RESTRICTION_ACTIONS = {
    "DEBIT_RESTRICTION": "Stop money leaving the account",
    "CHANNEL_RESTRICTION": "Stop one channel (USSD, mobile app, web, POS, ATM, agent)",
    "CARD_FREEZE": "Freeze the card",
    "BENEFICIARY_RESTRICTION": "Stop payments to one destination",
}


class SubmissionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _case(conn: Any, case_id: int, *, lock: bool = False) -> dict[str, Any]:
    rows = _rows(conn, f"SELECT * FROM cases WHERE id = %s{' FOR UPDATE' if lock else ''}", (case_id,))
    if not rows:
        raise SubmissionError(404, "case not found")
    return rows[0]


def bump_version(conn: Any, case_id: int) -> int:
    """Every material change to a case moves its version (plan §2.3)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET version = version + 1 WHERE id = %s RETURNING version", (case_id,))
        return int(dict(cur.fetchone())["version"])


def evidence_snapshot(conn: Any, case_id: int) -> dict[str, Any]:
    """What the analyst is deciding on: the alerts, and the decisions behind them."""
    rows = _rows(
        conn,
        """
        SELECT a.id AS alert_id, a.decision_id, a.risk_level::text AS risk_level, a.score_0_100, a.source,
               t.transaction_ref, t.amount_minor, t.currency,
               d.model_version_id, d.ruleset_id, d.threshold_set_id, d.feature_spec_version
          FROM alerts a
          JOIN transactions t ON t.id = a.transaction_id
          JOIN decisions d ON d.id = a.decision_id
         WHERE a.case_id = %s
         ORDER BY a.raised_at
        """,
        (case_id,),
    )
    notes = _rows(conn, "SELECT count(*) AS n FROM case_notes WHERE case_id = %s", (case_id,))[0]["n"]
    return {"alerts": rows, "alert_count": len(rows), "note_count": int(notes)}


def validate_restrictions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        action = (item.get("action") or "").upper()
        if action not in RESTRICTION_ACTIONS:
            raise SubmissionError(422, f"unknown restriction action {action!r}; allowed: "
                                       f"{', '.join(sorted(RESTRICTION_ACTIONS))}")
        if not item.get("account_token") and not item.get("beneficiary_token"):
            raise SubmissionError(422, "a restriction must name the account or the destination it applies to")
        out.append({
            "action": action,
            "account_token": item.get("account_token"),
            "beneficiary_token": item.get("beneficiary_token"),
            "channel": item.get("channel"),
            "reason": item.get("reason"),
        })
    return out


def submit(conn: Any, *, case_id: int, user: dict[str, Any], proposed_outcome: str, rationale: str,
           restrictions: list[dict[str, Any]], expected_version: int | None) -> dict[str, Any]:
    """Propose an outcome. The case's own analyst does this; it decides nothing."""
    case = _case(conn, case_id, lock=True)
    if case["state"] == "CLOSED":
        raise SubmissionError(409, "the case is closed")
    if expected_version is not None and int(case["version"]) != expected_version:
        raise SubmissionError(409, f"the case has moved on: it is at version {case['version']}, "
                                   f"you read version {expected_version}. Reload and look again.")
    if case["assignee_id"] not in (None, user["id"]) and "cases:reassign" not in user.get("permissions", []):
        # A lead holds the desk and may act on any case; an analyst works their own.
        raise SubmissionError(403, "this case belongs to someone else; a lead can reassign it")
    wanted = validate_restrictions(restrictions)
    if wanted and proposed_outcome != "CONFIRMED_FRAUD":
        raise SubmissionError(422, "restrictions may only be asked for with a confirmed-fraud proposal")

    superseded = _rows(
        conn,
        "UPDATE fraud_submissions SET state = 'SUPERSEDED' WHERE case_id = %s AND state = 'PENDING' RETURNING id",
        (case_id,),
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fraud_submissions
                (case_id, proposed_outcome, rationale, evidence, restrictions, case_version, submitted_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (case_id, proposed_outcome, rationale, json.dumps(evidence_snapshot(conn, case_id), default=str),
             json.dumps(wanted), int(case["version"]), user["id"]),
        )
        submission = dict(cur.fetchone())
    # The case leaves the analyst's queue and joins the approval queue; it is not
    # finished, and the SLA and the clocks keep running (plan §4.2).
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET state = 'UNDER_REVIEW' WHERE id = %s AND state = 'OPEN'", (case_id,))
    version = bump_version(conn, case_id)
    chain.append(
        conn, actor_user_id=user["id"], action="FRAUD_SUBMITTED", object_type="case", object_id=case_id,
        to_state=proposed_outcome,
        payload={"submission_id": submission["id"], "restrictions": wanted, "case_version": version,
                 "superseded": [r["id"] for r in superseded], "rationale": rationale[:500]},
    )
    publish(conn, "fraud_submitted", {"case_id": case_id, "submission_id": submission["id"],
                                      "proposed_outcome": proposed_outcome, "submitted_by": user["id"],
                                      "restrictions": len(wanted)})
    submission["case_version"] = version
    return submission


def pending(conn: Any, case_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM fraud_submissions WHERE case_id = %s AND state = 'PENDING'", (case_id,))
    return rows[0] if rows else None


def decide(conn: Any, *, submission_id: int, user: dict[str, Any], decision: str, reason: str) -> dict[str, Any]:
    """A lead approves, rejects or returns. Only an approval writes the outcome."""
    if decision not in DECISIONS:
        raise SubmissionError(422, f"decision must be one of {', '.join(sorted(DECISIONS))}")
    rows = _rows(conn, "SELECT * FROM fraud_submissions WHERE id = %s FOR UPDATE", (submission_id,))
    if not rows:
        raise SubmissionError(404, "submission not found")
    submission = rows[0]
    if submission["state"] != PENDING:
        raise SubmissionError(409, f"this submission is already {submission['state'].lower()}")
    if submission["submitted_by"] == user["id"]:
        # The database refuses this too; refusing here as well means the person
        # is told why rather than seeing a constraint error.
        raise SubmissionError(403, "the person who proposed a fraud finding cannot approve it. "
                                   "Separation of duties (D12b) is the control, not a formality.")
    case = _case(conn, submission["case_id"], lock=True)
    state = DECISIONS[decision]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fraud_submissions SET state = %s, decided_by = %s, decided_at = now(), decision_reason = %s "
            "WHERE id = %s RETURNING *",
            (state, user["id"], reason, submission_id),
        )
        submission = dict(cur.fetchone())

    promoted: list[str] = []
    if state == APPROVED:
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET outcome = %s WHERE id = %s",
                        (submission["proposed_outcome"], case["id"]))
            if submission["proposed_outcome"] == "CONFIRMED_FRAUD":
                # The destinations of confirmed fraud become a deterministic veto
                # for everyone else (D11a), now on a lead's authority.
                cur.execute(
                    """
                    INSERT INTO beneficiary_lists (kind, token, note, added_by)
                    SELECT DISTINCT 'KNOWN_MULE'::beneficiary_list_kind, t.beneficiary_token,
                           'approved fraud on case ' || %s, %s
                      FROM alerts a JOIN transactions t ON t.id = a.transaction_id
                     WHERE a.case_id = %s AND t.beneficiary_token IS NOT NULL
                    ON CONFLICT DO NOTHING
                    RETURNING token
                    """,
                    (case["id"], user["id"], case["id"]),
                )
                promoted = [dict(r)["token"] for r in cur.fetchall()]
    elif state == RETURNED:
        # Back to the analyst who proposed it, with instructions.
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET assignee_id = %s, assigned_at = now(), "
                        "assignment_reason = 'returned by a lead for more work' WHERE id = %s",
                        (submission["submitted_by"], case["id"]))
    version = bump_version(conn, case["id"])
    chain.append(
        conn, actor_user_id=user["id"], action=f"FRAUD_{state}", object_type="case", object_id=case["id"],
        from_state=PENDING, to_state=state,
        payload={"submission_id": submission_id, "submitted_by": submission["submitted_by"],
                 "proposed_outcome": submission["proposed_outcome"], "reason": reason,
                 "known_mule_tokens_added": promoted, "case_version": version,
                 "restrictions": submission["restrictions"]},
    )
    publish(conn, "fraud_decided", {"case_id": case["id"], "submission_id": submission_id, "decision": state,
                                    "decided_by": user["id"], "submitted_by": submission["submitted_by"]})
    return {"submission": submission, "known_mule_tokens_added": promoted, "case_version": version}


def closure_blockers(conn: Any, case_id: int) -> list[str]:
    """What stops this case closing (plan §4.3). Empty means it may close."""
    case = _case(conn, case_id)
    blockers: list[str] = []
    if case["outcome"] is None:
        blockers.append("no outcome: a lead must approve a submission first")
    if pending(conn, case_id):
        blockers.append("a fraud submission is still waiting for a decision")
    approved = _rows(
        conn,
        "SELECT id, restrictions FROM fraud_submissions WHERE case_id = %s AND state = 'APPROVED' "
        "ORDER BY decided_at DESC LIMIT 1",
        (case_id,),
    )
    if case["outcome"] == "CONFIRMED_FRAUD" and not approved:
        blockers.append("a confirmed-fraud outcome needs an approved submission behind it")
    return blockers

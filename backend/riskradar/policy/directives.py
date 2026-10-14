"""Directives: the decision in a form a bank can wire in (WP-07, D74).

Plain English
-------------
Risk Radar decides ALLOW, MONITOR, REVIEW or HOLD. A bank's payment switch does
not understand a risk band on a dashboard; it needs an instruction it can act
on in milliseconds, that says how long it stays valid, and that says what to do
when the answer is late. That instruction is a directive.

Three rules decide what the bank should actually do with one, and all three
favour letting the payment through:

* In **SHADOW** mode, nothing is enforced. The bank records what it would have
  done so the pilot can be measured (FR-305), and the payment proceeds.
* In **LIVE** mode, a directive is followed only while it is fresh. Past
  ``expires_at`` the written fail-open action applies instead, because acting on
  a stale answer is worse than acting on none.
* No directive, or no Risk Radar, is fail-open too. That is the bank's side of
  the contract and is written into the policy text it signs.

Risk Radar itself never blocks anything (D7). LIVE mode cannot even be
switched on without a signed policy: the database refuses it (D69a).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# D69a: the four decisions map onto the four things a switch can do.
ACTION_FOR_DECISION = {
    "ALLOW": "APPROVE",
    "MONITOR": "APPROVE_AND_MONITOR",
    "REVIEW": "HOLD_FOR_REVIEW",
    "HOLD": "DECLINE",
}

INSERT_FOR_DECISION_SQL = """
    INSERT INTO directives (decision_id, transaction_id, action, mode, policy_version,
                            fail_open_action, issued_at, expires_at)
    SELECT %(decision_id)s, %(transaction_id)s, %(action)s, p.mode, p.version,
           p.fail_open_action, now(), now() + make_interval(secs => p.ttl_seconds)
      FROM enforcement_policies p
     WHERE p.is_active
    ON CONFLICT (decision_id) DO NOTHING
"""


def issue(conn: Any, *, decision_id: int, transaction_id: int, decision: str) -> None:
    """Write the directive for a decision, in the decision's own transaction.

    One INSERT, no read: the active policy is joined in the statement. With no
    active policy nothing is written, which the bank sees as "no directive" and
    therefore fails open.
    """
    with conn.cursor() as cur:
        cur.execute(INSERT_FOR_DECISION_SQL, {
            "decision_id": decision_id,
            "transaction_id": transaction_id,
            "action": ACTION_FOR_DECISION[decision],
        })


def effective(directive: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What the bank should do with this directive at ``now``. Pure."""
    expired = now >= directive["expires_at"]
    enforce = directive["mode"] == "LIVE" and not expired
    return {
        "expired": expired,
        "enforce": enforce,
        "effective_action": directive["action"] if enforce else directive["fail_open_action"],
        "seconds_left": max(0, int((directive["expires_at"] - now).total_seconds())),
    }


def as_contract(directive: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The directive as the bank receives it."""
    return {
        "directive_ref": str(directive["directive_ref"]),
        "transaction_ref": directive["transaction_ref"],
        "action": directive["action"],
        "mode": directive["mode"],
        "policy_version": directive["policy_version"],
        "fail_open_action": directive["fail_open_action"],
        "issued_at": directive["issued_at"],
        "expires_at": directive["expires_at"],
        "decision": directive["decision"],
        "risk_level": directive["risk_level"],
        "reasons": [s.get("code") for s in (directive.get("signals") or []) if isinstance(s, dict)],
        "first_delivered_at": directive["first_delivered_at"],
        "acknowledged_at": directive["acknowledged_at"],
        "ack_action": directive["ack_action"],
        **effective(directive, now),
    }

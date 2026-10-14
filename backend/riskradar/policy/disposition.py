"""Dispositions: who acts on an alert, the system or a person (WP-08, D80).

Plain English
-------------
An alert used to mean "an analyst investigates". Some alerts do not need an
investigation to know what to do. A wall of declined card probes means block
the card; a phone bound, a PIN changed and a new payee paid within the day is a
takeover in progress, and CBN's answer to that is a 24-hour flag and a call to
the customer. Neither is improved by an analyst reading the timeline first.

So every actionable decision gets a disposition from the versioned policy:

* MACHINE_ACTION when a named high-precision signal fired. The system takes
  the action it names and a person confirms by contacting the customer.
* HUMAN_REVIEW otherwise.
* AUTO_CLOSE only when the policy enables it, and only for an alert the model
  raised alone, at or below the policy's probability cut. Off in version 1.

A deterministic veto (sanctions, a known mule) is never a machine action: a
person owns that outcome, whatever the policy lists. Risk Radar still enforces
nothing itself (D7): the "action" is a directive and a flag the bank applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..rules.engine import Signal

NONE = "NONE"
HUMAN_REVIEW = "HUMAN_REVIEW"
MACHINE_ACTION = "MACHINE_ACTION"
AUTO_CLOSE = "AUTO_CLOSE"


@dataclass(frozen=True)
class DispositionPolicy:
    version: int
    machine_action_signals: frozenset[str]
    auto_close_enabled: bool = False
    auto_close_max_probability: float | None = None
    review_capacity_per_day: int = 75

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DispositionPolicy:
        cut = row.get("auto_close_max_probability")
        return cls(
            version=int(row["version"]),
            machine_action_signals=frozenset(row["machine_action_signals"] or ()),
            auto_close_enabled=bool(row["auto_close_enabled"]),
            auto_close_max_probability=float(cut) if cut is not None else None,
            review_capacity_per_day=int(row["review_capacity_per_day"]),
        )


def choose(*, actionable: bool, signals: list[Signal], p_fraud: float, model_applies: bool,
           policy: DispositionPolicy | None) -> str:
    """The disposition for one decision. Pure."""
    if not actionable:
        return NONE
    if policy is None:
        return HUMAN_REVIEW
    codes = {s.code for s in signals}
    if any(s.power == "OVERRIDE" for s in signals):
        return HUMAN_REVIEW
    if codes & policy.machine_action_signals:
        return MACHINE_ACTION
    raised_by_rule = any(s.power == "ESCALATE" for s in signals)
    if (policy.auto_close_enabled and model_applies and not raised_by_rule
            and policy.auto_close_max_probability is not None and p_fraud <= policy.auto_close_max_probability):
        return AUTO_CLOSE
    return HUMAN_REVIEW


def active_policy(conn: Any) -> DispositionPolicy | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, machine_action_signals, auto_close_enabled, auto_close_max_probability, "
            "review_capacity_per_day FROM disposition_policies WHERE is_active LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return None
    return DispositionPolicy.from_row(dict(row) if not isinstance(row, dict) else row)

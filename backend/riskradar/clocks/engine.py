"""The regulator's clocks, evaluated against a case (WP-05, REG-NG-05, REG-NG-06).

Plain English
-------------
When a customer reports that they were tricked into sending money, the CBN's
framework for authorised push payment fraud starts a set of clocks on the bank:
acknowledge the report, tell the receiving bank, finish investigating, pay the
customer back. Our own per-severity clocks (``cases/triage.py``) say how fast
an analyst should *look*; these say what the bank *owes*, and by when.

This file is pure logic. It takes clock definitions (versioned configuration in
``clock_policies``, never constants here), the moments a case has recorded, a
working-day calendar and "now", and says for each clock: not started, running,
due soon, met, met late, breached, or not applicable.

A clock starts when its start event is recorded and stops when its end event
is. Nothing here writes anything; ``clocks/sweep.py`` acts on breaches.

The values in policy version 1 come from the CBN **exposure draft** of
26 November 2025. They are configuration precisely so that the final circular
can change them without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .calendar import Calendar

# The moments a case can record. ``fraud_first_at`` is derived (the earliest
# alerted payment); the rest are entered by the desk.
EVENTS = (
    "fraud_first_at",
    "first_reported_at",
    "acknowledged_at",
    "counterparty_notified_at",
    "investigation_concluded_at",
    "reimbursed_at",
)
UNITS = ("MINUTES", "HOURS", "WORKING_DAYS")
OWNERS = ("BANK", "CUSTOMER")
# Mirrors the per-severity clocks: the last quarter of the time is "due".
DUE_FRACTION = 0.25


@dataclass(frozen=True)
class ClockDef:
    code: str
    obligation: str
    owner: str
    starts: str
    ends: str
    amount: int
    unit: str
    escalate: bool = True
    # A refund is owed only when the investigation finds fraud (D71c).
    requires_outcome: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClockDef:
        clock = cls(
            code=d["code"], obligation=d["obligation"], owner=d["owner"],
            starts=d["starts"], ends=d["ends"], amount=int(d["amount"]), unit=d["unit"],
            escalate=bool(d.get("escalate", True)), requires_outcome=d.get("requires_outcome"),
        )
        clock.validate()
        return clock

    def validate(self) -> None:
        if self.starts not in EVENTS or self.ends not in EVENTS:
            raise ValueError(f"{self.code}: unknown event {self.starts!r} or {self.ends!r}")
        if self.starts == self.ends:
            raise ValueError(f"{self.code}: a clock cannot start and stop on the same event")
        if self.unit not in UNITS:
            raise ValueError(f"{self.code}: unit must be one of {UNITS}")
        if self.owner not in OWNERS:
            raise ValueError(f"{self.code}: owner must be one of {OWNERS}")
        if self.amount < 1:
            raise ValueError(f"{self.code}: amount must be positive")

    def due(self, start: datetime, calendar: Calendar) -> datetime:
        if self.unit == "MINUTES":
            return start + timedelta(minutes=self.amount)
        if self.unit == "HOURS":
            return start + timedelta(hours=self.amount)
        return calendar.add_working_days(start, self.amount)

    @property
    def label(self) -> str:
        unit = {"MINUTES": "minute", "HOURS": "hour", "WORKING_DAYS": "working day"}[self.unit]
        return f"{self.amount} {unit}{'' if self.amount == 1 else 's'}"


def parse_policy(definitions: list[dict[str, Any]]) -> list[ClockDef]:
    clocks = [ClockDef.from_dict(d) for d in definitions]
    codes = [c.code for c in clocks]
    if len(set(codes)) != len(codes):
        raise ValueError("clock codes must be unique within a policy")
    return clocks


def evaluate(
    clocks: list[ClockDef],
    events: dict[str, datetime | None],
    *,
    outcome: str | None,
    now: datetime,
    calendar: Calendar,
) -> list[dict[str, Any]]:
    """Every clock's state for one case. Empty when the customer has not reported.

    A case nobody reported is a detection, not a complaint: the regulator's
    clocks start with the customer's report, so without one there is nothing
    to owe.
    """
    if not events.get("first_reported_at"):
        return []

    concluded = events.get("investigation_concluded_at") is not None
    out = []
    for clock in clocks:
        start, end = events.get(clock.starts), events.get(clock.ends)
        row: dict[str, Any] = {
            "code": clock.code,
            "obligation": clock.obligation,
            "owner": clock.owner,
            "limit": clock.label,
            "escalate": clock.escalate,
            "started_at": start,
            "due_at": None,
            "met_at": end,
            "remaining_minutes": None,
            "calendar_complete": True,
            "estimated_holidays": [],
        }

        if clock.requires_outcome and concluded and outcome != clock.requires_outcome:
            row["state"] = "NOT_APPLICABLE"
            out.append(row)
            continue
        if start is None:
            row["state"] = "NOT_STARTED"
            out.append(row)
            continue

        due = clock.due(start, calendar)
        row["due_at"] = due
        if clock.unit == "WORKING_DAYS":
            row["calendar_complete"] = calendar.covers(start, due)
            row["estimated_holidays"] = [
                h.name for h in calendar.holidays_between(start, due) if not h.confirmed
            ]

        if end is not None:
            row["state"] = "MET" if end <= due else "MET_LATE"
        else:
            remaining = (due - now).total_seconds() / 60.0
            row["remaining_minutes"] = int(remaining)
            total = (due - start).total_seconds() / 60.0
            if remaining < 0:
                row["state"] = "BREACHED"
            elif remaining <= total * DUE_FRACTION:
                row["state"] = "DUE"
            else:
                row["state"] = "RUNNING"
        out.append(row)
    return out


def most_urgent(states: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bank clock the desk should look at first: breached, then soonest due."""
    live = [s for s in states if s["owner"] == "BANK" and s["state"] in ("BREACHED", "DUE", "RUNNING")]
    if not live:
        return None
    return min(live, key=lambda s: s["remaining_minutes"])

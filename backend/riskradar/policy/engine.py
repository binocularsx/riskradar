"""The policy layer (D11, D11b).

Plain English
-------------
This is the only file in the system that actually decides anything.

It receives two things: a probability from the model ("I think there is a 28%
chance this is fraud") and a list of flags from the rules ("this account made 7
transfers in an hour"). It combines them into one verdict — LOW, MEDIUM, HIGH
or CRITICAL, and a recommendation of ALLOW, MONITOR, REVIEW or HOLD.

The order matters and is deliberate: start from what the model thinks, let
rules push it up, let suppressions pull it back down, and let a veto rule
overrule everything. Every one of those steps is written into a list that
ships with the decision, so when an analyst asks "why is this HIGH?" the answer
is read from data rather than guessed at.

An explicit, readable, versioned function mapping ``(P(fraud), signals, context)``
to a decision and a risk level. This is the third layer, and the only one that
decides anything: the model estimates, the rules describe, the policy decides.

Every step is recorded in a trace that ships with the decision record, so
"why is this HIGH?" is answered by reading data rather than by re-running code.

Order of application, and why it is this order:

1. **Band from the calibrated probability.** The thresholds are derived backwards
   from the alert budget (D11d) — 75 alerts/day for a three-analyst desk (D76) — never
   from "80 sounds high".
2. **Escalations raise the band.** One level each.
3. **Suppressions lower it.** *After* escalation, deliberately: suppression is
   the primary false-positive control (D11a), and a control that cannot cancel an
   escalation is not a control. Paying your own landlord six times in an hour is
   unusual and not suspicious.
4. **Overrides win absolutely.** Straight to CRITICAL, model gets no vote, and
   no suppression can pull it back. Some things are not probabilistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..rules.engine import Signal

LEVELS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# D7: the typed advisory decision. Risk Radar produces it; the caller enforces
# it, or does not. We are not in the money path.
LEVEL_TO_DECISION = {
    "LOW": "ALLOW",
    "MEDIUM": "MONITOR",
    "HIGH": "REVIEW",
    "CRITICAL": "HOLD",
}


@dataclass
class Thresholds:
    """One row of ``threshold_sets``. Versioned; the decision records which applied."""

    id: int
    version: int
    p_monitor: float
    p_review: float
    p_hold: float
    alert_min_level: str = "MEDIUM"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Thresholds":
        def f(v: Any) -> float:
            return float(v) if not isinstance(v, Decimal) else float(v)

        return cls(
            id=row["id"],
            version=row["version"],
            p_monitor=f(row["p_monitor"]),
            p_review=f(row["p_review"]),
            p_hold=f(row["p_hold"]),
            alert_min_level=row.get("alert_min_level", "MEDIUM"),
        )


@dataclass
class PolicyResult:
    p_fraud: float
    score_0_100: int
    risk_level: str
    decision: str
    actionable: bool
    signals: list[Signal] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)


def _index(level: str) -> int:
    return LEVELS.index(level)


def _shift(level: str, by: int) -> str:
    return LEVELS[max(0, min(len(LEVELS) - 1, _index(level) + by))]


def base_level(p_fraud: float, t: Thresholds) -> str:
    if p_fraud >= t.p_hold:
        return "CRITICAL"
    if p_fraud >= t.p_review:
        return "HIGH"
    if p_fraud >= t.p_monitor:
        return "MEDIUM"
    return "LOW"


def presentation_score(p_fraud: float) -> int:
    """The 0-100 number (D11b).

    A **presentation artefact for sorting**, derived from the calibrated
    probability. It is not a decision input, nothing downstream may threshold on
    it, and it exists so an analyst can order a queue by eye.
    """
    return int(round(max(0.0, min(1.0, p_fraud)) * 100))


def apply(
    p_fraud: float,
    signals: list[Signal],
    thresholds: Thresholds,
    *,
    rule_only_mode: bool = False,
) -> PolicyResult:
    """Combine a probability and a set of facts into one decision."""
    trace: list[dict[str, Any]] = []

    if rule_only_mode:
        # FR-017 / D15d: the model is unavailable. We do not invent a probability;
        # we say so, start from LOW, and let the deterministic layer carry the
        # weight. The alarm is raised by the worker, not silently here.
        level = "LOW"
        trace.append(
            {
                "step": "base",
                "source": "rule_only_mode",
                "level": level,
                "note": "model unavailable; probability not used",
            }
        )
    else:
        level = base_level(p_fraud, thresholds)
        trace.append(
            {
                "step": "base",
                "source": "model",
                "p_fraud": round(p_fraud, 6),
                "thresholds": {
                    "p_monitor": thresholds.p_monitor,
                    "p_review": thresholds.p_review,
                    "p_hold": thresholds.p_hold,
                },
                "level": level,
            }
        )

    escalations = [s for s in signals if s.power == "ESCALATE"]
    suppressions = [s for s in signals if s.power == "SUPPRESS"]
    overrides = [s for s in signals if s.power == "OVERRIDE"]

    for signal in escalations:
        before = level
        level = _shift(level, +1)
        trace.append(
            {
                "step": "escalate",
                "code": signal.code,
                "severity": signal.severity,
                "from": before,
                "to": level,
                "evidence": signal.evidence,
            }
        )

    for signal in suppressions:
        before = level
        level = _shift(level, -1)
        trace.append(
            {
                "step": "suppress",
                "code": signal.code,
                "from": before,
                "to": level,
                "evidence": signal.evidence,
            }
        )

    for signal in overrides:
        before = level
        level = "CRITICAL"
        trace.append(
            {
                "step": "override",
                "code": signal.code,
                "from": before,
                "to": level,
                "evidence": signal.evidence,
                "note": "deterministic veto; model and suppressions do not apply",
            }
        )

    decision = LEVEL_TO_DECISION[level]
    actionable = _index(level) >= _index(thresholds.alert_min_level)
    trace.append(
        {
            "step": "final",
            "level": level,
            "decision": decision,
            "actionable": actionable,
            "alert_min_level": thresholds.alert_min_level,
        }
    )

    return PolicyResult(
        p_fraud=p_fraud,
        score_0_100=presentation_score(p_fraud),
        risk_level=level,
        decision=decision,
        actionable=actionable,
        signals=signals,
        trace=trace,
    )

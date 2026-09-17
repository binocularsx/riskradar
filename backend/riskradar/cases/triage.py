"""Triage: exposure, urgency, and what the analyst should probably do.

Plain English
-------------
A list of cases is not a fraud desk. An analyst does not want to browse — they
want the next thing that matters, with enough context to decide in under a
minute, and a button to record what they decided.

This file supplies the three things a list cannot:

* **Exposure** — how much money is actually at risk. A case worth 4.2 million
  naira and a case worth 900 naira are not the same job, and sorting by "risk
  level" alone treats them identically.
* **Urgency** — how long the case has been sitting against how long it should
  have taken. Fraud is time-critical; money leaves.
* **A recommendation** — what the evidence suggests doing, in a sentence, with
  its reasons. The analyst confirms or overrides it.

The recommendation is *advice to a human*, which keeps it inside D7: Risk Radar
still never acts. It is also deliberately rule-based rather than modelled, so it
can always be explained in the words that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# How long a case of each severity should wait before somebody looks at it.
# Fraud desks work to clocks; a queue with no clock silently ages its worst
# cases to the bottom. Stored here rather than in the database because it is
# operational policy, not detection tuning — changing it changes nobody's score.
SLA_MINUTES = {
    "CRITICAL": 15,
    "HIGH": 30,
    "MEDIUM": 240,
    "LOW": 1440,
}

SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class Recommendation:
    """What to do, why, and how strongly.

    ``action`` is what the analyst would actually do next; ``because`` is the
    evidence in plain words. Both are shown together — an unexplained
    recommendation is exactly the "bare score" problem this project exists to
    avoid, moved up a layer.
    """

    action: str
    because: str
    urgency: str  # now | soon | routine
    disposition_hint: str | None = None  # the outcome this evidence points at

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "because": self.because,
            "urgency": self.urgency,
            "disposition_hint": self.disposition_hint,
        }


def sla_state(age_minutes: float, risk_level: str) -> tuple[str, int]:
    """Returns (state, minutes_remaining). Negative minutes means overdue."""
    budget = SLA_MINUTES.get(risk_level, 240)
    remaining = int(budget - age_minutes)
    if remaining < 0:
        return "BREACHED", remaining
    if remaining <= budget * 0.25:
        return "DUE", remaining
    return "OK", remaining


def recommend(
    *,
    risk_level: str,
    signals: list[str],
    exposure_minor: int,
    declined_count: int,
    alert_count: int,
    distinct_beneficiaries: int,
    new_device: bool,
) -> Recommendation:
    """Turn the evidence into a sentence an analyst can act on.

    Ordered most-serious-first, and the first match wins. Each branch names the
    specific evidence rather than the score, because "score 87" tells an analyst
    nothing about what to do next.
    """
    naira = exposure_minor / 100.0

    # A deterministic veto fired. Nothing about this is probabilistic.
    if "SANCTIONED_BENEFICIARY" in signals:
        return Recommendation(
            action="Escalate to InfoSec and hold the beneficiary",
            because="The destination is on the sanctions list. This is not a "
                    "probability — it is a match.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )
    if "KNOWN_MULE_BENEFICIARY" in signals:
        return Recommendation(
            action="Confirm fraud and recall the funds",
            because="The destination was confirmed fraudulent on an earlier case, "
                    "so this is a repeat to a known mule account.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )

    # Card testing: the card is compromised, and the card is the thing to kill.
    if "CARD_TESTING_PROBES" in signals:
        return Recommendation(
            action="Block the card and reissue",
            because=f"{declined_count} declined low-value authorisations in a short "
                    "window — the pattern of somebody testing a stolen card before "
                    "using it.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )

    # D82: the four added fraud types, each with the step that saves the money.
    if "SIM_SWAP_TRANSFER" in signals:
        return Recommendation(
            action="Hold further transfers and reach the customer on a second channel",
            because=f"₦{naira:,.0f} left for a new destination hours after the customer's SIM "
                    "changed. Whoever holds the new SIM receives the OTPs, so do not call the "
                    "number on file: use email, the branch or a registered alternative number.",
            urgency="now",
        )
    if "CARD_PRESENT_NEW_REGION_CASHOUT" in signals:
        return Recommendation(
            action="Block the card and confirm where the customer is",
            because=f"The card was used at terminal after terminal ({declined_count} declined) in a "
                    "region this customer has not used this month. A cloned card is cashed out "
                    "like this before the owner notices; a traveller rarely is.",
            urgency="now",
        )
    if "SCAM_BENEFICIARY_FANIN" in signals:
        return Recommendation(
            action="Call the customer before paying out, and ask who asked them to pay",
            because=f"₦{naira:,.0f} went to a new account that several other customers also paid "
                    "today. That is how a scam's collection account looks. The customer made the "
                    "payment themselves, so ask about the call or message that prompted it, then "
                    "notify the receiving bank to place a hold.",
            urgency="now",
        )
    if "DORMANT_ACCOUNT_REACTIVATION" in signals:
        return Recommendation(
            action="Verify the owner in person or by video before releasing more",
            because=f"An account dormant for months moved ₦{naira:,.0f} to a new destination. "
                    "Check whether its phone number or email was changed recently, and by whom.",
            urgency="now",
        )

    # Account takeover shape: new device plus rapid outbound movement.
    if new_device and "VELOCITY_BURST_1H" in signals:
        return Recommendation(
            action="Call the customer before any further transfer clears",
            because=f"A device never seen on this customer moved ₦{naira:,.0f} across "
                    f"{alert_count} transactions to {distinct_beneficiaries} "
                    "destinations. That is the shape of a taken-over account.",
            urgency="now",
            disposition_hint=None,
        )

    # Fan-out: one account, many fresh destinations, fast.
    if distinct_beneficiaries >= 4 and "VELOCITY_BURST_1H" in signals:
        return Recommendation(
            action="Review the destination accounts for a mule network",
            because=f"₦{naira:,.0f} pushed to {distinct_beneficiaries} different "
                    "destinations in under an hour. Individually unremarkable; "
                    "together, a fan-out.",
            urgency="now",
            disposition_hint=None,
        )

    # Suppression fired — the system already thinks this is probably fine.
    if any(s in signals for s in ("PRE_REGISTERED_BENEFICIARY", "ESTABLISHED_PAYEE_NORMAL")):
        return Recommendation(
            action="Verify quickly, then clear",
            because="The destination is one this customer has paid before, and the "
                    "amount is inside their normal range. The suppression rules "
                    "already pulled this down a level.",
            urgency="routine",
            disposition_hint="FALSE_POSITIVE",
        )

    if risk_level == "CRITICAL":
        return Recommendation(
            action="Contact the customer to verify",
            because=f"₦{naira:,.0f} across {alert_count} transactions scored in the "
                    "top band with no suppressing evidence.",
            urgency="now",
        )
    if risk_level == "HIGH":
        return Recommendation(
            action="Review the timeline, then contact the customer if it continues",
            because=f"₦{naira:,.0f} at risk. Unusual for this account, but no single "
                    "rule fired hard enough to be conclusive.",
            urgency="soon",
        )

    return Recommendation(
        action="Monitor — no action needed unless it repeats",
        because=f"₦{naira:,.0f} at risk. Above the noise floor, below anything that "
                "warrants interrupting the customer.",
        urgency="routine",
    )


# Every term below severity shares a budget of 850 points, so no combination of
# money, lateness and alert count can ever push a case past a more severe one.
# That is a deliberate ordering: CRITICAL means a deterministic rule fired — a
# sanctioned destination, a known mule — and a ₦1,000 transfer to a sanctioned
# entity is a compliance incident that outranks a ₦5m case that merely looks
# unusual. Without the cap, a large enough amount silently crossed the band.
_MONEY_POINTS = 450     # log10(naira) x 50, capped
_LATE_POINTS = 300      # scales to two hours overdue
_ALERT_POINTS = 100     # ten alerts or more


def priority_score(
    *, risk_level: str, exposure_minor: int, sla_remaining: int, alert_count: int
) -> float:
    """The order the worklist is served in.

    Deliberately *not* the model's score. The model answers "how likely is this
    fraud"; a queue has to answer "what should be worked first", and those are
    different questions. A ₦4m case ten minutes from breaching its clock
    outranks a ₦9,000 case that scored slightly higher — but neither outranks a
    sanctions hit.
    """
    import math

    severity = SEVERITY_ORDER.get(risk_level, 0)

    # Money matters, but not linearly: ₦10m is not a thousand times more urgent
    # than ₦10k. A log keeps large cases on top without letting one whale bury
    # the rest of the day's work.
    naira = max(exposure_minor, 0) / 100.0
    money = min(math.log10(naira + 1) * 50, _MONEY_POINTS)

    overdue_minutes = max(0, -sla_remaining)
    late = min(overdue_minutes / 120.0, 1.0) * _LATE_POINTS

    alerts = min(alert_count / 10.0, 1.0) * _ALERT_POINTS

    return severity * 1000 + money + late + alerts

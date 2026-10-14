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
            action="Recommend support blocks payments to this recipient",
            because="The recipient is on the sanctions list. This is an exact match, "
                    "not a judgement call.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )
    if "KNOWN_MULE_BENEFICIARY" in signals:
        return Recommendation(
            action="Confirm fraud and recommend support asks the receiving bank to return the money",
            because="This recipient was confirmed as fraudulent on an earlier case. "
                    "Money is going to an account already known to be used for fraud.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )

    # Card testing: the card is compromised, and the card is the thing to kill.
    if "CARD_TESTING_PROBES" in signals:
        return Recommendation(
            action="Recommend support blocks the card and issues a new one",
            because=f"{declined_count} small card payments were declined in a short time. "
                    "That is what somebody does to test a stolen card before using it.",
            urgency="now",
            disposition_hint="CONFIRMED_FRAUD",
        )

    # D82: the four added fraud types, each with the step that saves the money.
    if "SIM_SWAP_TRANSFER" in signals:
        return Recommendation(
            action="Recommend support holds further transfers and contacts the customer another way, not on the phone number on file",
            because=f"₦{naira:,.0f} was sent to a new recipient hours after the customer's SIM "
                    "card was changed. Whoever holds the new SIM receives the OTPs, so support should not "
                    "call the number on file: email, the branch or a registered alternative number instead.",
            urgency="now",
        )
    if "CARD_PRESENT_NEW_REGION_CASHOUT" in signals:
        return Recommendation(
            action="Recommend support blocks the card and confirms where the customer is",
            because=f"The card was used at one card machine after another ({declined_count} declined) "
                    "in a part of the country this customer has not used this month. A copied card "
                    "is emptied like this before the owner notices; a traveller rarely behaves this way.",
            urgency="now",
        )
    if "SCAM_BENEFICIARY_FANIN" in signals:
        return Recommendation(
            action="Recommend support contacts the customer before paying out, and asks who asked them to pay",
            because=f"₦{naira:,.0f} went to a new account that several other customers also paid "
                    "today. Scammers collect money from many victims into one account like this. The customer made the "
                    "payment themselves, so support should ask about the call or message that prompted "
                    "it, and ask the receiving bank to place a hold.",
            urgency="now",
        )
    if "DORMANT_ACCOUNT_REACTIVATION" in signals:
        return Recommendation(
            action="Recommend support confirms it is really the owner, in person or by video call, before letting more money go",
            because=f"An account unused for months suddenly sent ₦{naira:,.0f} to a new recipient. "
                    "Check the customer's record for a recent change to the phone number or email, and how it was made.",
            urgency="now",
        )

    # Account takeover shape: new device plus rapid outbound movement.
    if new_device and "VELOCITY_BURST_1H" in signals:
        return Recommendation(
            action="Recommend support holds further transfers and calls the customer",
            because=f"A phone or computer this customer has never used before sent ₦{naira:,.0f} "
                    f"in {alert_count} payments to {distinct_beneficiaries} recipients. "
                    "That is what it looks like when someone else has taken over the account.",
            urgency="now",
            disposition_hint=None,
        )

    # Fan-out: one account, many fresh destinations, fast.
    if distinct_beneficiaries >= 4 and "VELOCITY_BURST_1H" in signals:
        return Recommendation(
            action="Check the recipient accounts: they may be mule accounts working together",
            because=f"₦{naira:,.0f} was split across {distinct_beneficiaries} different "
                    "recipients in under an hour. Each payment looks normal on its own; "
                    "together they look like money being spread out to hide it.",
            urgency="now",
            disposition_hint=None,
        )

    # Suppression fired — the system already thinks this is probably fine.
    if any(s in signals for s in ("PRE_REGISTERED_BENEFICIARY", "ESTABLISHED_PAYEE_NORMAL")):
        return Recommendation(
            action="Check quickly, then close as no fraud",
            because="The customer has paid this recipient before, and the amount is "
                    "normal for them. Risk Radar has already lowered the risk level "
                    "for that reason.",
            urgency="routine",
            disposition_hint="FALSE_POSITIVE",
        )

    if risk_level == "CRITICAL":
        return Recommendation(
            action="Send support a heads-up, then recommend they confirm the payments with the customer",
            because=f"₦{naira:,.0f} in {alert_count} payments is rated Critical, the highest "
                    "risk level, and nothing suggests it is normal for this customer "
                    "(no regular recipient, no usual amount).",
            urgency="now",
        )
    if risk_level == "HIGH":
        return Recommendation(
            action="Look through the account's recent activity; if it continues, recommend support calls the customer",
            because=f"₦{naira:,.0f} at risk. This is unusual for the account, but no single "
                    "warning sign is strong enough to be sure.",
            urgency="soon",
        )

    return Recommendation(
        action="Keep an eye on it — no action needed unless it happens again",
        because=f"₦{naira:,.0f} at risk. Slightly unusual, but not enough to "
                "involve support or the customer.",
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
    *, risk_level: str, exposure_minor: int, sla_remaining: int, alert_count: int, reported: bool = False
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
    # D90: a customer has told us this is fraud. That outranks anything the
    # detector merely suspects, a sanctions hit included: the money is leaving
    # or gone, recall works for hours not days, and the CBN clocks are running.
    # Within the band, money and lateness (the regulator's clock included)
    # still order reported cases against each other.
    if reported:
        severity = SEVERITY_ORDER["CRITICAL"] + 1

    # Money matters, but not linearly: ₦10m is not a thousand times more urgent
    # than ₦10k. A log keeps large cases on top without letting one whale bury
    # the rest of the day's work.
    naira = max(exposure_minor, 0) / 100.0
    money = min(math.log10(naira + 1) * 50, _MONEY_POINTS)

    overdue_minutes = max(0, -sla_remaining)
    late = min(overdue_minutes / 120.0, 1.0) * _LATE_POINTS

    alerts = min(alert_count / 10.0, 1.0) * _ALERT_POINTS

    return severity * 1000 + money + late + alerts

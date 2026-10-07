"""What an analyst can do about a case, and where a case is on its way to resolution (D83).

Plain English
-------------
Risk Radar recommends; people act, in the bank's own systems (D7). So a
recommendation is only useful if each of its steps is something the analyst can
do, and can say here that they did. This module is that list:

* **Steps** — for each recommended action, the steps in order, each tied to an
  action the analyst records with its result ("customer reached: denied making
  the transfers"). The dashboard shows which are done and which are left.
* **Escalation guidance** — when to send a case to InfoSec or to the Fraud Ops
  lead, and what happens when you do, in words.
* **Stages** — the path every case takes, so its position can be drawn and the
  whole desk can be counted stage by stage.

Nothing here decides anything. It turns the recommendation into work that can be
tracked, and the tracking into something a lead can manage.
"""

from __future__ import annotations

from typing import Any

# Every action an analyst can record, and the results that action can have.
ACTIONS: dict[str, dict[str, Any]] = {
    "TIMELINE_REVIEWED": {
        "label": "Reviewed the timeline",
        "results": {"CONSISTENT": "Looks like the customer's normal behaviour",
                    "UNUSUAL": "Not like this customer"},
    },
    "CUSTOMER_CONTACTED": {
        "label": "Contacted the customer",
        "results": {"CONFIRMED_GENUINE": "Reached: they made these payments",
                    "DENIED": "Reached: they did not make them",
                    "SCAM_ADMITTED": "Reached: they paid because someone told them to",
                    "NOT_REACHED": "Could not reach them"},
        "hint": "Use the number on file, never one from the transaction. After a SIM swap, use email or the branch.",
    },
    "CARD_BLOCK_REQUESTED": {
        "label": "Card blocked",
        "results": {"DONE": "Blocked in the card system", "NOT_POSSIBLE": "Could not block"},
    },
    "HOLD_REQUESTED": {
        "label": "Further transfers held",
        "results": {"DONE": "Hold placed in core banking", "NOT_POSSIBLE": "Could not hold"},
    },
    "RECALL_REQUESTED": {
        "label": "Funds recall raised",
        "results": {"DONE": "Recall raised with the rail (NIP)", "NOT_POSSIBLE": "Nothing left to recall"},
    },
    "RECEIVING_BANK_NOTIFIED": {
        "label": "Receiving bank notified",
        "results": {"DONE": "Notified and asked to hold", "NOT_POSSIBLE": "Could not reach them"},
        "hint": "If the customer has reported the fraud, this also records when the receiving bank was told, for the regulatory deadline.",
    },
    "DESTINATIONS_CHECKED": {
        "label": "Checked the destination accounts",
        "results": {"NETWORK_SUSPECTED": "Several customers paid the same new accounts",
                    "NOTHING_FOUND": "Nothing links them"},
    },
    "CONTACT_DETAILS_CHECKED": {
        "label": "Checked recent changes to phone, email or PIN",
        "results": {"CHANGED_BY_CUSTOMER": "Changed by the customer", "CHANGED_NOT_BY_CUSTOMER": "Not changed by the customer",
                    "NO_CHANGES": "No recent changes"},
    },
    "IDENTITY_VERIFIED": {
        "label": "Verified the owner in person or by video",
        "results": {"VERIFIED": "It is the owner", "FAILED": "Could not verify"},
    },
    "CARD_LOCATION_CONFIRMED": {
        "label": "Asked where the customer and their card are",
        "results": {"TRAVELLING": "Customer is travelling with the card", "CARD_AT_HOME": "Customer and card are elsewhere"},
    },
}

# Steps per recommended action (triage.recommend). Each step is the thing to do
# and the action that records it. Order is the order to do them in.
STEPS: dict[str, list[dict[str, str]]] = {
    "Send to Information Security (InfoSec) and block payments to this recipient": [
        {"text": "Escalate to InfoSec now. They own sanctions matches.", "action": "ESCALATE"},
        {"text": "Do not contact the customer: a sanctions hit has reporting rules.", "action": ""},
        {"text": "Hold further transfers to the destination.", "action": "HOLD_REQUESTED"},
    ],
    "Confirm fraud and ask the receiving bank to return the money": [
        {"text": "Raise a recall on the transfers below, newest first.", "action": "RECALL_REQUESTED"},
        {"text": "Call the customer to confirm they did not authorise it.", "action": "CUSTOMER_CONTACTED"},
        {"text": "Record confirmed fraud.", "action": "OUTCOME"},
    ],
    "Block the card and issue a new one": [
        {"text": "Block the card. The card is compromised, not the account.", "action": "CARD_BLOCK_REQUESTED"},
        {"text": "Call the customer to arrange a reissue.", "action": "CUSTOMER_CONTACTED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Hold further transfers and contact the customer another way, not on the phone number on file": [
        {"text": "Hold further transfers in core banking.", "action": "HOLD_REQUESTED"},
        {"text": "Reach the customer by email or the branch, not the phone number on file.", "action": "CUSTOMER_CONTACTED"},
        {"text": "If they did not make the transfers, notify the receiving bank.", "action": "RECEIVING_BANK_NOTIFIED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Block the card and confirm where the customer is": [
        {"text": "Ask where the customer and their card are.", "action": "CARD_LOCATION_CONFIRMED"},
        {"text": "If they are not where the card was used, block the card.", "action": "CARD_BLOCK_REQUESTED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Call the customer before paying out, and ask who asked them to pay": [
        {"text": "Call the customer and ask who told them to make the payment.", "action": "CUSTOMER_CONTACTED"},
        {"text": "If it was a scam, notify the receiving bank and ask them to hold.", "action": "RECEIVING_BANK_NOTIFIED"},
        {"text": "Check whether other customers paid the same account.", "action": "DESTINATIONS_CHECKED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Confirm it is really the owner, in person or by video call, before letting more money go": [
        {"text": "Check who changed the phone number or email, and when.", "action": "CONTACT_DETAILS_CHECKED"},
        {"text": "Hold further transfers until the owner is verified.", "action": "HOLD_REQUESTED"},
        {"text": "Verify the owner in person or by video.", "action": "IDENTITY_VERIFIED"},
        {"text": "If the change came from inside the bank, escalate to InfoSec.", "action": "ESCALATE"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Call the customer before any further transfer clears": [
        {"text": "Call the number on file, not any number in the transaction.", "action": "CUSTOMER_CONTACTED"},
        {"text": "If they did not make them, hold further transfers.", "action": "HOLD_REQUESTED"},
        {"text": "If you cannot reach them while money is still leaving, send the case to InfoSec.", "action": "ESCALATE"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Check the recipient accounts: they may be mule accounts working together": [
        {"text": "Check whether the destinations are new and paid by other customers.", "action": "DESTINATIONS_CHECKED"},
        {"text": "If several customers fed the same new accounts, escalate to Fraud Ops as a network.", "action": "ESCALATE"},
        {"text": "Otherwise call the customer to confirm the payments.", "action": "CUSTOMER_CONTACTED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Check quickly, then close as no fraud": [
        {"text": "Check the timeline is consistent with this customer.", "action": "TIMELINE_REVIEWED"},
        {"text": "Record 'No fraud found'. That answer also teaches the system what normal looks like.", "action": "OUTCOME"},
    ],
    "Call the customer to check they made these payments": [
        {"text": "Call the number on file and confirm the transactions.", "action": "CUSTOMER_CONTACTED"},
        {"text": "If they did not make them, raise a recall.", "action": "RECALL_REQUESTED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Look through the account's recent activity, then call the customer if it continues": [
        {"text": "Read the timeline before doing anything else.", "action": "TIMELINE_REVIEWED"},
        {"text": "If the activity is still running, call the customer.", "action": "CUSTOMER_CONTACTED"},
        {"text": "Record the outcome.", "action": "OUTCOME"},
    ],
    "Keep an eye on it — no action needed unless it happens again": [
        {"text": "Check the timeline; no customer contact needed.", "action": "TIMELINE_REVIEWED"},
        {"text": "Record the outcome so the case leaves the queue.", "action": "OUTCOME"},
    ],
}

FALLBACK_STEPS = [
    {"text": "Read the timeline and decide whether it looks like the customer.", "action": "TIMELINE_REVIEWED"},
    {"text": "Call the customer if anything is unexplained.", "action": "CUSTOMER_CONTACTED"},
    {"text": "Record the outcome.", "action": "OUTCOME"},
]

# D108: InfoSec is out of scope at the owner's direction — there is no such
# specialist on this desk — so it is no longer offered as a destination. The
# entry stays defined, unlisted, because cases escalated to it before the change
# still name it and their history has to keep rendering.
RETIRED_ESCALATION = {
    "INFOSEC": {
        "label": "InfoSec (retired)",
        "when": [],
        "what_happens": ["This destination is no longer in use. Account takeover, login abuse "
                         "and MFA problems are actioned on this desk: propose the containment "
                         "the bank should apply, and a lead approves it."],
    },
}

ESCALATION = {
    "FRAUD_OPS": {
        "label": "Fraud Ops lead",
        "when": ["Several customers paid the same new accounts: a network, not one case.",
                 "The money at risk is beyond what you can decide on alone.",
                 "You need a hold or recall approved."],
        "what_happens": ["The case waits in the lead's escalated queue.",
                         "The lead can take it, reassign it, record the outcome and close it,",
                         "or hand it back to you with instructions.",
                         "The response clock keeps running while it is with them."],
    },
}

# The path every case takes. A case is in exactly one stage.
STAGES = ["NEW", "IN_REVIEW", "ESCALATED", "AWAITING_APPROVAL", "AWAITING_CLOSE", "CLOSED"]
STAGE_LABEL = {
    "NEW": "New, nobody on it",
    "IN_REVIEW": "Being investigated",
    "ESCALATED": "Escalated",
    # D93: proposed is not decided. The analyst is finished; the case is not.
    "AWAITING_APPROVAL": "Fraud finding proposed, waiting for a lead's decision",
    "AWAITING_CLOSE": "Approved, waiting for a lead to close",
    "CLOSED": "Closed",
}


def stage(case: dict[str, Any], *, pending_submission: bool = False) -> str:
    if case["state"] == "CLOSED":
        return "CLOSED"
    if case.get("outcome"):
        # An outcome exists only because a lead approved it (D93).
        return "AWAITING_CLOSE"
    if pending_submission:
        return "AWAITING_APPROVAL"
    if case["state"] == "ESCALATED":
        return "ESCALATED"
    if case["state"] == "UNDER_REVIEW" or case.get("assignee_id"):
        return "IN_REVIEW"
    return "NEW"


def steps_for(action: str | None) -> list[dict[str, str]]:
    return STEPS.get(action or "", FALLBACK_STEPS)


def catalog() -> dict[str, Any]:
    # `escalation` is what may be chosen now; `retired` only labels what a
    # past case already carries, so old history still reads correctly.
    return {"actions": ACTIONS, "escalation": ESCALATION, "retired_escalation": RETIRED_ESCALATION, "stages": [{"key": s, "label": STAGE_LABEL[s]} for s in STAGES]}

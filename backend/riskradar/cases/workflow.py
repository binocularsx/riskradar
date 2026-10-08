"""What an analyst can do about a case, and where a case is on its way to resolution (D83).

Plain English
-------------
Risk Radar recommends; people act, in the bank's own systems (D7). The fraud desk
is not customer-facing (D109): analysts investigate with the bank's records and
recommend, and the support team acts on the customer. So each step here is
investigation the analyst can do and can say here that they did, ending in a
proposal a lead approves before support sees it. This module is that list:

* **Steps** — for each recommended action, the steps in order, each tied to an
  action the analyst records with its result ("recipient accounts checked:
  several customers paid the same new accounts"). The dashboard shows which are
  done and which are left.
* **Escalation guidance** — when to send a case to the Fraud Ops lead, and what
  happens when you do, in words.
* **Stages** — the path every case takes, so its position can be drawn and the
  whole desk can be counted stage by stage.

Nothing here decides anything. It turns the recommendation into work that can be
tracked, and the tracking into something a lead can manage.
"""

from __future__ import annotations

from typing import Any

# Every action an analyst can record, and the results that action can have.
ACTIONS: dict[str, dict[str, Any]] = {
    # D109: investigation only. The desk is not customer-facing; every step an
    # analyst records here is something they did with the bank's records, never
    # with the customer. What used to be customer-facing steps is now asked of
    # the support team as a recommended action (see RETIRED_ACTIONS).
    "TIMELINE_REVIEWED": {
        "label": "Reviewed the account's recent activity",
        "results": {"CONSISTENT": "Looks like the customer's normal behaviour",
                    "UNUSUAL": "Not like this customer"},
    },
    "DESTINATIONS_CHECKED": {
        "label": "Checked the recipient accounts",
        "results": {"NETWORK_SUSPECTED": "Several customers paid the same new accounts",
                    "NOTHING_FOUND": "Nothing links them"},
    },
    "CONTACT_DETAILS_CHECKED": {
        "label": "Checked the customer's record for recent changes to phone, email or PIN",
        "results": {"CHANGED_BY_CUSTOMER": "Changed through the customer's usual channel and device",
                    "CHANGED_NOT_BY_CUSTOMER": "Changed from an unusual channel or device",
                    "NO_CHANGES": "No recent changes"},
    },
    "LINKED_ACCOUNTS_CHECKED": {
        "label": "Checked the customer's other accounts and connections",
        "results": {"LINKED_TO_FRAUD": "Linked to other flagged customers or accounts",
                    "NOTHING_LINKED": "Nothing suspicious linked"},
    },
    "CARD_USAGE_CHECKED": {
        "label": "Checked where and how the card was used",
        "results": {"UNUSUAL_PLACES": "Used somewhere this customer does not use it",
                    "USUAL_PLACES": "Used where this customer normally does"},
    },
    "REPORT_COMPARED": {
        "label": "Compared the customer's report with the payments",
        "results": {"MATCHES": "The customer's account matches the payments",
                    "PARTLY_MATCHES": "It partly matches",
                    "DOES_NOT_MATCH": "It does not match the payments"},
    },
}

# D109: steps the desk used to record itself and now asks of the support team.
# New recordings are refused; the labels stay so past cases still read.
RETIRED_ACTIONS: dict[str, dict[str, Any]] = {
    "CUSTOMER_CONTACTED": {
        "label": "Contacted the customer (now done by support)",
        "results": {"CONFIRMED_GENUINE": "Reached: they made these payments",
                    "DENIED": "Reached: they did not make them",
                    "SCAM_ADMITTED": "Reached: they paid because someone told them to",
                    "NOT_REACHED": "Could not reach them"},
        "instead": "CONTACT_CUSTOMER",
    },
    "CARD_BLOCK_REQUESTED": {
        "label": "Card blocked (now done by support)",
        "results": {"DONE": "Blocked in the card system", "NOT_POSSIBLE": "Could not block"},
        "instead": "CARD_FREEZE",
    },
    "HOLD_REQUESTED": {
        "label": "Further transfers held (now done by support)",
        "results": {"DONE": "Hold placed in core banking", "NOT_POSSIBLE": "Could not hold"},
        "instead": "DEBIT_RESTRICTION",
    },
    "RECALL_REQUESTED": {
        "label": "Funds recall raised (now done by support)",
        "results": {"DONE": "Recall raised with the rail (NIP)", "NOT_POSSIBLE": "Nothing left to recall"},
        "instead": "NOTIFY_RECEIVING_BANK",
    },
    "RECEIVING_BANK_NOTIFIED": {
        "label": "Receiving bank notified (now done by support)",
        "results": {"DONE": "Notified and asked to hold", "NOT_POSSIBLE": "Could not reach them"},
        "instead": "NOTIFY_RECEIVING_BANK",
    },
    "IDENTITY_VERIFIED": {
        "label": "Verified the owner in person or by video (now done by support)",
        "results": {"VERIFIED": "It is the owner", "FAILED": "Could not verify"},
        "instead": "VERIFY_IDENTITY",
    },
    "CARD_LOCATION_CONFIRMED": {
        "label": "Asked where the customer and their card are (now done by support)",
        "results": {"TRAVELLING": "Customer is travelling with the card", "CARD_AT_HOME": "Customer and card are elsewhere"},
        "instead": "CONTACT_CUSTOMER",
    },
}

_PROPOSE = {"text": "Propose your finding and the actions for support. A lead approves it before "
                    "support sees anything.", "action": "OUTCOME"}
_HEADS_UP = {"text": "If money is still leaving, send support an urgent heads-up to hold while you finish.",
             "action": "HEADS_UP"}

# Steps per recommended action (triage.recommend). Each step is investigation
# the analyst does with the bank's records, and ends in a proposal for a lead.
# Keyed on the recommendation's exact wording; test_triage pins that every
# recommendation has an entry.
STEPS: dict[str, list[dict[str, str]]] = {
    "Recommend support blocks payments to this recipient": [
        {"text": "Do not contact anyone outside the desk: a sanctions match has reporting rules.", "action": ""},
        {"text": "Check the customer's other accounts for payments to the same recipient.",
         "action": "LINKED_ACCOUNTS_CHECKED"},
        {**_PROPOSE, "text": "Propose confirmed fraud with a block on the recipient. A lead approves it "
                             "before support sees anything."},
    ],
    "Confirm fraud and recommend support asks the receiving bank to return the money": [
        {"text": "Check which payments went to the known mule account, newest first.", "action": "DESTINATIONS_CHECKED"},
        _HEADS_UP,
        {**_PROPOSE, "text": "Propose confirmed fraud, with a request to the receiving bank for each payment. "
                             "A lead approves it before support sees anything."},
    ],
    "Recommend support blocks the card and issues a new one": [
        {"text": "Check where and how the card was used.", "action": "CARD_USAGE_CHECKED"},
        _HEADS_UP,
        _PROPOSE,
    ],
    "Recommend support holds further transfers and contacts the customer another way, not on the phone number on file": [
        {"text": "Check the customer's record: when the SIM changed, and through which channel.",
         "action": "CONTACT_DETAILS_CHECKED"},
        {"text": "Check the recipients for links to other cases.", "action": "DESTINATIONS_CHECKED"},
        _HEADS_UP,
        {**_PROPOSE, "text": "Propose your finding with a hold and a safe-channel contact for support. "
                             "A lead approves it before support sees anything."},
    ],
    "Recommend support blocks the card and confirms where the customer is": [
        {"text": "Check where and how the card was used.", "action": "CARD_USAGE_CHECKED"},
        {"text": "Look through the account's recent activity for travel or other card use.",
         "action": "TIMELINE_REVIEWED"},
        _PROPOSE,
    ],
    "Recommend support contacts the customer before paying out, and asks who asked them to pay": [
        {"text": "Check whether other customers paid the same account.", "action": "DESTINATIONS_CHECKED"},
        {"text": "Look through the account's recent activity.", "action": "TIMELINE_REVIEWED"},
        {**_PROPOSE, "text": "Propose your finding with a customer contact and a request to the receiving bank. "
                             "A lead approves it before support sees anything."},
    ],
    "Recommend support confirms it is really the owner, in person or by video call, before letting more money go": [
        {"text": "Check the customer's record: who changed the phone number or email, and when.",
         "action": "CONTACT_DETAILS_CHECKED"},
        {"text": "Look through the account's recent activity.", "action": "TIMELINE_REVIEWED"},
        _HEADS_UP,
        _PROPOSE,
    ],
    "Recommend support holds further transfers and calls the customer": [
        {"text": "Check the customer's record for a new device, SIM, password or PIN change.",
         "action": "CONTACT_DETAILS_CHECKED"},
        {"text": "Check the recipients for links to other cases.", "action": "DESTINATIONS_CHECKED"},
        _HEADS_UP,
        _PROPOSE,
    ],
    "Check the recipient accounts: they may be mule accounts working together": [
        {"text": "Check whether the recipients are new and paid by other customers.", "action": "DESTINATIONS_CHECKED"},
        {"text": "If several customers fed the same new accounts, escalate to the Fraud Ops lead as a network.",
         "action": "ESCALATE"},
        _PROPOSE,
    ],
    "Check quickly, then close as no fraud": [
        {"text": "Check the account's recent activity is consistent with this customer.", "action": "TIMELINE_REVIEWED"},
        {**_PROPOSE, "text": "Propose 'No fraud found'. That answer also teaches the system what normal looks like."},
    ],
    "Send support a heads-up, then recommend they confirm the payments with the customer": [
        {"text": "Look through the account's recent activity.", "action": "TIMELINE_REVIEWED"},
        _HEADS_UP,
        {"text": "Check the recipients for links to other cases.", "action": "DESTINATIONS_CHECKED"},
        _PROPOSE,
    ],
    "Look through the account's recent activity; if it continues, recommend support calls the customer": [
        {"text": "Look through the account's recent activity before doing anything else.", "action": "TIMELINE_REVIEWED"},
        _PROPOSE,
    ],
    "Keep an eye on it — no action needed unless it happens again": [
        {"text": "Check the account's recent activity; nothing goes to support for this.", "action": "TIMELINE_REVIEWED"},
        {**_PROPOSE, "text": "Propose your finding so the case leaves the queue."},
    ],
}

# D109e: a case the support team reported always starts with their account of it.
REPORTED_FIRST_STEP = {"text": "Compare the customer's report with the payments on this case.",
                       "action": "REPORT_COMPARED"}

FALLBACK_STEPS = [
    {"text": "Look through the account's recent activity and decide whether it looks like the customer.",
     "action": "TIMELINE_REVIEWED"},
    _PROPOSE,
]

# D111: InfoSec is gone from the system, enum values included, so there is no
# retired destination left to render. D108 had kept one defined-but-unlisted for
# cases escalated before it was retired; no case was ever escalated to it.

ESCALATION = {
    "FRAUD_OPS": {
        "label": "Fraud Ops lead",
        "when": ["Several customers paid the same new accounts: a network, not one case.",
                 "The money at risk is beyond what you can decide on alone.",
                 "You want a lead's view before recommending action on the customer."],
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


def steps_for(action: str | None, *, reported: bool = False) -> list[dict[str, str]]:
    steps = STEPS.get(action or "", FALLBACK_STEPS)
    return [REPORTED_FIRST_STEP, *steps] if reported else steps


def label_for(code: str) -> str | None:
    """A recorded action's label, current or retired, so old history reads."""
    return (ACTIONS.get(code) or RETIRED_ACTIONS.get(code) or {}).get("label")


def catalog() -> dict[str, Any]:
    # `escalation` is what may be chosen now; `retired` only labels what a
    # past case already carries, so old history still reads correctly.
    return {"actions": ACTIONS, "retired_actions": RETIRED_ACTIONS, "escalation": ESCALATION, "stages": [{"key": s, "label": STAGE_LABEL[s]} for s in STAGES]}

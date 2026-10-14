"""Codes said the way the support team reads them (D109).

Anything Risk Radar sends to a person outside the desk is read by someone who
has never seen a rule code. The console has the same list in
``frontend/src/lib/words.js``; this is the part the backend needs for the
messages it writes itself.
"""

from __future__ import annotations

ACTION = {
    "DEBIT_RESTRICTION": "Stop money leaving the account",
    "CHANNEL_RESTRICTION": "Block one banking channel",
    "CARD_FREEZE": "Freeze the card",
    "BENEFICIARY_RESTRICTION": "Block payments to this recipient",
    "TRANSACTION_REVERSAL": "Reverse the payment",
    "SESSION_TERMINATION": "Log the customer out everywhere",
    "CREDENTIAL_RESET": "Reset the password and PIN",
    "MFA_REENROLMENT": "Set up two-step login again",
    "CONTACT_CUSTOMER": "Contact the customer on a safe channel",
    "VERIFY_IDENTITY": "Confirm it is really the owner, in person or by video call",
    "NOTIFY_RECEIVING_BANK": "Ask the receiving bank to hold or return the money",
}

OUTCOME = {
    "CONFIRMED_FRAUD": "Fraud confirmed",
    "FALSE_POSITIVE": "No fraud found",
    "INCONCLUSIVE": "Not enough evidence either way",
}


def words(code: str | None) -> str:
    """The fallback: ``SOME_CODE`` -> ``Some code``. Never shows a raw code."""
    if not code:
        return ""
    s = code.replace("_", " ").lower().strip()
    return s[:1].upper() + s[1:]


def action(code: str | None) -> str:
    return ACTION.get(code or "", words(code))


def outcome(code: str | None) -> str:
    return OUTCOME.get(code or "", words(code))

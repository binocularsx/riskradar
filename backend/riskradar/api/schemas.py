"""Request and response contracts.

FR-003 is the rule that shapes this module: **reject, do not coerce.** Pydantic
is configured to forbid unknown fields and to refuse silent type conversion, so a
caller sending ``amount_minor: "1000.50"`` gets a 422 and a dead-letter row
rather than a quietly rounded transaction. Money that arrives wrong should stop,
not round.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Channel = Literal["MOBILE_APP", "WEB", "USSD", "POS", "ATM", "AGENT", "BRANCH", "API"]
Instrument = Literal["CARD", "ACCOUNT_TRANSFER", "CASH", "WALLET"]
Rail = Literal["NIP", "NEFT", "RTGS", "CARD_SCHEME", "INTRABANK", "ATM_NETWORK"]
AuthResult = Literal["APPROVED", "DECLINED", "FAILED", "REVERSED"]
ProductType = Literal["SAVINGS", "CURRENT", "DOMICILIARY", "WALLET"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
CaseState = Literal["OPEN", "UNDER_REVIEW", "ESCALATED", "CLOSED"]
CaseOutcome = Literal["CONFIRMED_FRAUD", "FALSE_POSITIVE", "INCONCLUSIVE"]


class Strict(BaseModel):
    # strict=True stops "5" becoming 5 and "true" becoming True.
    # extra="forbid" stops a typo'd field being accepted and ignored — the caller
    # would believe they sent a device fingerprint that we never received.
    model_config = ConfigDict(strict=True, extra="forbid")


# JSON has no datetime type, so a timestamp always arrives as a string and strict
# mode would reject every well-formed payload. Relaxing the *parse* here is not a
# coercion loophole: the format is unambiguous, and _require_timezone below still
# refuses anything without an offset. What strict mode is actually protecting is
# `amount_minor` — money that arrives as a string should stop, not round.
IsoDatetime = Annotated[datetime, Field(strict=False)]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class TransactionIn(Strict):
    """One transaction as a channel or switch would present it (§9.1).

    Identifiers arrive **raw** and are tokenised before persistence (FR-004): the
    boundary is the last place a real account number exists. Callers therefore
    send ``customer_id`` and ``account_id``, never tokens — a caller that could
    supply a token could also forge one and merge two customers' baselines.
    """

    transaction_ref: Annotated[str, Field(min_length=1, max_length=128)]
    occurred_at: IsoDatetime

    amount_minor: Annotated[int, Field(ge=0)]
    currency: Annotated[str, Field(min_length=3, max_length=3)]

    channel: Channel
    instrument: Instrument
    rail: Rail

    customer_id: Annotated[str, Field(min_length=1, max_length=128)]
    account_id: Annotated[str, Field(min_length=1, max_length=128)]
    beneficiary_account_id: Annotated[str | None, Field(max_length=128)] = None
    device_fingerprint: Annotated[str | None, Field(max_length=256)] = None

    ip_region: Annotated[str | None, Field(max_length=64)] = None
    merchant_category: Annotated[str | None, Field(max_length=16)] = None

    auth_result: AuthResult = "APPROVED"
    decline_reason: Annotated[str | None, Field(max_length=64)] = None

    display_name: Annotated[str | None, Field(max_length=128)] = None

    # D22: point-in-time account context. Optional — if omitted the boundary
    # resolves it from the accounts dimension and stamps it. Either way it is
    # stamped once, here, and never joined afterwards.
    account_opened_at: IsoDatetime | None = None
    last_activity_at: IsoDatetime | None = None
    product_type: ProductType | None = None
    origin_sol_id: Annotated[str | None, Field(max_length=32)] = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        if not v.isalpha():
            raise ValueError("currency must be three letters (ISO 4217)")
        return v.upper()

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        # A naive timestamp is ambiguous, and every behavioural feature is a time
        # window. Guessing a zone here would corrupt velocity silently.
        if v.tzinfo is None:
            raise ValueError("occurred_at must include a timezone offset")
        return v


class TransactionAccepted(BaseModel):
    transaction_ref: str
    transaction_id: int
    status: Literal["accepted", "duplicate"]
    queued: bool


class BatchIn(Strict):
    """FR-006. Replayed transactions do not raise alerts unless asked (D8d).

    Re-scoring six weeks of history after a model change must not page anybody,
    and the default protects that even when the caller forgets.
    """

    transactions: Annotated[list[TransactionIn], Field(min_length=1, max_length=1000)]
    raise_alerts: bool = False
    is_replay: bool = True


class BatchAccepted(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: list[TransactionAccepted]
    errors: list[dict]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginIn(Strict):
    email: str
    password: str
    totp_code: str | None = None


class LoginOut(BaseModel):
    status: Literal["ok", "mfa_required"]
    user: dict | None = None


class MeOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str
    permissions: list[str]
    mfa_satisfied: bool


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


class NoteIn(Strict):
    body: Annotated[str, Field(min_length=1, max_length=4000)]


class OutcomeIn(Strict):
    outcome: CaseOutcome
    note: Annotated[str | None, Field(max_length=4000)] = None


class DispositionIn(Strict):
    """One analyst verdict, recorded in a single call.

    ``close`` is a request, not a command — the server only honours it if the
    caller holds ``cases:close``. An analyst can ask; a lead's ask succeeds.
    ``followed_recommendation`` is recorded so the desk can later measure how
    often the system's advice matched what a human actually decided, which is
    the only honest way to find out whether the advice is any good.
    """

    outcome: CaseOutcome
    note: Annotated[str | None, Field(max_length=4000)] = None
    close: bool = False
    followed_recommendation: bool | None = None


class EscalateIn(Strict):
    target: Literal["INFOSEC", "FRAUD_OPS"]
    note: Annotated[str | None, Field(max_length=4000)] = None


class AssignIn(Strict):
    assignee_id: int | None


def _aware(v: datetime | None) -> datetime | None:
    # A regulatory deadline computed from a naive timestamp could be an hour
    # out, which is longer than the 30-minute clock it starts.
    if v is not None and v.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return v


class ReportIn(Strict):
    """The customer's report that starts the CBN clocks (WP-05, D71)."""

    reported_at: IsoDatetime
    channel: Literal["BRANCH", "CONTACT_CENTRE", "MOBILE_APP", "WEB", "EMAIL", "USSD"]
    counterparty_institution: Annotated[str | None, Field(min_length=2, max_length=128)] = None
    note: Annotated[str | None, Field(max_length=4000)] = None

    @field_validator("reported_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        return _aware(v)


class MilestoneIn(Strict):
    """A moment that stops a regulatory clock. Recorded once, never edited."""

    milestone: Literal["ACKNOWLEDGED", "COUNTERPARTY_NOTIFIED", "INVESTIGATION_CONCLUDED", "REIMBURSED"]
    # When it happened, if not now: a call made before it was logged still
    # counts at the time it was made.
    at: IsoDatetime | None = None
    counterparty_institution: Annotated[str | None, Field(min_length=2, max_length=128)] = None
    note: Annotated[str | None, Field(max_length=4000)] = None

    @field_validator("at")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        return _aware(v)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


class RuleUpdateIn(Strict):
    enabled: bool | None = None
    params: dict | None = None
    severity: RiskLevel | None = None
    # D69e (base PRD FR-504, FR-506): why the change was made. When given it
    # becomes the rule's rationale, restarts its 90-day review clock, and is
    # written into the permanent record with the change.
    rationale: Annotated[str | None, Field(max_length=2000)] = None
    owner: Annotated[str | None, Field(max_length=200)] = None


class ThresholdsIn(Strict):
    p_monitor: float
    p_review: float
    p_hold: float
    alert_min_level: RiskLevel = "MEDIUM"
    notes: str | None = None


class ListEntryIn(Strict):
    kind: Literal["SANCTIONED", "KNOWN_MULE", "ALLOWLIST"]
    beneficiary_account_id: str
    account_id: str | None = None
    note: str | None = None


class PromoteModelIn(Strict):
    model_version_id: int
    comparison: dict | None = None

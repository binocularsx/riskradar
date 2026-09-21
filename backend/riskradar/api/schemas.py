"""Request and response contracts.

FR-003 is the rule that shapes this module: **reject, do not coerce.** Pydantic
is configured to forbid unknown fields and to refuse silent type conversion, so a
caller sending ``amount_minor: "1000.50"`` gets a 422 and a dead-letter row
rather than a quietly rounded transaction. Money that arrives wrong should stop,
not round.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Channel = Literal["MOBILE_APP", "WEB", "USSD", "POS", "ATM", "AGENT", "BRANCH", "API"]
Instrument = Literal["CARD", "ACCOUNT_TRANSFER", "CASH", "WALLET"]
Rail = Literal["NIP", "NEFT", "RTGS", "CARD_SCHEME", "INTRABANK", "ATM_NETWORK"]
AuthResult = Literal["APPROVED", "DECLINED", "FAILED", "REVERSED"]
ProductType = Literal["SAVINGS", "CURRENT", "DOMICILIARY", "WALLET"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
CaseState = Literal["OPEN", "UNDER_REVIEW", "ESCALATED", "CLOSED"]
CaseOutcome = Literal["CONFIRMED_FRAUD", "FALSE_POSITIVE", "INCONCLUSIVE"]


# D87: how far ahead of this server's clock an event may claim to have happened.
# A payment from the future is a caller's clock fault or a replay bug, and it
# would sit ahead of every velocity window until real time caught up with it.
MAX_CLOCK_SKEW = timedelta(seconds=int(os.environ.get("RISKRADAR_MAX_CLOCK_SKEW_SECONDS", "300")))


def _not_in_the_future(v: datetime) -> datetime:
    if v - datetime.now(timezone.utc) > MAX_CLOCK_SKEW:
        raise ValueError(
            f"occurred_at is more than {int(MAX_CLOCK_SKEW.total_seconds())}s in the future; check the sender's clock"
        )
    return v


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
    # D75: optional. When absent the boundary asks the core; tokenised either way.
    bvn: Annotated[str | None, Field(pattern=r"^\d{11}$")] = None
    nin: Annotated[str | None, Field(pattern=r"^\d{11}$")] = None

    # D78: money leaving the customer's account, or arriving in it. For an
    # INBOUND credit, customer_id and account_id are the receiving side and
    # the sender is the remitter; there is no beneficiary.
    direction: Literal["OUTBOUND", "INBOUND"] = "OUTBOUND"
    remitter_account_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    remitter_bank_code: Annotated[str | None, Field(max_length=16)] = None

    @model_validator(mode="after")
    def _direction_is_consistent(self) -> "TransactionIn":
        if self.direction == "INBOUND":
            if not self.remitter_account_id:
                raise ValueError("an INBOUND credit needs remitter_account_id")
            if self.beneficiary_account_id:
                raise ValueError("an INBOUND credit has a remitter, not a beneficiary")
        elif self.remitter_account_id or self.remitter_bank_code:
            raise ValueError("remitter fields belong to INBOUND credits only")
        return self
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
        return _not_in_the_future(v)


# ---------------------------------------------------------------------------
# The event envelope (WP-01, D72)
# ---------------------------------------------------------------------------
#
# A payment is one event type among several. Every non-payment event shares
# the envelope below and adds a typed ``detail``; the discriminator is
# ``event_type``, so a LOGIN carrying a SIM_CHANGED detail is refused rather
# than stored with the wrong shape.

EventType = Literal[
    "PAYMENT", "CREDIT", "LOGIN", "DEVICE_BOUND", "CREDENTIAL_CHANGED", "PAYEE_ADDED", "SIM_CHANGED", "LIMIT_CHANGED"
]
Ref = Annotated[str, Field(min_length=1, max_length=128)]


class EnvelopeIn(Strict):
    event_ref: Ref
    occurred_at: IsoDatetime
    customer_id: Annotated[str, Field(min_length=1, max_length=128)]
    account_id: Annotated[str | None, Field(max_length=128)] = None
    device_fingerprint: Annotated[str | None, Field(max_length=256)] = None
    ip_region: Annotated[str | None, Field(max_length=64)] = None
    channel: Channel | None = None
    bvn: Annotated[str | None, Field(pattern=r"^\d{11}$")] = None
    nin: Annotated[str | None, Field(pattern=r"^\d{11}$")] = None

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must include a timezone offset")
        return _not_in_the_future(v)


class LoginDetail(Strict):
    # A failed-login burst is not an event of its own; it is many of these.
    result: Literal["SUCCESS", "FAILED"]
    method: Literal["PASSWORD", "PIN", "BIOMETRIC", "OTP"]
    failure_reason: Literal["WRONG_CREDENTIAL", "LOCKED", "EXPIRED", "OTHER"] | None = None


class DeviceBoundDetail(Strict):
    binding: Literal["BOUND", "UNBOUND"]


class CredentialChangedDetail(Strict):
    credential: Literal["PASSWORD", "PIN", "MFA_METHOD", "EMAIL", "PHONE"]
    initiated_by: Literal["CUSTOMER", "BRANCH", "CONTACT_CENTRE"]


class PayeeAddedDetail(Strict):
    # Raw at the boundary, tokenised before it is stored, like a payment's.
    beneficiary_account_id: Annotated[str, Field(min_length=1, max_length=128)]
    beneficiary_bank_code: Annotated[str | None, Field(max_length=16)] = None


class SimChangedDetail(Strict):
    msisdn: Annotated[str, Field(min_length=6, max_length=20)]
    carrier: Annotated[str | None, Field(max_length=32)] = None


class LimitChangedDetail(Strict):
    limit: Literal["DAILY_TRANSFER", "SINGLE_TRANSFER", "CARD_DAILY"]
    from_minor: Annotated[int, Field(ge=0)]
    to_minor: Annotated[int, Field(ge=0)]


class PaymentEventIn(Strict):
    """A payment through the envelope: exactly the canonical transaction (§6.1)."""

    event_type: Literal["PAYMENT"]
    payment: TransactionIn


class LoginEventIn(EnvelopeIn):
    event_type: Literal["LOGIN"]
    detail: LoginDetail


class DeviceBoundEventIn(EnvelopeIn):
    event_type: Literal["DEVICE_BOUND"]
    device_fingerprint: Annotated[str, Field(min_length=1, max_length=256)]
    detail: DeviceBoundDetail


class CredentialChangedEventIn(EnvelopeIn):
    event_type: Literal["CREDENTIAL_CHANGED"]
    detail: CredentialChangedDetail


class PayeeAddedEventIn(EnvelopeIn):
    event_type: Literal["PAYEE_ADDED"]
    account_id: Annotated[str, Field(min_length=1, max_length=128)]
    detail: PayeeAddedDetail


class SimChangedEventIn(EnvelopeIn):
    event_type: Literal["SIM_CHANGED"]
    detail: SimChangedDetail


class LimitChangedEventIn(EnvelopeIn):
    event_type: Literal["LIMIT_CHANGED"]
    account_id: Annotated[str, Field(min_length=1, max_length=128)]
    detail: LimitChangedDetail


EventIn = Annotated[
    PaymentEventIn | LoginEventIn | DeviceBoundEventIn | CredentialChangedEventIn
    | PayeeAddedEventIn | SimChangedEventIn | LimitChangedEventIn,
    Field(discriminator="event_type"),
]


class EventAccepted(BaseModel):
    event_ref: str
    event_type: EventType
    event_id: int
    status: Literal["accepted", "duplicate"]
    # Only payments are scored today; WP-02 adds the other types.
    queued: bool
    transaction_id: int | None = None


class EventBatchIn(Strict):
    events: Annotated[list[EventIn], Field(min_length=1, max_length=1000)]
    is_replay: bool = True
    raise_alerts: bool = False


class EventBatchAccepted(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: list[EventAccepted]
    errors: list[dict]


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
    # D93: the case version the analyst read, so a stale verdict is refused.
    expected_case_version: int | None = None


class EscalateIn(Strict):
    target: Literal["INFOSEC", "FRAUD_OPS"]
    note: Annotated[str | None, Field(max_length=4000)] = None


class CaseActionIn(Strict):
    """D83: a recommended step done in the bank's systems, recorded with its result."""

    action_code: Annotated[str, Field(min_length=3, max_length=64)]
    result: Annotated[str, Field(min_length=2, max_length=64)]
    detail: Annotated[str | None, Field(max_length=2000)] = None


class ReturnIn(Strict):
    """D83: an escalated case handed back to the analyst who raised it."""

    findings: Annotated[str, Field(min_length=5, max_length=4000)]


class AssignIn(Strict):
    """D94: routing is automatic, so moving a case by hand is an exception and says why."""

    assignee_id: int | None
    reason: Annotated[str, Field(min_length=5, max_length=500)]
    expected_case_version: int | None = None


def _aware(v: datetime | None) -> datetime | None:
    # A regulatory deadline computed from a naive timestamp could be an hour
    # out, which is longer than the 30-minute clock it starts.
    if v is not None and v.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return v


class DirectiveAckIn(Strict):
    """What the bank did with a directive (WP-07, D74). Recorded once."""

    action_taken: Literal["APPLIED", "NOT_APPLIED", "SHADOW_RECORDED"]
    taken_at: IsoDatetime
    reason: Annotated[str | None, Field(max_length=500)] = None

    @field_validator("taken_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        return _aware(v)


class EnforcementPolicyIn(Strict):
    mode: Literal["SHADOW", "LIVE"]
    ttl_seconds: Annotated[int, Field(ge=5, le=3600)]
    fail_open_action: Literal["APPROVE", "APPROVE_AND_MONITOR"] = "APPROVE"
    policy_text: Annotated[str, Field(min_length=20, max_length=8000)]
    signed_by: Annotated[str | None, Field(min_length=3, max_length=200)] = None
    signed_at: IsoDatetime | None = None


class IndustryFlagIn(Strict):
    """One entry from the industry watch-list, as the bank-side connector received it."""

    external_ref: Annotated[str, Field(min_length=1, max_length=128)]
    bvn: Annotated[str, Field(pattern=r"^\d{11}$")]
    institution_code: Annotated[str, Field(min_length=2, max_length=32)]
    reason_code: Annotated[str, Field(min_length=2, max_length=64)]
    flagged_at: IsoDatetime
    expires_at: IsoDatetime
    lifted_at: IsoDatetime | None = None

    @field_validator("flagged_at", "expires_at", "lifted_at")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        return _aware(v)


class IndustryInboundIn(Strict):
    entries: Annotated[list[IndustryFlagIn], Field(min_length=1, max_length=1000)]


class WatchlistPlaceIn(Strict):
    """A temporary watch-list flag on the case's customer (WP-06, D73)."""

    reason: Annotated[str, Field(min_length=5, max_length=500)]
    hours: Annotated[int, Field(ge=1, le=24)] = 24


class WatchlistContactIn(Strict):
    outcome: Literal["CUSTOMER_CONFIRMED_GENUINE", "CUSTOMER_REPORTED_FRAUD"]
    note: Annotated[str | None, Field(max_length=4000)] = None


class WatchlistLiftIn(Strict):
    note: Annotated[str | None, Field(max_length=4000)] = None


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


class RestrictionIn(Strict):
    """D93: one action a submission asks the bank to take, from a fixed list."""

    action: Literal["DEBIT_RESTRICTION", "CHANNEL_RESTRICTION", "CARD_FREEZE", "BENEFICIARY_RESTRICTION"]
    account_token: Annotated[str | None, Field(max_length=128)] = None
    beneficiary_token: Annotated[str | None, Field(max_length=128)] = None
    channel: Channel | None = None
    reason: Annotated[str | None, Field(max_length=500)] = None


class FraudSubmissionIn(Strict):
    """D93: the analyst's proposal. It decides nothing until a lead approves it."""

    proposed_outcome: CaseOutcome
    rationale: Annotated[str, Field(min_length=20, max_length=4000)]
    restrictions: Annotated[list[RestrictionIn], Field(max_length=20)] = []
    expected_case_version: int | None = None


class FraudDecisionIn(Strict):
    """D93: a lead's decision on somebody else's proposal."""

    decision: Literal["APPROVE", "REJECT", "RETURN"]
    reason: Annotated[str, Field(min_length=10, max_length=4000)]


class CustomerReportIn(Strict):
    """D90: a customer reports payments as fraud, whether or not Risk Radar alerted on them."""

    transaction_refs: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]],
                                Field(min_length=1, max_length=50)]
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


class ConfigDecisionIn(Strict):
    """D96: a second administrator's verdict on a proposed detection-tuning change."""

    action: Literal["APPROVE", "REJECT", "RETURN"]
    reason: Annotated[str | None, Field(max_length=2000)] = None


class RestrictionAckIn(Strict):
    """D97: what the bank did about a recommended restriction — reconciliation."""

    outcome: Literal["APPLIED", "NOT_APPLIED", "REJECTED"]
    reason: Annotated[str | None, Field(max_length=2000)] = None
    taken_at: datetime | None = None


class ListEntryIn(Strict):
    kind: Literal["SANCTIONED", "KNOWN_MULE", "ALLOWLIST"]
    beneficiary_account_id: str
    account_id: str | None = None
    note: str | None = None


class PromoteModelIn(Strict):
    model_version_id: int
    comparison: dict | None = None


class BudgetConfigIn(Strict):
    """D86: the alert budget and how strictly it is paced. Every change is audited with its reason."""

    per_day: Annotated[int, Field(ge=1, le=10_000)]
    hourly_burst: Annotated[float, Field(ge=1.0, le=24.0)] = 3.0
    # D92: the share of the day reserved for rule alerts; the model gets the rest.
    rule_share: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6
    enforced: bool = True
    deferral_hours: Annotated[int, Field(ge=1, le=168)] = 24
    reason: Annotated[str, Field(min_length=10, max_length=2000)]


class DeriveThresholdsIn(Strict):
    """D86: solve thresholds from recent traffic; publish only when asked."""

    last_days: Annotated[float, Field(gt=0, le=90)] = 7.0
    publish: bool = False
    min_sample: Annotated[int, Field(ge=100)] = 2000
    notes: Annotated[str | None, Field(max_length=2000)] = None

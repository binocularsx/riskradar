"""The rules layer (D11, D11a).

Plain English
-------------
Six hand-written checks that a human can read and argue with.

Each one looks at a transaction and either says nothing, or raises a named flag
with its evidence attached — "VELOCITY_BURST_1H, because there were 7
transactions in an hour and the limit is 4". They never produce a score or a
number to be added up. They state facts.

They come in three kinds. Two **escalate** (make it look worse), two
**override** (a flat veto — a sanctioned destination is not a matter of
probability), and two **suppress** (make it look better — the customer set this
payee up themselves months ago). Suppression is the one that keeps analysts
from drowning in false alarms, which is why it was built rather than
postponed.

Rules emit **named signals as facts** — ``{code, severity, power, evidence{}}`` —
and never points. A weighted blend of rule points and model probability is a type
error: severity is ordinal, model output is cardinal, and adding them produces a
number meaningful in neither system. It also destroys calibration, which is what
lets you predict alert volume at a threshold, and alert volume is what decides
whether analysts drown.

So rules describe. The policy layer decides.

Six rules (D25), two of each power:

===========================  =========  ==============================================
Code                         Power      Catches
===========================  =========  ==============================================
VELOCITY_BURST_1H            ESCALATE   ATO extraction, mule fan-out speed
ACCOUNT_TAKEOVER_SEQUENCE    ESCALATE   A way in taken over, then a new destination (D77)
MULE_INBOUND_FANIN           ESCALATE   Credits from many senders, unlike the account (D78)
SECOND_LEG_ONWARD_PAYMENT    ESCALATE   That money leaving again within hours (D79)
CARD_TESTING_PROBES          ESCALATE   Authorisation probing — decline-heavy by nature
SCAM_BENEFICIARY_FANIN       ESCALATE   A new destination several customers paid today (D82)
SIM_SWAP_TRANSFER            ESCALATE   A new destination hours after the SIM changed (D82)
DORMANT_ACCOUNT_REACTIVATION ESCALATE   A long-quiet account suddenly moving real money (D82)
CARD_PRESENT_NEW_REGION_CASHOUT ESCALATE A card at terminal after terminal, somewhere new (D82)
SANCTIONED_BENEFICIARY       OVERRIDE   Some things are not probabilistic
KNOWN_MULE_BENEFICIARY       OVERRIDE   Destination confirmed fraudulent by an analyst
PRE_REGISTERED_BENEFICIARY   SUPPRESS   The customer set this payee up on purpose
ESTABLISHED_PAYEE_NORMAL     SUPPRESS   Long-standing payee, ordinary amount
===========================  =========  ==============================================

**Integrity (D10a).** Nothing in this module imports from the simulator or reads
a generator parameter. Rules see the transaction, the computed features, and
administered lists. That wall is the integrity control for the whole ML claim,
and ``tests/test_generator_detector_wall.py`` enforces it mechanically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..features.types import TxView


@dataclass(frozen=True)
class Signal:
    """A fact about the transaction. Never a score."""

    code: str
    power: str  # ESCALATE | OVERRIDE | SUPPRESS
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "power": self.power,
            "severity": self.severity,
            "evidence": self.evidence,
        }


@dataclass
class RuleContext:
    """Everything a rule may read.

    Note the absence of a database handle. Membership is resolved once, before
    evaluation, so a rule cannot issue a query per transaction and quietly turn
    the rules layer into the latency bottleneck.
    """

    tx: TxView
    features: dict[str, float]
    sanctioned: bool = False
    known_mule: bool = False
    pre_registered: bool = False


# A rule is a pure function from (context, params) to a Signal or None.
RuleFn = Callable[[RuleContext, dict[str, Any]], "Signal | None"]

_REGISTRY: dict[str, RuleFn] = {}
# D78: which way the money must be moving for a rule to apply. A velocity burst
# is about payments leaving; a fan-in is about credits arriving. Outbound by
# default, so every rule written before credits existed keeps its meaning.
_DIRECTIONS: dict[str, tuple[str, ...]] = {}


def rule(code: str, directions: tuple[str, ...] = ("OUTBOUND",)) -> Callable[[RuleFn], RuleFn]:
    def decorate(fn: RuleFn) -> RuleFn:
        _REGISTRY[code] = fn
        _DIRECTIONS[code] = directions
        return fn

    return decorate


def applies_to(code: str) -> tuple[str, ...]:
    return _DIRECTIONS[code]


# ---------------------------------------------------------------------------
# ESCALATE — raise the band
# ---------------------------------------------------------------------------


@rule("VELOCITY_BURST_1H")
def velocity_burst_1h(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """A burst of payments in an hour, going somewhere brand new.

    Counts attempts, not successes: an attacker hitting a limit and retrying is
    the same burst, and filtering to approvals would hide the loudest part of it.

    D67: speed alone is what traders and shop owners do all day. Speed *to a
    destination that first appeared in the bank's traffic within the last day*
    is how a taken-over account is emptied and how a mule ring spreads money.
    With ``new_destination_days`` set, the rule only fires when this payment's
    destination is that fresh. A payment with no destination (card, cash)
    never fires it on the gate; ``no_destination_min_count`` is the separate,
    higher bar for those payments, so a burst of card probes the card-testing
    rule misses is still caught (it restored 10 of 194 held-out card
    incidents for 0.7 extra false alarms a day).
    """
    threshold = int(params.get("min_count", 5))
    count = int(ctx.features.get("txn_count_1h_account", 0))
    if count < threshold:
        return None
    evidence: dict[str, Any] = {"count": count, "threshold": threshold, "window": "1h"}
    fresh = params.get("new_destination_days")
    if fresh is not None:
        first_seen = float(ctx.features.get("beneficiary_first_seen_days", -1.0))
        if first_seen < 0:
            # -1 is NOT_APPLICABLE: no destination (card, cash). It must never
            # read as "new"; these payments face their own, higher count.
            bar = params.get("no_destination_min_count")
            if bar is None or count < int(bar):
                return None
            evidence["no_destination_threshold"] = int(bar)
        elif first_seen < float(fresh):
            evidence["destination_first_seen_days"] = round(first_seen, 2)
            evidence["new_destination_days"] = fresh
        else:
            return None
    return Signal(
        code="VELOCITY_BURST_1H",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence=evidence,
    )


@rule("ACCOUNT_TAKEOVER_SEQUENCE")
def account_takeover_sequence(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """Money to a new destination, shortly after someone took over the way in (D77).

    Account takeover is decided before the first transfer. The attacker guesses
    the password or swaps the SIM, binds their own phone, changes the PIN, then
    pays someone the owner has never paid. Any one of those precursors happens
    to ordinary customers all the time; followed within hours by a payment to a
    destination new to the account, it is the takeover sequence.

    Every precursor is a feature read from events, so the evidence shows exactly
    which ones were present. Without a destination, or to a payee the account
    has paid before, the rule stays silent: the owner's own habits are not the
    attack.
    """
    if float(ctx.features.get("beneficiary_is_new_to_account", 0.0)) != 1.0:
        return None
    within = float(params.get("within_hours", 24))
    min_failed = int(params.get("min_failed_logins", 3))

    precursors: dict[str, float] = {}
    device = float(ctx.features.get("device_bound_hours", -1.0))
    if 0 <= device < within:
        precursors["device_bound_hours"] = round(device, 2)
    for name in ("sim_changed_hours", "credential_changed_hours"):
        hours = float(ctx.features.get(name, within))
        if 0 <= hours < within:
            precursors[name] = round(hours, 2)
    failed = int(ctx.features.get("failed_logins_1h_subject", 0))
    if failed >= min_failed:
        precursors["failed_logins_1h_subject"] = failed
    if len(precursors) < int(params.get("min_precursors", 1)):
        return None
    return Signal(
        code="ACCOUNT_TAKEOVER_SEQUENCE",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"precursors": precursors, "within_hours": within, "min_failed_logins": min_failed,
                  "payee_added_minutes": round(float(ctx.features.get("payee_added_minutes", -1.0)), 1)},
    )


@rule("MULE_INBOUND_FANIN", directions=("INBOUND",))
def mule_inbound_fanin(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """Money arriving from many different senders, on a day unlike the account's normal (D78).

    The receiving side of a mule ring: several victims, at other banks, pay one
    account within hours. A trader also takes many senders a day, every day, so
    the count of senders is judged together with the account's own normal: the
    same inflow that is Tuesday for a market stall is an event for a salary
    account.
    """
    min_senders = int(params.get("min_remitters", 3))
    min_ratio = float(params.get("min_count_ratio", 5.0))
    senders = int(ctx.features.get("distinct_remitters_24h_account", 0))
    ratio = float(ctx.features.get("inbound_count_ratio_24h_vs_daily_mean_30d", 0.0))
    if senders < min_senders or ratio < min_ratio:
        return None
    return Signal(
        code="MULE_INBOUND_FANIN",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"distinct_remitters_24h": senders, "inbound_count_ratio_vs_normal_day": round(ratio, 2),
                  "min_remitters": min_senders, "min_count_ratio": min_ratio,
                  "credits_24h": int(ctx.features.get("credits_24h_account", 0))},
    )


@rule("SECOND_LEG_ONWARD_PAYMENT")
def second_leg_onward_payment(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """The payment that moves received money on (D79, WP-04).

    The receiving-side playbook's point: the recoverable moment is the onward
    payment, not the credit. A credit fan-in (``MULE_INBOUND_FANIN``) says an
    account is collecting; this says the collection is now leaving, within hours
    of arriving, and a hold here keeps the money in the bank. It reads the same
    receiving-side profile the credit rule reads, from the payment's own
    features, so the pair lands in one case (D13a) and the directive on this
    payment carries the hold.
    """
    senders = int(ctx.features.get("distinct_remitters_24h_account", 0))
    ratio = float(ctx.features.get("inbound_count_ratio_24h_vs_daily_mean_30d", 0.0))
    since = float(ctx.features.get("minutes_since_last_credit", 1440.0))
    through = float(ctx.features.get("pass_through_ratio_24h", 0.0))
    if (senders < int(params.get("min_remitters", 3)) or ratio < float(params.get("min_count_ratio", 5.0))
            or since > float(params.get("max_minutes_since_credit", 180)) or through < float(params.get("min_pass_through", 0.5))):
        return None
    return Signal(
        code="SECOND_LEG_ONWARD_PAYMENT",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"distinct_remitters_24h": senders, "inbound_count_ratio_vs_normal_day": round(ratio, 2),
                  "minutes_since_last_credit": round(since, 1), "pass_through_ratio_24h": round(through, 3),
                  "max_minutes_since_credit": float(params.get("max_minutes_since_credit", 180)),
                  "min_pass_through": float(params.get("min_pass_through", 0.5))},
    )


@rule("CARD_TESTING_PROBES")
def card_testing_probes(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """Decline-heavy, low-value card activity — the probing shape.

    This rule only exists because the event model carries ``auth_result`` (D21).
    Before that field, the pattern was literally unrepresentable: declines never
    reach a ledger, and a run of tiny *approved* transactions is a different
    thing wearing the same costume.
    """
    if ctx.tx.instrument != "CARD":
        return None
    min_rate = float(params.get("min_decline_rate_24h", 0.5))
    min_failed = int(params.get("min_failed_1h", 3))

    rate = float(ctx.features.get("decline_rate_24h_account", 0.0))
    failed = int(ctx.features.get("failed_attempts_1h_account", 0))
    if rate < min_rate or failed < min_failed:
        return None
    return Signal(
        code="CARD_TESTING_PROBES",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={
            "decline_rate_24h": round(rate, 4),
            "failed_attempts_1h": failed,
            "min_decline_rate_24h": min_rate,
            "min_failed_1h": min_failed,
        },
    )


# ---------------------------------------------------------------------------
# D82 — four fraud types a Nigerian desk sees that the first three did not cover
# ---------------------------------------------------------------------------


@rule("SCAM_BENEFICIARY_FANIN")
def scam_beneficiary_fanin(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """A payment to a destination several other customers of ours paid today, that is new.

    Social engineering is the fraud Nigerian banks report most: a caller posing
    as the bank, a regulator or a relative, or an "investment", talks the real
    customer into sending the money themselves, from their own phone. Nothing
    about the session is wrong, so no takeover signal can see it. What the scam
    cannot hide is its collection account: many victims pay it within a day or
    two, and it did not exist in our traffic last week.

    A school, a church levy or a popular vendor is also paid by many customers,
    which is why the destination must be new, and new to this customer.
    """
    if float(ctx.features.get("beneficiary_is_new_to_account", 0.0)) != 1.0:
        return None
    others = float(ctx.features.get("beneficiary_distinct_senders_24h", -1.0))
    first_seen = float(ctx.features.get("beneficiary_first_seen_days", -1.0))
    min_others = int(params.get("min_other_senders", 2))
    max_age = float(params.get("max_beneficiary_age_days", 7))
    if others < min_others or first_seen < 0 or first_seen > max_age:
        return None
    min_amount = params.get("min_amount_log10")
    amount = float(ctx.features.get("amount_log10", 0.0))
    if min_amount is not None and amount < float(min_amount):
        return None
    # Persuaded victims send more than they usually do; a school fee is ordinary.
    min_ratio = params.get("min_amount_ratio")
    ratio = float(ctx.features.get("amount_ratio_to_account_p95_30d", 0.0))
    if min_ratio is not None and ratio < float(min_ratio):
        return None
    return Signal(
        code="SCAM_BENEFICIARY_FANIN",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"other_customers_paying_it_24h": int(others), "destination_first_seen_days": round(first_seen, 2),
                  "min_other_senders": min_others, "max_beneficiary_age_days": max_age,
                  "payee_added_minutes": round(float(ctx.features.get("payee_added_minutes", -1.0)), 1)},
    )


@rule("SIM_SWAP_TRANSFER")
def sim_swap_transfer(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """Money to a new destination within hours of the customer's SIM changing.

    A SIM swap hands the attacker the OTPs and the USSD session. On USSD there
    is no app to bind and often no password to guess, so the takeover sequence
    (which wants two precursors) sees one; this rule is that one precursor, on
    its own, followed by a destination the account has never paid. People do
    replace lost SIMs, so the window is short and the destination must be new.
    """
    if float(ctx.features.get("beneficiary_is_new_to_account", 0.0)) != 1.0:
        return None
    within = float(params.get("within_hours", 12))
    hours = float(ctx.features.get("sim_changed_hours", within))
    if not 0 <= hours < within:
        return None
    channels = params.get("channels")
    if channels and ctx.tx.channel not in channels:
        return None
    min_amount = params.get("min_amount_log10")
    if min_amount is not None and float(ctx.features.get("amount_log10", 0.0)) < float(min_amount):
        return None
    min_ratio = params.get("min_amount_ratio")
    if min_ratio is not None and float(ctx.features.get("amount_ratio_to_account_p95_30d", 0.0)) < float(min_ratio):
        return None
    return Signal(
        code="SIM_SWAP_TRANSFER",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"sim_changed_hours": round(hours, 2), "within_hours": within, "channel": ctx.tx.channel,
                  "hour_of_day_local": int(ctx.features.get("hour_of_day_local", -1))},
    )


@rule("DORMANT_ACCOUNT_REACTIVATION")
def dormant_account_reactivation(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """An account quiet for months, suddenly sending a large sum somewhere new.

    Dormant accounts are drained from inside and outside: a changed phone number
    on a forgotten account, a transfer nobody is watching for. Owners also come
    back, so the sum must be large and the destination new to the account.
    """
    quiet = float(ctx.features.get("days_since_account_activity", -1.0))
    min_quiet = float(params.get("min_dormant_days", 60))
    if quiet < min_quiet:
        return None
    amount = float(ctx.features.get("amount_log10", 0.0))
    min_amount = float(params.get("min_amount_log10", 5.0))
    if amount < min_amount:
        return None
    if params.get("require_new_destination", True) and ctx.tx.beneficiary_token \
            and float(ctx.features.get("beneficiary_is_new_to_account", 0.0)) != 1.0:
        return None
    return Signal(
        code="DORMANT_ACCOUNT_REACTIVATION",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"days_since_account_activity": round(quiet, 1), "min_dormant_days": min_quiet,
                  "amount_naira": round(10 ** amount - 1), "min_amount_log10": min_amount,
                  "credential_changed_hours": round(float(ctx.features.get("credential_changed_hours", -1.0)), 1)},
    )


@rule("CARD_PRESENT_NEW_REGION_CASHOUT")
def card_present_new_region_cashout(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """The card at terminal after terminal within the hour, in a region its owner has not used.

    A skimmed or stolen card is emptied at ATMs, POS terminals and agents before
    the owner notices, usually away from home. A traveller uses a card away from
    home too, once or twice; the run of attempts is what separates them.
    """
    if ctx.tx.instrument != "CARD" or ctx.tx.channel not in ("POS", "ATM", "AGENT"):
        return None
    if float(ctx.features.get("region_is_new_to_subject", -1.0)) != 1.0:
        return None
    count = int(ctx.features.get("card_present_count_1h_account", 0))
    min_count = int(params.get("min_count_1h", 3))
    if count < min_count:
        return None
    return Signal(
        code="CARD_PRESENT_NEW_REGION_CASHOUT",
        power="ESCALATE",
        severity=params.get("severity", "HIGH"),
        evidence={"card_present_attempts_1h": count, "min_count_1h": min_count, "region": ctx.tx.ip_region,
                  "hour_of_day_local": int(ctx.features.get("hour_of_day_local", -1))},
    )


# ---------------------------------------------------------------------------
# OVERRIDE — deterministic veto. The model gets no vote.
# ---------------------------------------------------------------------------


@rule("SANCTIONED_BENEFICIARY")
def sanctioned_beneficiary(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """A sanctions hit is not a probability, and no calibration argument applies."""
    if not ctx.sanctioned:
        return None
    return Signal(
        code="SANCTIONED_BENEFICIARY",
        power="OVERRIDE",
        severity="CRITICAL",
        evidence={"list": "SANCTIONED", "beneficiary_token": ctx.tx.beneficiary_token},
    )


@rule("KNOWN_MULE_BENEFICIARY")
def known_mule_beneficiary(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """A destination an analyst already confirmed as fraudulent (D13c).

    This is the loop closing: case outcomes are training signal *and* immediate
    operational control. The second transfer to a known mule should not have to
    re-convince a model.
    """
    if not ctx.known_mule:
        return None
    return Signal(
        code="KNOWN_MULE_BENEFICIARY",
        power="OVERRIDE",
        severity="CRITICAL",
        evidence={"list": "KNOWN_MULE", "beneficiary_token": ctx.tx.beneficiary_token},
    )


# ---------------------------------------------------------------------------
# SUPPRESS — the primary false-positive control (D11a). Built, never deferred.
# ---------------------------------------------------------------------------


@rule("PRE_REGISTERED_BENEFICIARY")
def pre_registered_beneficiary(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """The customer deliberately set this payee up on this account.

    Suppression runs *after* escalation in the policy layer precisely so this can
    pull back a velocity flag: paying your own landlord six times in an hour is
    unusual and not suspicious.
    """
    if not ctx.pre_registered:
        return None
    return Signal(
        code="PRE_REGISTERED_BENEFICIARY",
        power="SUPPRESS",
        severity="LOW",
        evidence={"list": "ALLOWLIST", "scope": "account"},
    )


@rule("ESTABLISHED_PAYEE_NORMAL")
def established_payee_normal(ctx: RuleContext, params: dict[str, Any]) -> Signal | None:
    """A long-standing destination receiving an ordinary amount.

    Both conditions are required. A familiar payee receiving ten times the usual
    amount is exactly the shape of a compromised session reusing a real payee, so
    familiarity alone must never suppress.
    """
    min_age = float(params.get("min_beneficiary_age_days", 60))
    max_ratio = float(params.get("max_amount_ratio", 1.0))

    # D64: no payee, no "established payee". The feature fix already makes this
    # unreachable; the guard means a future feature bug cannot quietly bring
    # back suppression on card and cash payments.
    if not ctx.tx.beneficiary_token:
        return None
    if float(ctx.features.get("beneficiary_is_new_to_account", 1.0)) != 0.0:
        return None
    age = float(ctx.features.get("beneficiary_first_seen_days", 0.0))
    ratio = float(ctx.features.get("amount_ratio_to_account_p95_30d", 99.0))
    if age < min_age or ratio > max_ratio:
        return None
    return Signal(
        code="ESTABLISHED_PAYEE_NORMAL",
        power="SUPPRESS",
        severity="LOW",
        evidence={
            "beneficiary_first_seen_days": round(age, 2),
            "amount_ratio_to_account_p95_30d": round(ratio, 4),
            "min_beneficiary_age_days": min_age,
            "max_amount_ratio": max_ratio,
        },
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

ALL_RULE_CODES: tuple[str, ...] = (
    "VELOCITY_BURST_1H",
    "ACCOUNT_TAKEOVER_SEQUENCE",
    "MULE_INBOUND_FANIN",
    "SECOND_LEG_ONWARD_PAYMENT",
    "CARD_TESTING_PROBES",
    "SCAM_BENEFICIARY_FANIN",
    "SIM_SWAP_TRANSFER",
    "DORMANT_ACCOUNT_REACTIVATION",
    "CARD_PRESENT_NEW_REGION_CASHOUT",
    "SANCTIONED_BENEFICIARY",
    "KNOWN_MULE_BENEFICIARY",
    "PRE_REGISTERED_BENEFICIARY",
    "ESTABLISHED_PAYEE_NORMAL",
)

assert set(ALL_RULE_CODES) == set(_REGISTRY), "rule registry disagrees with the catalogue"


def evaluate(ctx: RuleContext, configs: dict[str, dict[str, Any]]) -> list[Signal]:
    """Run every enabled rule.

    ``configs`` is the active ruleset's per-rule configuration, loaded from
    ``rule_configs`` — so enabling, disabling and retuning is administration
    (FR-040) and audited (FR-041), not a deploy.
    """
    signals: list[Signal] = []
    for code in ALL_RULE_CODES:
        config = configs.get(code)
        if not config or not config.get("enabled", True):
            continue
        if ctx.tx.direction not in _DIRECTIONS[code]:
            continue
        signal = _REGISTRY[code](ctx, config.get("params") or {})
        if signal is not None:
            signals.append(signal)
    return signals

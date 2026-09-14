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
CARD_TESTING_PROBES          ESCALATE   Authorisation probing — decline-heavy by nature
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


def rule(code: str) -> Callable[[RuleFn], RuleFn]:
    def decorate(fn: RuleFn) -> RuleFn:
        _REGISTRY[code] = fn
        return fn

    return decorate


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
    "CARD_TESTING_PROBES",
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
        signal = _REGISTRY[code](ctx, config.get("params") or {})
        if signal is not None:
            signals.append(signal)
    return signals

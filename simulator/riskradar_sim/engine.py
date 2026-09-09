"""Event generation: ordinary life, and three criminal processes.

D10 — the label is "emitted by a fraud process", never "crossed a threshold".
Every fraud event here is produced by a state machine modelling how the crime
actually unfolds. Detection sees only the shadow.

D17 — three typologies, chosen to be **structurally distinct**, which is the
precondition for held-out-typology evaluation meaning anything:

* **Account takeover** — compromise, new device, reconnaissance, dormancy, then
  burst extraction across the victim's *own accounts* and out to fresh payees.
* **Mule fan-out** — one account to many fresh beneficiaries within minutes.
* **Card testing** — low-value authorisation probes, mostly declined, preceding
  a large authorisation.

Note what the third one needs: ``auth_result``. Before D21 added it, card
testing was literally unrepresentable and would have had to be faked as a run of
tiny *approved* transactions — which is a different phenomenon wearing the same
costume, and a model trained on it learns the wrong thing.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

from .population import IP_REGIONS, MERCHANT_CATEGORIES, Account, Customer

# Channel mix for ordinary retail activity in this market.
CHANNELS = ["MOBILE_APP", "USSD", "WEB", "POS", "ATM", "AGENT", "BRANCH"]
CHANNEL_WEIGHTS = [0.44, 0.20, 0.07, 0.15, 0.08, 0.05, 0.01]

DECLINE_REASONS = [
    "INSUFFICIENT_FUNDS", "LIMIT_EXCEEDED", "INVALID_PIN", "DO_NOT_HONOUR", "TIMEOUT",
]


@dataclass
class Event:
    """One generated transaction.

    ``is_fraud``, ``typology`` and ``incident_id`` exist **only** in the offline
    corpus used for training and evaluation. They are not part of the ingestion
    contract and are never sent to the API — the API has no field for them. That
    is the generator/detector wall expressed in the transport itself.
    """

    payload: dict
    is_fraud: bool = False
    typology: str | None = None
    incident_id: str | None = None


def _rails_for(channel: str, instrument: str, rng: random.Random) -> str:
    if instrument == "CARD":
        return "CARD_SCHEME" if channel in ("POS", "WEB") else "ATM_NETWORK"
    if channel == "ATM":
        return "ATM_NETWORK"
    return rng.choices(["NIP", "INTRABANK", "NEFT", "RTGS"], [0.68, 0.22, 0.07, 0.03])[0]


def _instrument_for(channel: str, rng: random.Random) -> str:
    if channel in ("POS", "ATM"):
        return "CARD"
    if channel == "AGENT":
        return rng.choices(["CASH", "ACCOUNT_TRANSFER"], [0.6, 0.4])[0]
    if channel == "BRANCH":
        return rng.choices(["CASH", "ACCOUNT_TRANSFER"], [0.5, 0.5])[0]
    return rng.choices(["ACCOUNT_TRANSFER", "WALLET"], [0.88, 0.12])[0]


def _base_payload(
    customer: Customer,
    account: Account,
    when: datetime,
    *,
    amount_minor: int,
    channel: str,
    instrument: str,
    rail: str,
    device: str | None,
    beneficiary: str | None,
    region: str,
    auth_result: str = "APPROVED",
    decline_reason: str | None = None,
    merchant_category: str | None = None,
) -> dict:
    return {
        "transaction_ref": f"TX{uuid.uuid4().hex[:20].upper()}",
        "occurred_at": when.isoformat(),
        "amount_minor": amount_minor,
        "currency": "NGN",
        "channel": channel,
        "instrument": instrument,
        "rail": rail,
        "customer_id": customer.customer_id,
        "account_id": account.account_id,
        "beneficiary_account_id": beneficiary,
        "device_fingerprint": device,
        "ip_region": region,
        "merchant_category": merchant_category,
        "auth_result": auth_result,
        "decline_reason": decline_reason,
        "display_name": customer.display_name,
        # D22: point-in-time account context, stamped at the boundary.
        "account_opened_at": account.opened_at.isoformat(),
        "last_activity_at": (
            account.last_activity_at.isoformat() if account.last_activity_at else None
        ),
        "product_type": account.product_type,
        "origin_sol_id": account.origin_sol_id,
    }


# ---------------------------------------------------------------------------
# Ordinary life
# ---------------------------------------------------------------------------


def legitimate_event(
    rng: random.Random, customer: Customer, account: Account, when: datetime
) -> Event:
    channel = rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0]
    instrument = _instrument_for(channel, rng)
    rail = _rails_for(channel, instrument, rng)

    # Naira amounts with a realistic long tail: airtime and small POS dominate,
    # rent and salary sit far out on the right.
    bucket = rng.choices(["micro", "small", "medium", "large"], [0.34, 0.40, 0.21, 0.05])[0]
    naira = {
        "micro": lambda: rng.uniform(100, 3_000),
        "small": lambda: rng.uniform(3_000, 40_000),
        "medium": lambda: rng.uniform(40_000, 250_000),
        "large": lambda: rng.uniform(250_000, 2_500_000),
    }[bucket]()

    beneficiary = None
    if instrument in ("ACCOUNT_TRANSFER", "WALLET"):
        # Mostly a payee they already use; occasionally somebody new, because
        # real people do pay new people and a model that treats novelty alone as
        # fraud would drown the queue.
        if rng.random() < 0.82 and customer.known_beneficiaries:
            beneficiary = rng.choice(customer.known_beneficiaries)
        else:
            beneficiary = f"BEN{uuid.uuid4().hex[:12].upper()}"
            customer.known_beneficiaries.append(beneficiary)

    # Ordinary friction: cards get declined, transfers time out, people mistype
    # PINs. Without this the model would learn that any decline at all is fraud.
    auth_result, reason = "APPROVED", None
    if rng.random() < 0.06:
        auth_result = rng.choices(["DECLINED", "FAILED", "REVERSED"], [0.6, 0.3, 0.1])[0]
        if auth_result != "REVERSED":
            reason = rng.choice(DECLINE_REASONS)

    region = customer.home_region if rng.random() < 0.93 else rng.choice(IP_REGIONS)

    return Event(
        _base_payload(
            customer,
            account,
            when,
            amount_minor=int(round(naira * 100)),
            channel=channel,
            instrument=instrument,
            rail=rail,
            device=rng.choice(customer.devices) if channel in ("MOBILE_APP", "WEB", "USSD") else None,
            beneficiary=beneficiary,
            region=region,
            auth_result=auth_result,
            decline_reason=reason,
            merchant_category=rng.choice(MERCHANT_CATEGORIES) if channel == "POS" else None,
        )
    )


# ---------------------------------------------------------------------------
# Typology 1 — account takeover
# ---------------------------------------------------------------------------


def account_takeover(
    rng: random.Random, customer: Customer, start: datetime
) -> Iterator[Event]:
    """Compromise -> new device -> reconnaissance -> pause -> extraction.

    The extraction deliberately moves across **more than one of the victim's own
    accounts** where they have them. That is the shape D19 exists to correlate:
    without a customer-level subject this single incident would open one case per
    account.
    """
    incident = f"ATO-{uuid.uuid4().hex[:10]}"
    attacker_device = f"DEV{uuid.uuid4().hex[:12]}"
    attacker_region = rng.choice([r for r in IP_REGIONS if r != customer.home_region])
    now = start

    # 1. Reconnaissance: small probes from the new device. Some fail — the
    #    attacker is guessing at limits and balances.
    for _ in range(rng.randint(2, 5)):
        account = rng.choice(customer.accounts)
        failed = rng.random() < 0.35
        yield Event(
            _base_payload(
                customer, account, now,
                amount_minor=int(rng.uniform(100, 2_000) * 100),
                channel=rng.choice(["MOBILE_APP", "USSD"]),
                instrument="ACCOUNT_TRANSFER",
                rail="NIP",
                device=attacker_device,
                beneficiary=rng.choice(customer.known_beneficiaries) if customer.known_beneficiaries else None,
                region=attacker_region,
                auth_result="DECLINED" if failed else "APPROVED",
                decline_reason="INVALID_PIN" if failed else None,
            ),
            is_fraud=True, typology="ACCOUNT_TAKEOVER", incident_id=incident,
        )
        now += timedelta(minutes=rng.uniform(2, 14))

    # 2. Dormancy. The attacker waits — sometimes for hours — which is exactly
    #    what defeats a naive fixed-window velocity rule.
    now += timedelta(minutes=rng.uniform(25, 240))

    # 3. Extraction: escalating transfers to fresh destinations, across accounts.
    mule_accounts = [f"BEN{uuid.uuid4().hex[:12].upper()}" for _ in range(rng.randint(2, 5))]
    escalation = rng.uniform(1.15, 1.55)
    naira = rng.uniform(40_000, 180_000)

    for i in range(rng.randint(4, 9)):
        account = customer.accounts[min(i // 3, len(customer.accounts) - 1)]
        # Hitting a limit is part of the process, not an accident.
        blocked = rng.random() < 0.22
        yield Event(
            _base_payload(
                customer, account, now,
                amount_minor=int(naira * 100),
                channel="MOBILE_APP",
                instrument="ACCOUNT_TRANSFER",
                rail="NIP",
                device=attacker_device,
                beneficiary=rng.choice(mule_accounts),
                region=attacker_region,
                auth_result="DECLINED" if blocked else "APPROVED",
                decline_reason=rng.choice(["LIMIT_EXCEEDED", "INSUFFICIENT_FUNDS"]) if blocked else None,
            ),
            is_fraud=True, typology="ACCOUNT_TAKEOVER", incident_id=incident,
        )
        if not blocked:
            naira *= escalation
        now += timedelta(minutes=rng.uniform(1.5, 11))


# ---------------------------------------------------------------------------
# Typology 2 — mule network fan-out
# ---------------------------------------------------------------------------


def mule_fanout(rng: random.Random, customer: Customer, start: datetime) -> Iterator[Event]:
    """One account pushing funds to many fresh beneficiaries within minutes.

    Amounts sit just under round numbers, which is a habit rather than a rule:
    the generator never reads a threshold, it models a person trying not to look
    round.
    """
    incident = f"MULE-{uuid.uuid4().hex[:10]}"
    account = customer.primary
    device = rng.choice(customer.devices)
    now = start

    for _ in range(rng.randint(6, 15)):
        naira = rng.choice([49_500, 99_000, 148_500, 199_000, 245_000]) * rng.uniform(0.95, 1.05)
        failed = rng.random() < 0.12
        yield Event(
            _base_payload(
                customer, account, now,
                amount_minor=int(naira * 100),
                channel=rng.choices(["MOBILE_APP", "WEB", "USSD"], [0.7, 0.2, 0.1])[0],
                instrument="ACCOUNT_TRANSFER",
                rail=rng.choices(["NIP", "INTRABANK"], [0.85, 0.15])[0],
                device=device,
                beneficiary=f"BEN{uuid.uuid4().hex[:12].upper()}",  # fresh every time
                region=customer.home_region,
                auth_result="FAILED" if failed else "APPROVED",
                decline_reason="TIMEOUT" if failed else None,
            ),
            is_fraud=True, typology="MULE_FANOUT", incident_id=incident,
        )
        now += timedelta(seconds=rng.uniform(25, 220))


# ---------------------------------------------------------------------------
# Typology 3 — card testing
# ---------------------------------------------------------------------------


def card_testing(rng: random.Random, customer: Customer, start: datetime) -> Iterator[Event]:
    """Low-value authorisation probes, mostly declined, then the real charge.

    This typology exists at the **switch**, not the core (§9.1): a declined
    authorisation never posts to a ledger. It is representable here only because
    the canonical event carries ``auth_result`` (D21).
    """
    incident = f"CARD-{uuid.uuid4().hex[:10]}"
    account = rng.choice(customer.accounts)
    now = start
    region = rng.choice(IP_REGIONS)

    for _ in range(rng.randint(8, 22)):
        declined = rng.random() < 0.78
        yield Event(
            _base_payload(
                customer, account, now,
                amount_minor=int(rng.uniform(50, 900) * 100),
                channel=rng.choices(["WEB", "POS"], [0.8, 0.2])[0],
                instrument="CARD",
                rail="CARD_SCHEME",
                device=None,
                beneficiary=None,
                region=region,
                auth_result="DECLINED" if declined else "APPROVED",
                decline_reason=rng.choice(["DO_NOT_HONOUR", "INVALID_PIN"]) if declined else None,
                merchant_category=rng.choice(MERCHANT_CATEGORIES),
            ),
            is_fraud=True, typology="CARD_TESTING", incident_id=incident,
        )
        now += timedelta(seconds=rng.uniform(8, 90))

    # The payoff: one or two large authorisations once a live card is found.
    for _ in range(rng.randint(1, 2)):
        yield Event(
            _base_payload(
                customer, account, now,
                amount_minor=int(rng.uniform(180_000, 900_000) * 100),
                channel="WEB",
                instrument="CARD",
                rail="CARD_SCHEME",
                device=None,
                beneficiary=None,
                region=region,
                merchant_category=rng.choice(MERCHANT_CATEGORIES),
            ),
            is_fraud=True, typology="CARD_TESTING", incident_id=incident,
        )
        now += timedelta(minutes=rng.uniform(1, 6))


TYPOLOGIES = {
    "ACCOUNT_TAKEOVER": account_takeover,
    "MULE_FANOUT": mule_fanout,
    "CARD_TESTING": card_testing,
}

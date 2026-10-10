"""Money coming in: salaries, transfers from family, a shop's takings, a mule's receipts (D78).

Plain English
-------------
Until now the simulated bank only watched money leave. Real accounts also
receive: a salary on the 25th, a sister sending school fees, forty customers
paying a trader for tomatoes. And a mule account, before it fans money out,
first takes in several victims' payments from other banks within a few hours.

This layer writes those credits around the payments the simulator already
produces. Like the event layer (D77), it draws from its **own** random stream
and never changes a payment or an event, so the payment corpus and the event
corpus a seed produced before this file existed are the ones it produces now.

The honest part is the legitimate fan-in. A market trader receives from many
different senders every day; a rule that fires on "many senders" alone would
drown the desk in traders. What separates a mule is that the inflow is unlike
the account's own normal day, which is only visible because the traders are
here (D57).
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta

from .engine import Event
from .population import Customer

# Nigerian bank codes for the institutions money arrives from.
BANK_CODES = ["044", "058", "011", "033", "057", "232", "035", "214", "070", "076"]

# Ordinary person-to-person credits, per customer per day.
P2P_RATE = 0.06
# A trader's and a small business's daily takings, from many senders.
TRADER_CREDITS = (6, 20)
SMALL_BUSINESS_CREDITS = (2, 8)
# A mule's receipts before the fan-out; some rings use one or two large senders.
MULE_SENDERS = (3, 10)
MULE_FEW_SENDERS_SHARE = 0.15


class CreditLayer:
    def __init__(self, seed: int, customers: list[Customer]) -> None:
        self.rng = random.Random(f"riskradar-credits:{seed}")
        self.customers = customers
        # Everyone has a few people who send them money, and an employer.
        self.family: dict[str, list[str]] = {
            c.customer_id: [self._account() for _ in range(self.rng.randint(2, 6))] for c in customers
        }
        self.employer: dict[str, str] = {c.customer_id: self._account() for c in customers}
        self.payday: dict[str, int] = {c.customer_id: self.rng.randint(24, 28) for c in customers}
        # A trader's regular customers come back; new ones arrive.
        self.regulars: dict[str, list[str]] = defaultdict(list)

    def _account(self) -> str:
        return f"RMT{self.rng.getrandbits(48):012X}"

    def _credit(self, customer: Customer, when: datetime, amount_naira: float, remitter: str,
                fraud: Event | None = None) -> Event:
        account = customer.primary
        failed = self.rng.random() < 0.01
        payload = {
            "transaction_ref": f"CR{self.rng.getrandbits(80):020X}",
            "occurred_at": when.isoformat(),
            "amount_minor": int(amount_naira * 100),
            "currency": "NGN",
            "channel": "API",
            "instrument": "ACCOUNT_TRANSFER",
            "rail": "NIP",
            "customer_id": customer.customer_id,
            "account_id": account.account_id,
            "beneficiary_account_id": None,
            "device_fingerprint": None,
            "ip_region": None,
            "merchant_category": None,
            "auth_result": "FAILED" if failed else "APPROVED",
            "decline_reason": "TIMEOUT" if failed else None,
            "display_name": customer.display_name,
            "account_opened_at": account.opened_at.isoformat(),
            "last_activity_at": account.last_activity_at.isoformat() if account.last_activity_at else None,
            "product_type": account.product_type,
            "origin_sol_id": account.origin_sol_id,
            "direction": "INBOUND",
            "remitter_account_id": remitter,
            "remitter_bank_code": self.rng.choice(BANK_CODES),
        }
        return Event(payload, is_fraud=bool(fraud and fraud.is_fraud),
                     typology=fraud.typology if fraud else None,
                     incident_id=fraud.incident_id if fraud else None, kind="CREDIT")

    def _at(self, customer: Customer, day: datetime) -> datetime:
        lo, hi = customer.active_hours
        return day.replace(hour=self.rng.randint(lo, min(hi, 23)), minute=self.rng.randint(0, 59),
                           second=self.rng.randint(0, 59), microsecond=0)

    def for_day(self, day: datetime, payments: list[Event], by_id: dict[str, Customer]) -> list[Event]:
        out: list[Event] = []
        for customer in self.customers:
            cid = customer.customer_id
            if customer.archetype == "salary_earner" and day.day == self.payday[cid]:
                out.append(self._credit(customer, day.replace(hour=self.rng.randint(6, 10), minute=self.rng.randint(0, 59)),
                                        self.rng.uniform(150_000, 900_000), self.employer[cid]))
            if self.rng.random() < P2P_RATE:
                for _ in range(self.rng.randint(1, 2)):
                    out.append(self._credit(customer, self._at(customer, day), self.rng.uniform(2_000, 80_000),
                                            self.rng.choice(self.family[cid])))
            spread = {"trader": TRADER_CREDITS, "small_business": SMALL_BUSINESS_CREDITS}.get(customer.archetype)
            if spread:
                for _ in range(self.rng.randint(*spread)):
                    regulars = self.regulars[cid]
                    if regulars and self.rng.random() < 0.55:
                        sender = self.rng.choice(regulars)
                    else:
                        sender = self._account()
                        if len(regulars) < 60:
                            regulars.append(sender)
                    out.append(self._credit(customer, self._at(customer, day), self.rng.uniform(1_000, 50_000), sender))

        incidents: dict[str, list[Event]] = defaultdict(list)
        for pay in payments:
            if pay.typology == "MULE_FANOUT":
                incidents[pay.incident_id].append(pay)
        for events in incidents.values():
            out.extend(self.mule_receipts(events, by_id))
        return out

    def mule_receipts(self, events: list[Event], by_id: dict[str, Customer]) -> list[Event]:
        """Victims' money lands in the mule account in the hours before it fans out."""
        first = min(events, key=lambda e: e.payload["occurred_at"])
        customer = by_id[first.payload["customer_id"]]
        start = datetime.fromisoformat(first.payload["occurred_at"])
        few = self.rng.random() < MULE_FEW_SENDERS_SHARE
        senders = self.rng.randint(1, 2) if few else self.rng.randint(*MULE_SENDERS)
        out = []
        for _ in range(senders):
            when = start - timedelta(minutes=self.rng.uniform(5, 180))
            amount = self.rng.uniform(300_000, 1_500_000) if few else self.rng.uniform(40_000, 400_000)
            out.append(self._credit(customer, when, amount, self._account(), fraud=first))
        return out

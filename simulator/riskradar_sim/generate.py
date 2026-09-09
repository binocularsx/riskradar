"""Corpus generation and live streaming.

Plain English
-------------
Runs the invented population forward through a number of days and produces the
transactions they make.

Two modes matter. It can write everything to a file for training the model, and
that file contains the answers — which transactions were fraudulent and which
criminal story produced them. Or it can post transactions to the running system
through exactly the same public door a real bank would use, in which case the
answers are *not* sent, because the live system has nowhere to put them and is
not supposed to know.

The simulator is a **pure external client** (D8). It has no database handle, no
import of the product's detection code, and it reaches Risk Radar only through
``POST /v1/transactions`` — the same door a bank would use. Deleting this
package would leave the product working.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .engine import TYPOLOGIES, Event, legitimate_event
from .population import Customer, build_population


@dataclass
class SimulationConfig:
    """The reference operating point (D23), scaled by ``--profile``."""

    n_customers: int = 3_000
    days: int = 30
    transactions_per_day: int = 20_000
    # ~0.3% of transactions are fraudulent, arising from a much smaller number of
    # incidents — each emitting 8-25 events (D23).
    fraud_incidents_per_day: int = 9
    seed: int = 20260909

    @classmethod
    def profile(cls, name: str) -> "SimulationConfig":
        if name == "demo":
            # Deliberately fraud-rich (~2-4%, not the 0.3% operating point): a
            # demo database needs cases in it. The honest figures come from the
            # training and reference profiles, and the QA report says so.
            return cls(n_customers=400, days=7, transactions_per_day=1_200,
                       fraud_incidents_per_day=4)
        if name == "training":
            # ~5 incidents/day x ~12 events x 30 days against 600,000 legitimate
            # transactions lands near the 0.3% fraud rate of D23, while still
            # producing ~150 incidents — roughly 50 per typology, which is the
            # floor for held-out-typology evaluation (D10b) to say anything.
            return cls(n_customers=3_000, days=30, transactions_per_day=20_000,
                       fraud_incidents_per_day=5)
        if name == "reference":
            # D23 as written. Large; used for the load test and nothing else.
            return cls(n_customers=50_000, days=14, transactions_per_day=100_000,
                       fraud_incidents_per_day=30)
        raise SystemExit(f"unknown profile {name!r} (demo | training | reference)")


def _active_hour(rng: random.Random, customer: Customer, day: datetime) -> datetime:
    """A time inside this customer's own waking hours.

    Time-of-day is a property of a person here, so "unusual hour" remains
    something the model can discover rather than something we asserted.
    """
    lo, hi = customer.active_hours
    hour = rng.randint(lo, min(hi, 23))
    return day.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59),
                       microsecond=0)


def generate(config: SimulationConfig) -> Iterator[Event]:
    """Yield events in chronological order across the configured window."""
    rng = random.Random(config.seed)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=config.days)

    customers = build_population(rng, n_customers=config.n_customers, now=start)

    # Weight account selection by its own activity rate, so quiet accounts stay
    # quiet and busy ones stay busy — the population's shape, not a scenario's.
    pairs: list[tuple[Customer, object]] = []
    weights: list[float] = []
    for customer in customers:
        for account in customer.accounts:
            pairs.append((customer, account))
            weights.append(account.daily_rate)

    for day_index in range(config.days):
        day = start + timedelta(days=day_index)
        events: list[Event] = []

        # --- ordinary life --------------------------------------------------
        for _ in range(config.transactions_per_day):
            customer, account = rng.choices(pairs, weights)[0]
            when = _active_hour(rng, customer, day)
            event = legitimate_event(rng, customer, account, when)
            account.last_activity_at = when
            events.append(event)

        # --- criminal processes ---------------------------------------------
        for _ in range(config.fraud_incidents_per_day):
            typology = rng.choice(list(TYPOLOGIES))
            # D20c: the process *selects* on attributes it never sets. An
            # attacker prefers a dormant account; the account was already
            # dormant, drawn from the population before any of this.
            if typology == "ACCOUNT_TAKEOVER":
                pool = [c for c in customers if any(a.is_naturally_dormant for a in c.accounts)]
                victim = rng.choice(pool or customers)
            else:
                victim = rng.choice(customers)

            begin = _active_hour(rng, victim, day)
            events.extend(TYPOLOGIES[typology](rng, victim, begin))

        events.sort(key=lambda e: e.payload["occurred_at"])
        yield from events


def write_corpus(config: SimulationConfig, out_path: Path) -> dict[str, int]:
    """Write JSONL for offline training and evaluation.

    Labels live **only** in this file. They are not part of the ingestion
    contract and never travel to the API.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"total": 0, "fraud": 0}
    typologies: dict[str, int] = {}
    incidents: set[str] = set()

    with out_path.open("w", encoding="utf-8") as fh:
        for event in generate(config):
            record = dict(event.payload)
            record["is_fraud"] = event.is_fraud
            record["typology"] = event.typology
            record["incident_id"] = event.incident_id
            fh.write(json.dumps(record) + "\n")
            counts["total"] += 1
            if event.is_fraud:
                counts["fraud"] += 1
                typologies[event.typology] = typologies.get(event.typology, 0) + 1
                incidents.add(event.incident_id)

    counts["incidents"] = len(incidents)
    counts.update({f"typology_{k}": v for k, v in typologies.items()})
    counts["fraud_rate_pct"] = round(100 * counts["fraud"] / max(1, counts["total"]), 3)
    return counts

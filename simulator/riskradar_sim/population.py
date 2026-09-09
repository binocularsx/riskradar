"""The synthetic customer population.

D10a — the generator/detector wall. Nothing in this package imports
``riskradar.rules``, ``riskradar.features`` or ``riskradar.policy``, and no
detection threshold appears anywhere in it. The simulator models criminal
*processes*; detection never sees the process, only its shadow. If the generator
labelled fraud by threshold — high amount, new device, velocity — the model would
learn the thresholds we already have, by hand, for free, and the 0.99 AUC would
mean nothing.

D20c — enrichment must not become a label proxy. Account age, dormancy and
product type are drawn from **population distributions before any fraud process
is assigned**. A fraud process may *select* an account using those attributes —
a real attacker does prefer a dormant account — but may never *set* them. If the
simulator marked only ATO victims dormant, dormancy would simply *be* the label
and the held-out evaluation would be worthless.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Nigerian realism reference (D10e: the rejected dataset is retained only for
# this). None of these values is read by detection code.
SOL_IDS = [
    "SOL001", "SOL014", "SOL027", "SOL033", "SOL048", "SOL052",
    "SOL061", "SOL078", "SOL090", "SOL103",
]
IP_REGIONS = [
    "NG-LA", "NG-AB", "NG-RI", "NG-KN", "NG-OY", "NG-EN", "NG-KD", "NG-DE",
]
IP_REGION_WEIGHTS = [0.34, 0.16, 0.09, 0.09, 0.11, 0.07, 0.08, 0.06]

MERCHANT_CATEGORIES = ["5411", "5812", "5541", "5912", "5732", "4900", "7995", "5999"]

FIRST_NAMES = [
    "Chidera", "Amaka", "Tunde", "Ngozi", "Femi", "Bisi", "Ifeanyi", "Zainab",
    "Emeka", "Yetunde", "Musa", "Chioma", "Segun", "Halima", "Obinna", "Folake",
]
LAST_NAMES = [
    "Okafor", "Adeyemi", "Balogun", "Nwosu", "Ibrahim", "Eze", "Ogundipe",
    "Abubakar", "Chukwu", "Lawal", "Onyeka", "Bello",
]

PRODUCTS = ["SAVINGS", "CURRENT", "DOMICILIARY", "WALLET"]
PRODUCT_WEIGHTS = [0.52, 0.34, 0.05, 0.09]


@dataclass
class Account:
    account_id: str
    product_type: str
    origin_sol_id: str
    opened_at: datetime
    # Transactions per day for this account, drawn from the population.
    # A naturally quiet account is quiet because it is quiet — not because a
    # fraud process is about to arrive (D20c).
    daily_rate: float
    last_activity_at: datetime | None = None

    @property
    def is_naturally_dormant(self) -> bool:
        return self.daily_rate < 0.08


@dataclass
class Customer:
    customer_id: str
    display_name: str
    accounts: list[Account] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    home_region: str = "NG-LA"
    # Payees this customer uses regularly — landlord, family, their own savings.
    known_beneficiaries: list[str] = field(default_factory=list)
    # Hour-of-day preference, so "unusual hour" is a real behavioural property
    # of a person rather than a rule we wrote down.
    active_hours: tuple[int, int] = (7, 22)

    @property
    def primary(self) -> Account:
        return self.accounts[0]


def build_population(
    rng: random.Random,
    *,
    n_customers: int,
    now: datetime,
) -> list[Customer]:
    """Create customers, their accounts, devices and payees.

    D19a: customers own **more than one account**. Without that, ``subject_token``
    and ``account_token`` would be 1:1 in every generated row, the distinction
    would be untestable, and we would have shipped a decorative column.
    """
    customers: list[Customer] = []

    for _ in range(n_customers):
        cid = f"CIF{uuid.uuid4().hex[:12].upper()}"
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        region = rng.choices(IP_REGIONS, IP_REGION_WEIGHTS)[0]

        # 1-3 accounts, skewed toward 2. One customer, many accounts.
        n_accounts = rng.choices([1, 2, 3], [0.30, 0.52, 0.18])[0]
        accounts: list[Account] = []
        for i in range(n_accounts):
            product = "CURRENT" if i == 0 and rng.random() < 0.55 else (
                rng.choices(PRODUCTS, PRODUCT_WEIGHTS)[0]
            )
            # Account age spans years. Drawn here, before any fraud assignment.
            age_days = rng.choices(
                [rng.uniform(1, 60), rng.uniform(60, 400), rng.uniform(400, 2600)],
                [0.12, 0.33, 0.55],
            )[0]
            # Activity rate is a property of the account, not of a scenario.
            rate = rng.choices(
                [rng.uniform(0.01, 0.08), rng.uniform(0.08, 1.2), rng.uniform(1.2, 4.0)],
                [0.18, 0.60, 0.22],
            )[0]
            accounts.append(
                Account(
                    account_id=f"ACC{uuid.uuid4().hex[:12].upper()}",
                    product_type=product,
                    origin_sol_id=rng.choice(SOL_IDS),
                    opened_at=now - timedelta(days=age_days),
                    daily_rate=rate,
                )
            )

        devices = [f"DEV{uuid.uuid4().hex[:12]}" for _ in range(rng.choices([1, 2, 3], [0.62, 0.30, 0.08])[0])]
        payees = [f"BEN{uuid.uuid4().hex[:12].upper()}" for _ in range(rng.randint(2, 7))]

        start = rng.randint(5, 9)
        customers.append(
            Customer(
                customer_id=cid,
                display_name=name,
                accounts=accounts,
                devices=devices,
                home_region=region,
                known_beneficiaries=payees,
                active_hours=(start, start + rng.randint(11, 15)),
            )
        )

    return customers

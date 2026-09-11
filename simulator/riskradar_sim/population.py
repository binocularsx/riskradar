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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from .ids import hex_id

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


# How people actually differ from one another (D57).
#
# The first version of this file had one kind of customer, who paid one person
# at a time from a short list of regulars. That made every suspicious behaviour
# the exclusive property of fraud: only fraud paid several people in an hour,
# only fraud paid brand-new accounts. An audit later showed a single column
# could discard 77% of normal traffic without losing one fraud case.
#
# Real populations are not like that. A shop owner pays fifteen staff on the
# same morning. A trader settles suppliers all week. A family sends money to one
# account after a funeral. Those people are why fraud detection is hard, and
# leaving them out did not make the problem easier — it made it fake.
ARCHETYPES = ["ordinary", "salary_earner", "small_business", "trader"]
ARCHETYPE_WEIGHTS = [0.74, 0.12, 0.09, 0.05]


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

    # What kind of life this person has. Drawn from the population before any
    # fraud process is assigned, exactly like account age and dormancy (D20c) —
    # a fraud process may *select* a trader, it may never *make* somebody one.
    archetype: str = "ordinary"
    # Staff or suppliers this person pays in a batch. Empty for most people.
    payout_group: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Account:
        return self.accounts[0]

    @property
    def does_batch_payouts(self) -> bool:
        """Pays many people in one sitting, legitimately and routinely."""
        return self.archetype in ("small_business", "trader")

    @property
    def new_payee_rate(self) -> float:
        """How often this person pays somebody they have never paid before.

        A trader meets new suppliers constantly; a salaried person mostly pays
        the same landlord every month. Making this vary by person is what stops
        "first time paying this account" from being a fraud marker.
        """
        return {
            "ordinary": 0.26,
            "salary_earner": 0.16,
            "small_business": 0.34,
            "trader": 0.48,
        }[self.archetype]


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
        cid = f"CIF{hex_id(12).upper()}"
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
                    account_id=f"ACC{hex_id(12).upper()}",
                    product_type=product,
                    origin_sol_id=rng.choice(SOL_IDS),
                    opened_at=now - timedelta(days=age_days),
                    daily_rate=rate,
                )
            )

        devices = [f"DEV{hex_id(12)}" for _ in range(rng.choices([1, 2, 3], [0.62, 0.30, 0.08])[0])]

        archetype = rng.choices(ARCHETYPES, ARCHETYPE_WEIGHTS)[0]
        # Traders and shop owners know far more payees than a salaried person.
        n_payees = {
            "ordinary": rng.randint(2, 7),
            "salary_earner": rng.randint(2, 5),
            "small_business": rng.randint(5, 12),
            "trader": rng.randint(8, 20),
        }[archetype]
        payees = [f"BEN{hex_id(12).upper()}" for _ in range(n_payees)]

        # Staff and suppliers paid in a batch. This is the legitimate source of
        # "many different people within the hour" — the behaviour that used to
        # belong to mule fan-out alone.
        payout_group: list[str] = []
        if archetype == "small_business":
            payout_group = [f"BEN{hex_id(12).upper()}" for _ in range(rng.randint(5, 16))]
        elif archetype == "trader":
            payout_group = [f"BEN{hex_id(12).upper()}" for _ in range(rng.randint(8, 25))]

        start = rng.randint(5, 9)
        customers.append(
            Customer(
                archetype=archetype,
                payout_group=payout_group,
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

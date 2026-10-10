"""Identifiers for simulated customers, accounts, devices, payees and payments.

D68: these used to come from ``uuid4``, which is random on every run whatever
the seed. The demo is built in two runs (history first, thresholds derived,
then the live window), so the live window came from a different bank: every
account had no history, every device and payee was new. Identifiers now come
from their own stream, seeded by ``generate()``, so two runs with the same seed
build the same bank.

The stream is separate from the main generator so the sequence of every other
random choice, and therefore every corpus already built, is unchanged. Outside
``generate()`` (the live ``stream`` and ``burst`` commands) it stays seeded from
system entropy, so separate live runs never repeat a transaction reference.
"""

from __future__ import annotations

import random

_IDS = random.Random()


def reseed(seed: int) -> None:
    _IDS.seed(f"riskradar-ids:{seed}")


def hex_id(n: int) -> str:
    """``n`` lowercase hex characters, like ``uuid4().hex[:n]``."""
    return f"{_IDS.getrandbits(4 * n):0{n}x}"

"""Simulated BVNs, the simulated core's customer file, and another bank (D75).

A BVN here is derived from the customer number with a hash, so it is stable for
the same customer in every run and draws nothing from the random streams that
build the bank (D68): every corpus already generated stays valid.

Channel and switch payloads do not carry a BVN (§9.1), so the simulator does
not put one in them. Instead it writes the customer file a core banking system
would hold, ``customer_id,bvn,nin``, and Risk Radar's fixture resolver reads it
exactly where a Finacle customer inquiry would be made. Each customer is
written before the first payment that names them is posted.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

# The repository's fixtures folder, where the API's fixture resolver looks, whatever
# directory the simulator is run from.
DEFAULT_CORE_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "core_identities.csv"


def _digits(kind: str, customer_id: str, n: int) -> str:
    value = int(hashlib.sha256(f"{kind}:{customer_id}".encode("utf-8")).hexdigest(), 16)
    return str(value)[:n]


def bvn_for(customer_id: str) -> str:
    # Nigerian BVNs are eleven digits and commonly begin with 22.
    return "22" + _digits("bvn", customer_id, 9)


def nin_for(customer_id: str) -> str:
    return _digits("nin", customer_id, 11).rjust(11, "1")


class CoreFile:
    """Appends each new customer to the simulated core's file, once."""

    def __init__(self, path: Path = DEFAULT_CORE_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.known: set[str] = set()
        if self.path.exists():
            with self.path.open(newline="", encoding="utf-8") as fh:
                self.known = {r["customer_id"] for r in csv.DictReader(fh)}
        else:
            self.path.write_text("customer_id,bvn,nin\n", encoding="utf-8")

    def ensure(self, customer_ids: list[str]) -> int:
        new = [c for c in dict.fromkeys(customer_ids) if c not in self.known]
        if new:
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                for cid in new:
                    writer.writerow([cid, bvn_for(cid), nin_for(cid)])
            self.known.update(new)
        return len(new)

"""The three outside systems identity depends on, as adapters (D75).

Plain English
-------------
A BVN watch-list needs three things Risk Radar does not own:

1. **The core banking system** knows which BVN (and NIN) belongs to a customer
   number. Channel and switch payloads usually do not carry it (§9.1).
2. **The identity registry** (NIBSS for BVN, NIMC for NIN) says whether a BVN
   is real (REG-NG-03, INT-05).
3. **The industry watch-list** (NIBSS) is where a bank publishes a temporary
   flag and learns of flags other banks placed (REG-NG-04, FR-702).

Each is an interface with a version that works today and a version for the real
system. Swapping is configuration, not code:

    RISKRADAR_CORE_RESOLVER      fixture (default) | none | finacle
    RISKRADAR_IDENTITY_REGISTRY  format  (default) | nibss
    RISKRADAR_INDUSTRY_CONNECTOR none    (default) | loopback | nibss

The real adapters raise ``NotConnected`` until their connection is configured.
Nothing fails because of that: a customer with no BVN keeps a customer-level
flag, an unverifiable BVN is recorded as UNAVAILABLE, and an industry message
waits in the outbox with the reason on it.

Raw BVNs and NINs exist only inside a call to these adapters and at the ingestion
boundary. What Risk Radar stores is always the token (D9c).
"""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import REPO_ROOT

log = logging.getLogger("riskradar.identity")

BVN_PATTERN = re.compile(r"^\d{11}$")
HIGH_RISK_EVENT_TYPES = frozenset({"CREDENTIAL_CHANGED", "SIM_CHANGED", "DEVICE_BOUND"})


class NotConnected(Exception):
    """The outside system this adapter talks to is not configured yet."""


@dataclass(frozen=True)
class CoreIdentity:
    bvn: str
    nin: str | None = None


# --------------------------------------------------------------------------
# 1. Core banking: customer number -> BVN, NIN
# --------------------------------------------------------------------------


class CoreIdentityResolver(Protocol):
    name: str

    def resolve(self, customer_id: str) -> CoreIdentity | None: ...


class NoCoreResolver:
    name = "none"

    def resolve(self, customer_id: str) -> CoreIdentity | None:
        return None


class FixtureCoreResolver:
    """A customer file standing in for the core: ``customer_id,bvn,nin`` rows.

    The simulator writes it for the bank it builds (``history``), so the whole
    identity path runs end to end before a core is connected. Reloaded when the
    file changes. No file, no identities: the same as an unconnected core.
    """

    name = "fixture"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("RISKRADAR_CORE_FIXTURE", REPO_ROOT / "fixtures" / "core_identities.csv"))
        self._mtime: tuple[int, int] | None = None
        self._rows: dict[str, CoreIdentity] = {}

    def _load(self) -> None:
        try:
            st = self.path.stat()
            mtime = (st.st_mtime_ns, st.st_size)  # size too: appends within one clock tick
        except FileNotFoundError:
            self._rows, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        with self.path.open(newline="", encoding="utf-8") as fh:
            self._rows = {
                r["customer_id"]: CoreIdentity(r["bvn"], r.get("nin") or None)
                for r in csv.DictReader(fh)
                if BVN_PATTERN.match(r.get("bvn", ""))
            }
        self._mtime = mtime

    def resolve(self, customer_id: str) -> CoreIdentity | None:
        self._load()
        return self._rows.get(customer_id)


class FinacleCoreResolver:
    """Customer inquiry against Finacle (see docs/core-banking-reference.md).

    To connect: call the core's customer inquiry for the CIF and read the BVN
    and NIN fields from the customer master, inside the bank's network, with a
    short timeout. Until ``RISKRADAR_FINACLE_URL`` is set this raises, and the
    boundary carries on without a BVN.
    """

    name = "finacle"

    def __init__(self) -> None:
        self.url = os.environ.get("RISKRADAR_FINACLE_URL")

    def resolve(self, customer_id: str) -> CoreIdentity | None:
        raise NotConnected("Finacle customer inquiry is not configured (RISKRADAR_FINACLE_URL)")


# --------------------------------------------------------------------------
# 2. Identity registry: is this BVN real?
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    status: str  # VALID | INVALID | UNVERIFIED | UNAVAILABLE
    source: str


class IdentityRegistry(Protocol):
    name: str

    def verify(self, bvn: str) -> Verification: ...


class FormatRegistry:
    """Checks the shape only. A well-formed BVN stays UNVERIFIED, never VALID:
    eleven digits is not proof that a person exists."""

    name = "format"

    def verify(self, bvn: str) -> Verification:
        if not BVN_PATTERN.match(bvn) or len(set(bvn)) == 1:
            return Verification("INVALID", "FORMAT_CHECK")
        return Verification("UNVERIFIED", "FORMAT_CHECK")


class NibssRegistry:
    """BVN validation through NIBSS. Until configured, every check is UNAVAILABLE."""

    name = "nibss"

    def verify(self, bvn: str) -> Verification:
        if not os.environ.get("RISKRADAR_NIBSS_BVN_URL"):
            return Verification("UNAVAILABLE", "NIBSS_NOT_CONNECTED")
        raise NotConnected("NIBSS BVN validation client not implemented for this deployment")


# --------------------------------------------------------------------------
# 3. The industry watch-list
# --------------------------------------------------------------------------


class IndustryConnector(Protocol):
    name: str

    def publish(self, message: dict[str, Any]) -> str: ...

    def pull(self) -> list[dict[str, Any]]: ...


class NoIndustryConnector:
    name = "none"

    def publish(self, message: dict[str, Any]) -> str:
        raise NotConnected("no industry watch-list connector configured (RISKRADAR_INDUSTRY_CONNECTOR)")

    def pull(self) -> list[dict[str, Any]]:
        return []


class LoopbackIndustryConnector:
    """Accepts every message as if the industry hub had, and returns a reference.

    For demonstrating the outbox end to end. It shares nothing with anyone:
    flags from other banks arrive through the inbound API, where the simulator
    can play another bank.
    """

    name = "loopback"

    def publish(self, message: dict[str, Any]) -> str:
        return f"LOOPBACK-{message['flag_id']}-{message['operation']}"

    def pull(self) -> list[dict[str, Any]]:
        return []


class NibssIndustryConnector:
    """The NIBSS watch-list. Runs inside the bank's perimeter.

    A message carries a BVN token, not a BVN. The bank-side connector turns the
    token into the BVN it sends, from the bank's own records (it holds the
    pepper and its customers' BVNs), which is how D9d always meant a token to
    travel. Until ``RISKRADAR_NIBSS_WATCHLIST_URL`` is set this raises and the
    message waits.
    """

    name = "nibss"

    def publish(self, message: dict[str, Any]) -> str:
        raise NotConnected("NIBSS watch-list is not configured (RISKRADAR_NIBSS_WATCHLIST_URL)")

    def pull(self) -> list[dict[str, Any]]:
        raise NotConnected("NIBSS watch-list is not configured (RISKRADAR_NIBSS_WATCHLIST_URL)")


# --------------------------------------------------------------------------
# 4. The restriction channel (D97): where a lead's approved restriction is sent
# --------------------------------------------------------------------------


class RestrictionConnector(Protocol):
    name: str

    def publish(self, message: dict[str, Any]) -> str: ...


class NoRestrictionConnector:
    name = "none"

    def publish(self, message: dict[str, Any]) -> str:
        raise NotConnected("no restriction connector configured (RISKRADAR_RESTRICTION_CONNECTOR)")


class LoopbackRestrictionConnector:
    """Accepts every recommendation as a bank's system would, and returns a
    reference — for demonstrating the outbox end to end. It enforces nothing;
    the bank still reconciles the outcome through the ack (D7)."""

    name = "loopback"

    def publish(self, message: dict[str, Any]) -> str:
        return f"LOOPBACK-RESTRICT-{message['restriction_ref']}"


class CoreRestrictionConnector:
    """The bank's core/channel systems, which actually place a debit
    restriction, block a channel, freeze a card or block a beneficiary.

    A message carries tokens, not account numbers; the bank-side connector maps
    each token to the real account, card or destination inside its own network,
    where it holds the pepper (D9d). Until ``RISKRADAR_RESTRICTION_URL`` is set
    this raises and the recommendation waits in the outbox, visibly."""

    name = "core"

    def publish(self, message: dict[str, Any]) -> str:
        raise NotConnected("bank restriction channel is not configured (RISKRADAR_RESTRICTION_URL)")


_RESOLVERS = {"none": NoCoreResolver, "fixture": FixtureCoreResolver, "finacle": FinacleCoreResolver}
_REGISTRIES = {"format": FormatRegistry, "nibss": NibssRegistry}
class AccountManagerConnector(Protocol):
    """D106: the customer's account manager, reached like any other external
    party - a message their system accepts, and a reference back."""

    name: str

    def publish(self, message: dict[str, Any]) -> str: ...


class NoAccountManagerConnector:
    name = "none"

    def publish(self, message: dict[str, Any]) -> str:
        raise NotConnected(
            "no account manager connector configured (RISKRADAR_ACCOUNT_MANAGER_CONNECTOR)")


class LoopbackAccountManagerConnector:
    """Accepts every report as a relationship-management system would, and
    returns a reference. For demonstrating the outbox end to end; it notifies
    nobody, and its reference says so."""

    name = "loopback"

    def publish(self, message: dict[str, Any]) -> str:
        return f"loopback-am-{message.get('report_ref', 'unknown')}"


class EmailAccountManagerConnector:
    """D107: deliver the confirmed-fraud report to an account manager's inbox.

    Configuration, all from the environment and none of it defaulted to
    anything that could send:

        RISKRADAR_ACCOUNT_MANAGER_EMAIL   where the report goes (required)
        RISKRADAR_SMTP_HOST               the relay (required)
        RISKRADAR_SMTP_PORT               defaults to 587
        RISKRADAR_SMTP_FROM               defaults to the account it signs in as
        RISKRADAR_SMTP_USER / _PASSWORD   omitted for an open internal relay
        RISKRADAR_SMTP_STARTTLS           "0" to disable; on by default

    A missing host or recipient raises ``NotConnected`` rather than failing, so
    the report waits in the outbox instead of burning its attempts against a
    relay nobody has configured — and so selecting this connector without
    finishing the configuration cannot send anything anywhere.

    One inbox, not one per customer. Risk Radar holds no directory of which
    manager owns which account, and inventing one here would be a customer data
    store that nothing else in the system needs (D9c keeps identifiers one-way).
    The report names the subject token; the recipient's own systems resolve it.
    """

    name = "email"

    def publish(self, message: dict[str, Any]) -> str:
        import smtplib
        from email.message import EmailMessage
        from email.utils import make_msgid

        host = os.environ.get("RISKRADAR_SMTP_HOST", "").strip()
        to = os.environ.get("RISKRADAR_ACCOUNT_MANAGER_EMAIL", "").strip()
        if not host or not to:
            raise NotConnected(
                "account manager email is selected but not configured "
                "(RISKRADAR_SMTP_HOST, RISKRADAR_ACCOUNT_MANAGER_EMAIL)")

        port = int(os.environ.get("RISKRADAR_SMTP_PORT", "587"))
        user = os.environ.get("RISKRADAR_SMTP_USER", "")
        password = os.environ.get("RISKRADAR_SMTP_PASSWORD", "")
        sender = os.environ.get("RISKRADAR_SMTP_FROM", "") or user or f"riskradar@{host}"

        mail = EmailMessage()
        message_id = make_msgid(domain="riskradar")
        mail["Message-ID"] = message_id
        mail["From"] = sender
        mail["To"] = to
        mail["Subject"] = (f"Confirmed fraud - case {message.get('case_id')} "
                           f"- report {str(message.get('report_ref', ''))[:8]}")
        mail.set_content(_report_text(message))

        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if os.environ.get("RISKRADAR_SMTP_STARTTLS", "1") != "0":
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(mail)
        return message_id


def _report_text(m: dict[str, Any]) -> str:
    """The report as an account manager reads it.

    Plain text on purpose: it has to survive every mail client, and the person
    reading it needs the facts and the next step, not formatting. Amounts are
    minor units in the payload and naira here, because nobody acts on kobo.
    """
    naira = (m.get("exposure_minor") or 0) / 100
    lines = [
        f"Case {m.get('case_id')} has been confirmed as fraud by the fraud operations desk.",
        "",
        f"  Customer reference   {m.get('subject_token')}",
        f"  Exposure             NGN {naira:,.2f} across {m.get('transactions')} transaction(s)",
        f"  First seen           {m.get('first_seen') or 'unknown'}",
        f"  Last seen            {m.get('last_seen') or 'unknown'}",
        f"  Confirmed at         {m.get('confirmed_at')}",
        f"  Report reference     {m.get('report_ref')}",
        "",
        "Why the desk concluded fraud:",
        f"  {m.get('rationale') or 'no rationale recorded'}",
        "",
    ]
    asked = m.get("restrictions_recommended") or []
    if asked:
        lines.append("The bank has been asked to:")
        for r in asked:
            target = (r.get("transaction_ref") or r.get("account_token")
                      or r.get("beneficiary_token") or "")
            lines.append(f"  - {r.get('action', '').replace('_', ' ').lower()}  {target}")
    else:
        lines.append("No restriction was requested on this case.")
    lines += [
        "",
        "Over to you: contact and follow-up with the customer are yours. This desk",
        "works transactions and does not contact customers.",
        "",
        m.get("advisory", ""),
    ]
    return "\n".join(lines)


_ACCOUNT_MANAGER_CONNECTORS = {
    "none": NoAccountManagerConnector,
    "loopback": LoopbackAccountManagerConnector,
    "email": EmailAccountManagerConnector,
}


_CONNECTORS = {"none": NoIndustryConnector, "loopback": LoopbackIndustryConnector, "nibss": NibssIndustryConnector}
_RESTRICTION_CONNECTORS = {
    "none": NoRestrictionConnector,
    "loopback": LoopbackRestrictionConnector,
    "core": CoreRestrictionConnector,
}
_cache: dict[str, Any] = {}


def _choose(kind: str, env: str, default: str, table: dict[str, type]) -> Any:
    choice = os.environ.get(env, default)
    if choice not in table:
        raise RuntimeError(f"{env}={choice!r}; expected one of {sorted(table)}")
    key = f"{kind}:{choice}"
    if key not in _cache:
        _cache[key] = table[choice]()
    return _cache[key]


def core_resolver() -> CoreIdentityResolver:
    return _choose("core", "RISKRADAR_CORE_RESOLVER", "fixture", _RESOLVERS)


def identity_registry() -> IdentityRegistry:
    return _choose("registry", "RISKRADAR_IDENTITY_REGISTRY", "format", _REGISTRIES)


def industry_connector() -> IndustryConnector:
    return _choose("industry", "RISKRADAR_INDUSTRY_CONNECTOR", "none", _CONNECTORS)


def restriction_connector() -> RestrictionConnector:
    return _choose("restriction", "RISKRADAR_RESTRICTION_CONNECTOR", "none", _RESTRICTION_CONNECTORS)


def account_manager_connector() -> AccountManagerConnector:
    return _choose("account_manager", "RISKRADAR_ACCOUNT_MANAGER_CONNECTOR", "none",
                   _ACCOUNT_MANAGER_CONNECTORS)


def institution_code() -> str:
    return os.environ.get("RISKRADAR_INSTITUTION_CODE", "RISKRADAR-DEV")

"""One-way tokenisation at the ingestion boundary (D9c, D9d, D19).

Plain English
-------------
Turns real account numbers into meaningless strings, permanently.

The moment a transaction arrives, the customer's ID and account number are run
through a one-way scramble, and only the scrambled version is ever saved. The
same account number always produces the same scrambled string, which is what
lets the system say "this is the fifth transfer from this account today"
without ever knowing which account that is.

There is no way back. No lookup table, no key kept in a safe. If Risk Radar's
database were stolen tomorrow, there would be no account numbers in it. When a
case has to be escalated to the bank, the scrambled string travels and the
bank looks it up on their side.

HMAC-SHA256 with a secret pepper. Deterministic, so behavioural baselines and
future ring detection still work — they need *stable* identity, not *readable*
identity. There is no mapping table and no key custody: Risk Radar cannot resolve
a token back to an account, by design, and on escalation the token travels while
the receiving bank performs the lookup on their side.

Two levels, because banking identity is customer-first (D19):

* ``subject_token``  — the customer. The subject of a **case**.
* ``account_token``  — the account.  The subject of a **behavioural baseline**.
"""

from __future__ import annotations

import hashlib
import hmac

from ..config import settings


def _token(namespace: str, value: str) -> str:
    """Namespaced HMAC.

    The namespace stops a customer identifier and an account identifier that
    happen to share a string from colliding into the same token — which would
    silently merge two people's behaviour into one baseline.
    """
    message = f"{namespace}:{value}".encode("utf-8")
    digest = hmac.new(settings().hmac_pepper, message, hashlib.sha256).hexdigest()
    return f"{namespace[:3]}_{digest[:40]}"


def subject_token(customer_id: str) -> str:
    """Customer-level token. Correlates cases across a victim's own accounts."""
    return _token("subject", customer_id)


def account_token(account_id: str) -> str:
    """Account-level token. Keyed to behavioural baselines."""
    return _token("account", account_id)


def beneficiary_token(account_id: str) -> str:
    """Beneficiary accounts share the account namespace deliberately.

    A mule's receiving account today is a sender tomorrow, and fan-in detection
    depends on those being the same token.
    """
    return _token("account", account_id)


def device_token(fingerprint: str) -> str:
    return _token("device", fingerprint)


def hash_api_key(raw_key: str) -> str:
    """API keys are compared by hash, never stored raw.

    Plain SHA-256 rather than Argon2: these are long random machine-generated
    secrets with no entropy problem, and ingestion checks one on every request —
    a deliberately slow KDF here would be a self-inflicted rate limit.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def hash_session_id(raw_cookie_value: str) -> str:
    """Session cookies are stored hashed, so a database read cannot impersonate."""
    return hashlib.sha256(raw_cookie_value.encode("utf-8")).hexdigest()

"""Password hashing and TOTP.

Argon2id (D12): memory-hard, so a stolen hash costs an attacker RAM as well as
time, which is the property bcrypt lacks against GPU cracking.
"""

from __future__ import annotations

import secrets

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Defaults are the argon2-cffi RFC 9106 low-memory profile. Stated explicitly so
# the cost is a decision on the page rather than whatever the library ships next.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str | None, plaintext: str) -> bool:
    """Constant-ish time verification that never raises on bad input.

    A missing hash still performs a dummy verification: returning early for
    unknown accounts turns login into a user-enumeration oracle.
    """
    if not stored_hash:
        _hasher.hash("dummy-to-equalise-timing")
        return False
    try:
        return _hasher.verify(stored_hash, plaintext)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


# ---------------------------------------------------------------------------
# TOTP (D12a)
# ---------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Risk Radar")


def verify_totp(secret: str | None, code: str) -> bool:
    """One step of clock drift tolerated in each direction.

    Wider windows make phone-clock complaints go away and make a stolen code
    replayable for minutes. One step is the usual compromise.
    """
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def new_session_token() -> str:
    """256 bits from the OS CSPRNG. Stored hashed; the raw value only ever
    exists in the cookie."""
    return secrets.token_urlsafe(32)


def new_api_key() -> str:
    return "rr_" + secrets.token_urlsafe(32)

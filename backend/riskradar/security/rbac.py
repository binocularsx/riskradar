"""Role-based access control with separation of duties (D12b).

Plain English
-------------
Who is allowed to do what.

Four jobs exist: the analyst who investigates alerts, the fraud operations lead
who can close cases, the information-security analyst who handles suspected
account compromise, and the administrator who tunes the system.

The important line is that the administrator — the one person who can change
how sensitive the detection is — is not allowed to open a single case. Not even
to look. Otherwise the same person could loosen a threshold and then review the
cases that threshold failed to produce, and nobody would ever know.

The load-bearing sentence from PRD §5: *the account that tunes detection cannot
be the account that clears what detection misses.*

So ADMIN holds no case permission at all — not even read. That is the strict
reading of "explicitly cannot: review or decide any case", and it is the right
one: an administrator who can retune a threshold *and* inspect the cases that
threshold produced can quietly tune their own work out of view. They keep
``metrics:read``, which is what threshold tuning against an alert budget (D11d)
actually requires.

D111 removed INFOSEC_ANALYST. D108 had already retired it as an escalation
target; the role itself, its permissions and its account are gone, so the desk
is three human roles and SYSTEM.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    CASES_READ = "cases:read"
    CASES_REVIEW = "cases:review"          # move to UNDER_REVIEW, add notes
    # D93: an analyst *proposes* an outcome; a different lead decides it.
    CASES_SUBMIT_OUTCOME = "cases:submit_outcome"
    CASES_APPROVE_FRAUD = "cases:approve_fraud"
    CASES_CLOSE = "cases:close"
    CASES_REASSIGN = "cases:reassign"
    CASES_ESCALATE = "cases:escalate"
    METRICS_READ = "metrics:read"
    AUDIT_READ = "audit:read"
    ADMIN_USERS = "admin:users"
    ADMIN_RULES = "admin:rules"
    ADMIN_THRESHOLDS = "admin:thresholds"
    ADMIN_MODELS = "admin:models"
    ADMIN_LISTS = "admin:lists"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "ANALYST": frozenset(
        {
            Permission.CASES_READ,
            Permission.CASES_REVIEW,
            Permission.CASES_SUBMIT_OUTCOME,
            Permission.CASES_ESCALATE,
        }
    ),
    "FRAUD_OPS_LEAD": frozenset(
        {
            Permission.CASES_READ,
            Permission.CASES_REVIEW,
            # A lead may also propose — and may never decide their own (D93).
            Permission.CASES_SUBMIT_OUTCOME,
            Permission.CASES_APPROVE_FRAUD,
            Permission.CASES_ESCALATE,
            Permission.CASES_CLOSE,
            Permission.CASES_REASSIGN,
            Permission.METRICS_READ,
        }
    ),
    "ADMIN": frozenset(
        {
            Permission.METRICS_READ,
            Permission.AUDIT_READ,
            Permission.ADMIN_USERS,
            Permission.ADMIN_RULES,
            Permission.ADMIN_THRESHOLDS,
            Permission.ADMIN_MODELS,
            Permission.ADMIN_LISTS,
        }
    ),
    # Never logs in. Present so the audit actor can always be a real user row.
    "SYSTEM": frozenset(),
}

# D12a made MFA mandatory for FRAUD_OPS_LEAD and ADMIN and default-on for
# ANALYST. D95 made it mandatory for *every* human role, enforced on every
# request, so that no user row could quietly turn it off.
#
# D105 moves the switch back to the user row — at the owner's direction, so an
# administrator can decide whether a given person is asked for a code — and this
# set is now the **default applied when an account is created**, not a runtime
# gate. `auth.login` and `api.deps` read `users.totp_enabled`; changing it is a
# maker-checker change like any other, and it is audited. The protection D95
# wanted is kept where it counts: the column can no longer be changed quietly,
# because changing it takes two administrators and leaves a record.
# SYSTEM never logs in and holds no factor.
MFA_REQUIRED_ROLES = frozenset({"ANALYST", "FRAUD_OPS_LEAD", "ADMIN"})


def permissions_for(role: str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has(role: str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def mfa_required(role: str) -> bool:
    return role in MFA_REQUIRED_ROLES

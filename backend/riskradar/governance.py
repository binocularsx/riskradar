"""Privileged-access maker-checker for detection tuning (D96, plan §3, FR-308).

An administrator *proposes* a change to the detection configuration; a different
administrator holding the same permission *approves* it; only an approval
publishes a new ruleset or threshold version. This closes D69e: the old
`approved_by` was the person who made the change, so it recorded nobody's
oversight. Now it records the second signature it always pretended to be.

The separation is a database CHECK (`config_decider_is_not_proposer`), mirrored
by an explicit refusal here so the message is a sentence rather than a
constraint name. The shape is deliberately the fraud-approval shape (D93): one
route for "a proposal a second person decides", learned once.

Detection tuning only (rules and thresholds, the surface D69e names). Model
promotion, list entries and user administration are the next surfaces (D96d).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import chain
from .model import registry
from .security.passwords import new_totp_secret, totp_uri
from .security.rbac import Permission

# The permission a proposer must hold, and an approver must hold too — approval
# is not a lesser act than the change it enacts.
PERMISSION_FOR: dict[str, Permission] = {
    "RULE_UPDATE": Permission.ADMIN_RULES,
    "THRESHOLD_PUBLISH": Permission.ADMIN_THRESHOLDS,
    "MODEL_PROMOTE": Permission.ADMIN_MODELS,          # D98
    "LIST_ADD": Permission.ADMIN_LISTS,                # D98
    "LIST_REMOVE": Permission.ADMIN_LISTS,             # D98
    "USER_CREATE": Permission.ADMIN_USERS,             # D99
    # D104: the rest of an account's life. Granting a role, taking one away,
    # disabling somebody and re-issuing their second factor are all changes to
    # who can do what, so they take the same second signature as creating the
    # account did. A single administrator who could promote themselves to
    # nothing-is-off-limits would make every other control decorative.
    "USER_ROLE_CHANGE": Permission.ADMIN_USERS,        # D104
    "USER_SET_ACTIVE": Permission.ADMIN_USERS,         # D104
    "USER_MFA_RESET": Permission.ADMIN_USERS,          # D104
    # D105: whether a person is asked for a code, their password, and the email
    # that identifies them. All three decide who can get into an account.
    "USER_SET_MFA": Permission.ADMIN_USERS,            # D105
    "USER_PASSWORD_RESET": Permission.ADMIN_USERS,     # D105
    "USER_PROFILE_UPDATE": Permission.ADMIN_USERS,     # D105
    # D103: lifting a restriction is a customer-impacting action, so it takes a
    # second signature like the one that imposed it — from the desk that owns
    # the case, not from an administrator who cannot see it (D12b).
    "RESTRICTION_RELEASE": Permission.CASES_APPROVE_FRAUD,
}

# An applier returns ``(applied_version, extra)``: the version/id the change
# produced (stored on the request), and any one-time material the approver needs
# back but which is never stored — a new user's TOTP provisioning URI (D99).

_REVIEW_INTERVAL = timedelta(days=90)


# ---------------------------------------------------------------------------
# Appliers — run only on approval, by the approver, against current active state
# ---------------------------------------------------------------------------


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


def _apply_rule_update(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> int:
    """Publish a new ruleset version with one rule changed (was admin.update_rule).

    Versioned rather than edited in place: decisions record the ruleset version
    that applied (FR-042), so mutating a version decisions already point at makes
    them unreproducible. Built against *current* active state, not the snapshot
    the proposer read, so an approval lands on top of any change since — the
    before-snapshot on the request records what the proposer actually saw.
    """
    code = payload["code"]
    active = _rows(conn, "SELECT id, version FROM rulesets WHERE is_active LIMIT 1")
    if not active:
        raise _Conflict("no active ruleset")
    old = _rows(
        conn,
        """
        SELECT code, power, severity, enabled, params,
               owner, rationale, approved_by, approved_at, next_review_at
          FROM rule_configs WHERE ruleset_id = %s
        """,
        (active[0]["id"],),
    )
    current = next((r for r in old if r["code"] == code), None)
    if not current:
        raise _Conflict(f"unknown rule {code}")

    now = datetime.now(timezone.utc)
    new_version = active[0]["version"] + 1
    with conn.cursor() as cur:
        cur.execute("UPDATE rulesets SET is_active = false WHERE is_active")
        cur.execute(
            """
            INSERT INTO rulesets (version, notes, created_by, is_active)
            VALUES (%s, %s, %s, true) RETURNING id
            """,
            (new_version, f"{code} change approved by {approver['email']}", proposer_id),
        )
        row = cur.fetchone()
        new_id = int(row["id"] if isinstance(row, dict) else row[0])

        for rule in old:
            params = rule["params"]
            if isinstance(params, str):
                params = json.loads(params)
            enabled, severity = rule["enabled"], rule["severity"]
            owner, rule_rationale = rule["owner"], rule["rationale"]
            approved_by, approved_at = rule["approved_by"], rule["approved_at"]
            next_review_at = rule["next_review_at"]
            if rule["code"] == code:
                if payload.get("enabled") is not None:
                    enabled = payload["enabled"]
                if payload.get("params") is not None:
                    params = payload["params"]
                if payload.get("severity") is not None:
                    severity = payload["severity"]
                if payload.get("owner") is not None:
                    owner = payload["owner"]
                # The approver is the recorded approval; the rationale is the
                # request's, required at proposal time.
                rule_rationale = rationale
                approved_by = approver["email"]
                approved_at = now
                next_review_at = now + _REVIEW_INTERVAL
            cur.execute(
                """
                INSERT INTO rule_configs (ruleset_id, code, power, severity, enabled, params,
                                          owner, rationale, approved_by, approved_at, next_review_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (new_id, rule["code"], rule["power"], severity, enabled, json.dumps(params or {}),
                 owner, rule_rationale, approved_by, approved_at, next_review_at),
            )
    return new_version, {}


def _apply_threshold_publish(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> int:
    """Publish and activate a new threshold version (was admin.create_thresholds)."""
    p_monitor, p_review, p_hold = payload["p_monitor"], payload["p_review"], payload["p_hold"]
    if not (p_monitor <= p_review <= p_hold):
        raise _Conflict("thresholds must be ordered: monitor <= review <= hold")
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM threshold_sets")
        row = cur.fetchone()
        version = int(row["v"] if isinstance(row, dict) else row[0])
        cur.execute("UPDATE threshold_sets SET is_active = false WHERE is_active")
        cur.execute(
            """
            INSERT INTO threshold_sets
                (version, p_monitor, p_review, p_hold, alert_min_level, notes, created_by, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, true)
            """,
            (version, p_monitor, p_review, p_hold, payload["alert_min_level"],
             payload.get("notes") or rationale, proposer_id),
        )
    return version, {}


def _apply_model_promote(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> int:
    """Repoint the active-model pointer (was admin.promote_model). Promotion is a
    pointer move, audited, and now it takes a second administrator (D15b, D98).
    The approver is recorded as who put it live; the proposer is on the request."""
    model_version_id = payload["model_version_id"]
    target = _rows(conn, "SELECT id, name, version FROM model_versions WHERE id = %s",
                   (model_version_id,))
    if not target:
        raise _Conflict("model version not found")
    with conn.cursor() as cur:
        cur.execute("UPDATE model_versions SET is_active = false WHERE is_active")
        cur.execute(
            "UPDATE model_versions SET is_active = true, promoted_at = now(), promoted_by = %s WHERE id = %s",
            (approver["id"], model_version_id),
        )
    registry.invalidate_cache()
    # applied_version is the numeric model id (the string semver would not fit an int).
    return int(model_version_id), {}


def _apply_list_add(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> int | None:
    """Add a sanctions / known-mule / allowlist entry (was admin.add_list_entry).

    The account was tokenised at proposal time (D9c), so the payload carries only
    tokens. The proposer is recorded as who added it; the approver is on the
    request and the audit."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO beneficiary_lists (kind, token, account_token, note, added_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (payload["kind"], payload["token"], payload.get("account_token"),
             payload.get("note"), proposer_id),
        )
        row = cur.fetchone()
    if not row:
        raise _Conflict("that entry is already on the list")
    return int(row["id"] if isinstance(row, dict) else row[0]), {}


def _apply_list_remove(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> int:
    """Remove a list entry (was admin.remove_list_entry). Removing a sanctions or
    known-mule entry unprotects, so it too takes two administrators (D98)."""
    entry_id = payload["entry_id"]
    existing = _rows(conn, "SELECT id FROM beneficiary_lists WHERE id = %s", (entry_id,))
    if not existing:
        raise _Conflict("that entry is no longer on the list")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM beneficiary_lists WHERE id = %s", (entry_id,))
    return int(entry_id), {}


def _apply_user_create(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Create the user (was admin.create_user), on a second administrator's
    approval (D99). The password was hashed at proposal time, so no plaintext was
    ever stored; the TOTP secret is generated *here*, at approval, so no
    authenticator secret sits in the change-request payload. The provisioning URI
    is returned once to the approver and stored nowhere it can be read again."""
    # Check first rather than catching a unique violation: a caught constraint
    # error would leave the transaction aborted and fail the rest of decide().
    if _rows(conn, "SELECT 1 FROM users WHERE lower(email) = lower(%s)", (payload["email"],)):
        raise _Conflict("a user with that email already exists")
    secret = new_totp_secret()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, display_name, password_hash, role, totp_secret, totp_enabled)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING id
            """,
            (payload["email"], payload["display_name"], payload["password_hash"],
             payload["role"], secret),
        )
        row = cur.fetchone()
    uid = int(row["id"] if isinstance(row, dict) else row[0])
    return uid, {"user_id": uid, "totp_uri": totp_uri(secret, payload["email"])}





MIN_ACTIVE_ADMINS = 2
"""D96 seeds two administrators because "one administrator is not enough" for
maker-checker. Dropping to one would not merely be risky: with a single
administrator no admin change can ever be approved again, because the approver
may not be the proposer. The system would be wedged with no way out."""


def _managed_user(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(
        conn,
        "SELECT id, email, display_name, role, active, is_system FROM users WHERE id = %s",
        (payload["user_id"],),
    )
    if not rows:
        raise _Conflict("that user no longer exists")
    target = rows[0]
    if target["is_system"]:
        raise _Conflict("the system account is not a person and cannot be managed")
    return target


def _keep_enough_admins(conn: Any, losing_id: int) -> None:
    """Refuse a change that would leave too few administrators to govern.

    A backstop rather than the main protection. The approver rules already make
    the desert case unreachable: an approver may be neither the proposer nor the
    subject, so two administrators besides the subject must exist for any
    decision to happen at all. This stays as a cheap invariant in case those
    rules are ever loosened, and because losing the last approver would wedge
    the system with no way back.
    """
    remaining = _rows(
        conn,
        """SELECT count(*) AS n FROM users
            WHERE role = 'ADMIN' AND active AND NOT is_system AND id <> %s""",
        (losing_id,),
    )[0]["n"]
    if int(remaining) < MIN_ACTIVE_ADMINS:
        raise _Conflict(
            "that would leave fewer than two active administrators, and "
            "maker-checker needs a second one to approve anything afterwards"
        )


def _not_themselves(target: dict[str, Any], approver: dict[str, Any], what: str) -> None:
    """Separation of duties reaches the approver too (D93, D99).

    The router refuses a *proposer* who is the subject; this closes the other
    half at the moment it actually matters, because an administrator must not
    sign off a change to their own access. Checked here rather than only in the
    router because apply time is when the state is real: the approver could have
    become the subject between proposal and decision.
    """
    if int(target["id"]) == int(approver["id"]):
        raise _Conflict(f"an administrator cannot approve {what} to their own account")


def _apply_user_role_change(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Move a user to a different role, on a second administrator's approval (D104).

    The role *is* the permission set (D12b), so this is the privilege-escalation
    path in the system: promoting somebody to ADMIN hands them detection tuning,
    and demoting the wrong person can empty the approver pool.
    """
    target = _managed_user(conn, payload)
    _not_themselves(target, approver, "a role change")
    if target["role"] == payload["role"]:
        raise _Conflict(f"{target['email']} is already {payload['role']}")
    if target["role"] == "ADMIN" and payload["role"] != "ADMIN":
        _keep_enough_admins(conn, int(target["id"]))
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET role = %s WHERE id = %s", (payload["role"], target["id"]))
    return int(target["id"]), {"user_id": int(target["id"]), "email": target["email"],
                               "from_role": target["role"], "to_role": payload["role"]}


def _apply_user_set_active(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Disable or restore an account (D104).

    Disabling is the leaver path and the compromise path, so it must be quick —
    but not unilateral, because disabling the other administrator is how one
    person would take sole control. ``sessions.resolve`` refuses an inactive
    user, so a disabled account loses its live session on its next request; the
    row is kept rather than deleted so the audit trail still names a real actor.
    """
    target = _managed_user(conn, payload)
    active = bool(payload["active"])
    _not_themselves(target, approver, "enabling or disabling")
    if bool(target["active"]) == active:
        raise _Conflict(f"{target['email']} is already {'active' if active else 'disabled'}")
    if not active and target["role"] == "ADMIN":
        _keep_enough_admins(conn, int(target["id"]))
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET active = %s WHERE id = %s", (active, target["id"]))
    return int(target["id"]), {"user_id": int(target["id"]), "email": target["email"],
                               "active": active}


def _apply_user_mfa_reset(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Issue a fresh authenticator secret (D104).

    The lost-phone path. It is deliberately maker-checker rather than a button,
    because re-issuing somebody's second factor is exactly what an attacker who
    already holds one administrator account would want to do to another. The new
    secret is generated here, at approval, and its provisioning URI is returned
    once to the approver to hand over — mirroring USER_CREATE (D99). Every code
    the old authenticator still shows stops working the moment this lands.
    """
    target = _managed_user(conn, payload)
    _not_themselves(target, approver, "an MFA reset")
    secret = new_totp_secret()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET totp_secret = %s, totp_enabled = true WHERE id = %s",
            (secret, target["id"]),
        )
    return int(target["id"]), {"user_id": int(target["id"]), "email": target["email"],
                               "totp_uri": totp_uri(secret, target["email"])}


def _apply_user_set_mfa(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Decide whether this person is asked for a code (D105).

    `users.totp_enabled` is the switch that `auth.login` and `api.deps` read, so
    turning it off genuinely stops the prompt — it used to be inert, overridden
    by the role (D95), which made an administrator's "MFA off" a lie.

    Turning it off removes a control from an account that reads customer
    financial data, so it is not a button: it takes a second administrator and
    it leaves an audit row naming both of them and the reason. Turning it back
    on bites immediately, including on sessions already open, because the check
    runs per request and those sessions never satisfied a factor.
    """
    target = _managed_user(conn, payload)
    enabled = bool(payload["enabled"])
    _not_themselves(target, approver, "their own multi-factor requirement")
    current = _rows(conn, "SELECT totp_enabled, totp_secret FROM users WHERE id = %s",
                    (target["id"],))[0]
    if bool(current["totp_enabled"]) == enabled:
        raise _Conflict(
            f"{target['email']} is already {'asked' if enabled else 'not asked'} for a code")
    if enabled and not current["totp_secret"]:
        raise _Conflict(
            f"{target['email']} has no authenticator secret — reset their MFA first, "
            "which issues one, otherwise they could never sign in")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET totp_enabled = %s WHERE id = %s", (enabled, target["id"]))
    return int(target["id"]), {"user_id": int(target["id"]), "email": target["email"],
                               "totp_enabled": enabled}


def _apply_user_password_reset(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Set a new password for somebody who cannot sign in (D105).

    Hashed at proposal time exactly as in USER_CREATE (D99), so the plaintext is
    never stored and never sits in a pending request. Whoever sets a password can
    sign in as that person if their second factor is off, which is why this is
    maker-checker and not a button — and why the two changes are separate
    requests, so turning MFA off and resetting a password is visibly two
    approvals rather than one convenient action.
    """
    target = _managed_user(conn, payload)
    _not_themselves(target, approver, "a password reset")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (payload["password_hash"], target["id"]))
    return int(target["id"]), {"user_id": int(target["id"]), "email": target["email"]}


def _apply_user_profile_update(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Correct the email and display name on an account (D105).

    The email is not decoration: it is the login identifier, so changing it moves
    who can sign in to this account and its permissions. A mistyped address on
    creation used to leave an account nobody could reach and nobody could fix.
    """
    target = _managed_user(conn, payload)
    _not_themselves(target, approver, "their own email or name")
    email = payload["email"].strip()
    if _rows(conn, "SELECT 1 FROM users WHERE lower(email) = lower(%s) AND id <> %s",
             (email, target["id"])):
        raise _Conflict("another user already has that email")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET email = %s, display_name = %s WHERE id = %s",
                    (email, payload["display_name"].strip(), target["id"]))
    return int(target["id"]), {"user_id": int(target["id"]), "email": email,
                               "from_email": target["email"]}


def _apply_restriction_release(
    conn: Any, proposer_id: int, approver: dict[str, Any], rationale: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """D103: issue the lift the proposer asked for, against the order as it is now.

    Checked again here, not trusted from the proposal: the bank may have
    answered, or somebody may have lifted it already, between proposing and
    approving.
    """
    from . import restrictions

    order = restrictions.order_by_ref(conn, payload["restriction_ref"])
    if order is None:
        raise _Conflict("that restriction no longer exists")
    blockers = restrictions.release_blockers(conn, order)
    if blockers:
        raise _Conflict("; ".join(blockers))
    try:
        release = restrictions.create_release(
            conn, order=order, approver_id=approver["id"],
            reason=payload.get("reason") or rationale, proposed_by=proposer_id,
        )
    except restrictions.RestrictionError as exc:
        raise _Conflict(exc.detail) from exc
    return int(release["id"]), {"restriction_ref": str(release["restriction_ref"])}


_APPLIERS = {
    "RULE_UPDATE": _apply_rule_update,
    "THRESHOLD_PUBLISH": _apply_threshold_publish,
    "MODEL_PROMOTE": _apply_model_promote,
    "LIST_ADD": _apply_list_add,
    "LIST_REMOVE": _apply_list_remove,
    "USER_CREATE": _apply_user_create,
    "USER_ROLE_CHANGE": _apply_user_role_change,         # D104
    "USER_SET_ACTIVE": _apply_user_set_active,           # D104
    "USER_MFA_RESET": _apply_user_mfa_reset,             # D104
    "USER_SET_MFA": _apply_user_set_mfa,                 # D105
    "USER_PASSWORD_RESET": _apply_user_password_reset,   # D105
    "USER_PROFILE_UPDATE": _apply_user_profile_update,   # D105
    "RESTRICTION_RELEASE": _apply_restriction_release,   # D103
}


class _Conflict(Exception):
    """An apply-time problem the caller turns into a 409 (state moved under the
    proposal, or the payload no longer fits)."""


class MakerCheckerError(Exception):
    """A refusal the router turns into a 4xx with the message intact."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Propose / list / decide
# ---------------------------------------------------------------------------


def propose(
    conn: Any,
    actor: dict[str, Any],
    *,
    change_type: str,
    target: str,
    summary: str,
    payload: dict[str, Any],
    before_snapshot: dict[str, Any],
    rationale: str,
) -> dict[str, Any]:
    """Record a proposed change as PENDING. A second proposal for the same target
    supersedes the first (one thing for the approver to decide)."""
    if change_type not in _APPLIERS:
        raise MakerCheckerError(400, f"unknown change type {change_type}")
    if len((rationale or "").strip()) < 20:
        raise MakerCheckerError(422, "a rationale of at least 20 characters is required")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE config_change_requests SET state = 'SUPERSEDED'
             WHERE change_type = %s AND target = %s AND state = 'PENDING'
            """,
            (change_type, target),
        )
        cur.execute(
            """
            INSERT INTO config_change_requests
                (change_type, target, summary, payload, before_snapshot, rationale, proposed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, change_type, target, summary, state, proposed_at
            """,
            (change_type, target, summary, json.dumps(payload),
             json.dumps(before_snapshot, default=str), rationale.strip(), actor["id"]),
        )
        row = cur.fetchone()
    request = dict(row) if not isinstance(row, dict) else row

    chain.append(
        conn,
        actor_user_id=actor["id"],
        action="CONFIG_CHANGE_PROPOSED",
        object_type="config_change_request",
        object_id=request["id"],
        to_state=summary,
        payload={"change_type": change_type, "target": target, "rationale": rationale.strip()},
    )
    return {"status": "pending", "request": request}


def list_requests(conn: Any, *, states: tuple[str, ...] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = """
        SELECT r.id, r.change_type, r.target, r.summary, r.payload, r.before_snapshot,
               r.rationale, r.state, r.proposed_at, r.decided_at, r.decision_reason,
               r.applied_version,
               p.display_name AS proposed_by, p.id AS proposed_by_id,
               d.display_name AS decided_by
          FROM config_change_requests r
          JOIN users p ON p.id = r.proposed_by
          LEFT JOIN users d ON d.id = r.decided_by
    """
    params: tuple = ()
    if states:
        sql += " WHERE r.state = ANY(%s)"
        params = (list(states),)
    sql += " ORDER BY r.proposed_at DESC LIMIT %s"
    params = params + (limit,)
    return _rows(conn, sql, params)


def decide(
    conn: Any,
    actor: dict[str, Any],
    *,
    request_id: int,
    action: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Approve, reject or return a pending change. Only an approval applies it.

    The approver must hold the same permission as the change and must not be the
    proposer — enforced here with a sentence and by the database CHECK behind it.
    """
    if action not in ("APPROVE", "REJECT", "RETURN"):
        raise MakerCheckerError(400, f"unknown action {action}")

    with conn.cursor() as cur:
        # Lock the row so two approvers cannot both decide it.
        cur.execute(
            "SELECT * FROM config_change_requests WHERE id = %s FOR UPDATE",
            (request_id,),
        )
        row = cur.fetchone()
    if not row:
        raise MakerCheckerError(404, "no such change request")
    request = dict(row) if not isinstance(row, dict) else row

    if request["state"] != "PENDING":
        raise MakerCheckerError(409, f"request is {request['state'].lower()}, not pending")

    required = PERMISSION_FOR[request["change_type"]]
    if str(required) not in actor["permissions"]:
        raise MakerCheckerError(
            403, f"approving a {request['change_type']} needs {required}"
        )
    if request["proposed_by"] == actor["id"]:
        raise MakerCheckerError(
            403,
            "the administrator who proposed a change cannot approve it. "
            "This is maker-checker, not a bug.",
        )
    if action in ("REJECT", "RETURN") and not (reason or "").strip():
        raise MakerCheckerError(422, f"a reason is required to {action.lower()} a change")

    now = datetime.now(timezone.utc)
    applied_version: int | None = None
    extra: dict[str, Any] = {}
    if action == "APPROVE":
        payload = request["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            applied_version, extra = _APPLIERS[request["change_type"]](
                conn, request["proposed_by"], actor, request["rationale"], payload
            )
        except _Conflict as exc:
            raise MakerCheckerError(409, str(exc))

    new_state = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "RETURN": "RETURNED"}[action]
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE config_change_requests
               SET state = %s, decided_by = %s, decided_at = %s,
                   decision_reason = %s, applied_version = %s
             WHERE id = %s
            """,
            (new_state, actor["id"], now, (reason or None), applied_version, request_id),
        )

    chain.append(
        conn,
        actor_user_id=actor["id"],
        action=f"CONFIG_CHANGE_{new_state}",
        object_type="config_change_request",
        object_id=request_id,
        from_state=request["summary"],
        to_state=new_state,
        payload={
            "change_type": request["change_type"],
            "target": request["target"],
            "proposed_by": request["proposed_by"],
            "applied_version": applied_version,
            "reason": reason,
        },
    )
    return {"status": new_state.lower(), "applied_version": applied_version, **extra}

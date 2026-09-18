"""Administration (FR-040, FR-041, FR-042).

Rules, thresholds, lists, users and model promotion — every one of them
configuration rather than a code change, and every change audited with actor,
old value and new value.

Separation of duties runs the other way here too (D12b): the ADMIN role holds
none of the case permissions, so nothing in this module can review or decide a
case, and the routes that tune detection are unreachable to the analysts whose
work they shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...audit import chain
from ...model import registry
from ...security.passwords import hash_password, new_api_key, new_totp_secret, totp_uri
from ...security.rbac import Permission, mfa_required
from ...security.tokens import account_token, hash_api_key
from ..deps import get_conn, requires
from ..schemas import ListEntryIn, PromoteModelIn, RuleUpdateIn, ThresholdsIn

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# D69e: a rule changed with a stated reason is due for review again in 90 days.
REVIEW_INTERVAL = timedelta(days=90)


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Rules (FR-040, FR-041)
# ---------------------------------------------------------------------------


@router.get("/rules")
def list_rules(
    user: dict = Depends(requires(Permission.ADMIN_RULES)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    ruleset = _rows(conn, "SELECT id, version, notes FROM rulesets WHERE is_active LIMIT 1")
    if not ruleset:
        raise HTTPException(500, "no active ruleset")
    rules = _rows(
        conn,
        """
        SELECT id, code, power, severity, enabled, params,
               owner, rationale, approved_by, approved_at, next_review_at,
               (next_review_at IS NULL OR next_review_at < now()) AS review_overdue
          FROM rule_configs WHERE ruleset_id = %s ORDER BY power, code
        """,
        (ruleset[0]["id"],),
    )
    # D69e (base PRD FR-507): flag orphaned rules (no owner) and dormant ones
    # (enabled, but silent for 30 days). A dormant rule is either guarding
    # against something rare or broken; either way somebody should look.
    fired = {
        r["code"]: int(r["n"])
        for r in _rows(
            conn,
            """
            SELECT s->>'code' AS code, count(*) AS n
              FROM decisions d, jsonb_array_elements(d.signals) s
             WHERE d.decided_at > now() - interval '30 days'
             GROUP BY 1
            """,
        )
    }
    for rule in rules:
        rule["fired_30d"] = fired.get(rule["code"], 0)
        rule["orphaned"] = not (rule.get("owner") or "").strip()
        rule["dormant"] = bool(rule["enabled"]) and rule["fired_30d"] == 0
    return {"ruleset": ruleset[0], "rules": rules}


@router.patch("/rules/{code}")
def update_rule(
    code: str,
    body: RuleUpdateIn,
    user: dict = Depends(requires(Permission.ADMIN_RULES)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Enable, disable or retune a rule without a code change.

    The change is versioned by creating a new ruleset rather than editing the
    active one in place: decisions record the ruleset version that applied
    (FR-042), and mutating a version that decisions already point at would make
    those decisions unreproducible — quietly, and only discoverably months later.
    """
    active = _rows(conn, "SELECT id, version FROM rulesets WHERE is_active LIMIT 1")
    if not active:
        raise HTTPException(500, "no active ruleset")
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
        raise HTTPException(404, f"unknown rule {code}")

    new_version = active[0]["version"] + 1
    with conn.cursor() as cur:
        cur.execute("UPDATE rulesets SET is_active = false WHERE is_active")
        cur.execute(
            """
            INSERT INTO rulesets (version, notes, created_by, is_active)
            VALUES (%s, %s, %s, true) RETURNING id
            """,
            (new_version, f"{code} updated by {user['email']}", user["id"]),
        )
        row = cur.fetchone()
        new_id = int(row["id"] if isinstance(row, dict) else row[0])

        for rule in old:
            params = rule["params"]
            if isinstance(params, str):
                params = json.loads(params)
            enabled = rule["enabled"]
            severity = rule["severity"]
            # Governance travels with every version (D69e), so an unchanged
            # rule keeps its owner and review date across unrelated edits.
            owner, rationale = rule["owner"], rule["rationale"]
            approved_by, approved_at = rule["approved_by"], rule["approved_at"]
            next_review_at = rule["next_review_at"]
            if rule["code"] == code:
                if body.enabled is not None:
                    enabled = body.enabled
                if body.params is not None:
                    params = body.params
                if body.severity is not None:
                    severity = body.severity
                if body.owner is not None:
                    owner = body.owner
                if body.rationale:
                    rationale = body.rationale
                    approved_by = user["email"]
                    approved_at = datetime.now(timezone.utc)
                    next_review_at = approved_at + REVIEW_INTERVAL
            cur.execute(
                """
                INSERT INTO rule_configs (ruleset_id, code, power, severity, enabled, params,
                                          owner, rationale, approved_by, approved_at, next_review_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (new_id, rule["code"], rule["power"], severity, enabled, json.dumps(params or {}),
                 owner, rationale, approved_by, approved_at, next_review_at),
            )

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="RULE_UPDATED",
        object_type="rule",
        object_id=code,
        from_state=json.dumps(
            {"enabled": current["enabled"], "severity": current["severity"], "params": current["params"]},
            default=str,
        ),
        to_state=json.dumps(
            {
                "enabled": body.enabled if body.enabled is not None else current["enabled"],
                "severity": body.severity or current["severity"],
                "params": body.params if body.params is not None else current["params"],
            },
            default=str,
        ),
        payload={"new_ruleset_version": new_version, "rationale": body.rationale,
                 "owner": body.owner},
    )
    return list_rules(user=user, conn=conn)


# ---------------------------------------------------------------------------
# Thresholds (FR-041, FR-042)
# ---------------------------------------------------------------------------


@router.get("/thresholds")
def list_thresholds(
    user: dict = Depends(requires(Permission.ADMIN_THRESHOLDS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return {
        "versions": _rows(
            conn,
            """
            SELECT id, version, p_monitor, p_review, p_hold, alert_min_level,
                   notes, created_at, is_active
              FROM threshold_sets ORDER BY version DESC LIMIT 50
            """,
        )
    }


@router.post("/thresholds")
def create_thresholds(
    body: ThresholdsIn,
    user: dict = Depends(requires(Permission.ADMIN_THRESHOLDS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """New version, activated. Never an in-place edit, for the same reason as rules."""
    if not (body.p_monitor <= body.p_review <= body.p_hold):
        raise HTTPException(400, "thresholds must be ordered: monitor <= review <= hold")

    old = _rows(
        conn,
        "SELECT version, p_monitor, p_review, p_hold, alert_min_level FROM threshold_sets WHERE is_active",
    )
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
            RETURNING id, version
            """,
            (
                version,
                body.p_monitor,
                body.p_review,
                body.p_hold,
                body.alert_min_level,
                body.notes,
                user["id"],
            ),
        )
        created = cur.fetchone()

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="THRESHOLDS_CHANGED",
        object_type="threshold_set",
        object_id=version,
        from_state=json.dumps(old[0], default=str) if old else None,
        to_state=json.dumps(body.model_dump(), default=str),
    )
    return dict(created) if not isinstance(created, dict) else created


# ---------------------------------------------------------------------------
# Rule lists (D11a)
# ---------------------------------------------------------------------------


@router.get("/lists")
def get_lists(
    kind: str | None = Query(None),
    user: dict = Depends(requires(Permission.ADMIN_LISTS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    sql = """
        SELECT l.id, l.kind, l.token, l.account_token, l.note, l.added_at,
               u.display_name AS added_by
          FROM beneficiary_lists l LEFT JOIN users u ON u.id = l.added_by
    """
    params: tuple = ()
    if kind:
        sql += " WHERE l.kind = %s"
        params = (kind,)
    sql += " ORDER BY l.added_at DESC LIMIT 500"
    return {"items": _rows(conn, sql, params)}


@router.post("/lists")
def add_list_entry(
    body: ListEntryIn,
    user: dict = Depends(requires(Permission.ADMIN_LISTS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Entries are supplied as raw account identifiers and tokenised here.

    The list never holds an account number — the same one-way boundary as
    ingestion (D9c), so an administrator can add a sanctioned destination
    without Risk Radar acquiring the ability to resolve one.
    """
    if body.kind == "ALLOWLIST" and not body.account_id:
        raise HTTPException(400, "ALLOWLIST entries are scoped to one account")
    token = account_token(body.beneficiary_account_id)
    scope = account_token(body.account_id) if body.account_id else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO beneficiary_lists (kind, token, account_token, note, added_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (body.kind, token, scope, body.note, user["id"]),
        )
        row = cur.fetchone()

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="LIST_ENTRY_ADDED",
        object_type="beneficiary_list",
        object_id=body.kind,
        to_state=token,
        payload={"scoped_to_account": bool(scope), "note": body.note},
    )
    return {"id": (row["id"] if isinstance(row, dict) else row[0]) if row else None, "token": token}


@router.delete("/lists/{entry_id}")
def remove_list_entry(
    entry_id: int,
    user: dict = Depends(requires(Permission.ADMIN_LISTS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    existing = _rows(conn, "SELECT kind, token FROM beneficiary_lists WHERE id = %s", (entry_id,))
    if not existing:
        raise HTTPException(404, "not found")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM beneficiary_lists WHERE id = %s", (entry_id,))
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="LIST_ENTRY_REMOVED",
        object_type="beneficiary_list",
        object_id=existing[0]["kind"],
        from_state=existing[0]["token"],
    )
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Models (D15a, D15b)
# ---------------------------------------------------------------------------


@router.get("/models")
def list_models(
    user: dict = Depends(requires(Permission.ADMIN_MODELS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return {
        "items": _rows(
            conn,
            """
            SELECT id, name, version, artifact_hash, feature_spec_version, calibration,
                   metrics, trained_at, created_at, is_active, promoted_at
              FROM model_versions ORDER BY created_at DESC
            """,
        )
    }


@router.post("/models/promote")
def promote_model(
    body: PromoteModelIn,
    user: dict = Depends(requires(Permission.ADMIN_MODELS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Promotion is repointing a pointer, audited, with the comparison attached.

    Rollback is repointing it back — not a redeploy. ADMIN executes; the metrics
    are the authority (D15b).
    """
    target = _rows(
        conn, "SELECT id, name, version, metrics FROM model_versions WHERE id = %s",
        (body.model_version_id,),
    )
    if not target:
        raise HTTPException(404, "model version not found")
    previous = _rows(conn, "SELECT id, name, version FROM model_versions WHERE is_active")

    with conn.cursor() as cur:
        cur.execute("UPDATE model_versions SET is_active = false WHERE is_active")
        cur.execute(
            "UPDATE model_versions SET is_active = true, promoted_at = now(), promoted_by = %s WHERE id = %s",
            (user["id"], body.model_version_id),
        )
    registry.invalidate_cache()

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="MODEL_PROMOTED",
        object_type="model_version",
        object_id=body.model_version_id,
        from_state=f"{previous[0]['name']}:{previous[0]['version']}" if previous else None,
        to_state=f"{target[0]['name']}:{target[0]['version']}",
        payload={"comparison": body.comparison or {}, "metrics": target[0]["metrics"]},
    )
    return {"status": "promoted", "active": target[0]}


# ---------------------------------------------------------------------------
# Users and API keys
# ---------------------------------------------------------------------------


@router.get("/users")
def list_users(
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return {
        "items": _rows(
            conn,
            """
            SELECT id, email, display_name, role, totp_enabled, active, created_at
              FROM users WHERE NOT is_system ORDER BY id
            """,
        )
    }


@router.post("/users")
def create_user(
    email: str,
    display_name: str,
    role: str,
    password: str,
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    if role not in ("ANALYST", "FRAUD_OPS_LEAD", "INFOSEC_ANALYST", "ADMIN"):
        raise HTTPException(400, "invalid role")
    secret = new_totp_secret()
    # D12a: mandatory for leads and admins, default-on for analysts. So: on.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, display_name, password_hash, role, totp_secret, totp_enabled)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING id
            """,
            (email, display_name, hash_password(password), role, secret),
        )
        row = cur.fetchone()
        uid = int(row["id"] if isinstance(row, dict) else row[0])

    chain.append(
        conn,
        actor_user_id=user["id"],
        action="USER_CREATED",
        object_type="user",
        object_id=uid,
        to_state=role,
        payload={"email": email, "mfa_required": mfa_required(role)},
    )
    # The secret is returned exactly once, at creation, and never stored anywhere
    # the admin can read it again.
    return {"id": uid, "totp_uri": totp_uri(secret, email)}


@router.post("/api-keys")
def create_api_key(
    name: str,
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    raw = new_api_key()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash) VALUES (%s, %s) RETURNING id",
            (name, hash_api_key(raw)),
        )
        row = cur.fetchone()
    chain.append(
        conn,
        actor_user_id=user["id"],
        action="API_KEY_CREATED",
        object_type="api_key",
        object_id=row["id"] if isinstance(row, dict) else row[0],
        payload={"name": name},
    )
    return {"api_key": raw, "note": "shown once; only its hash is stored"}


@router.get("/api-keys")
def list_api_keys(
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """D87: which callers can post transactions, and when each last did."""
    return {"keys": _rows(conn, "SELECT id, name, active, created_at, last_used_at FROM api_keys ORDER BY id")}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(
    key_id: int,
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """D87: revoke, never delete: the key's history stays attributable. Takes effect on the next request."""
    rows = _rows(conn, "UPDATE api_keys SET active = false WHERE id = %s AND active RETURNING id, name", (key_id,))
    if not rows:
        raise HTTPException(404, "no active API key with that id")
    chain.append(conn, actor_user_id=user["id"], action="API_KEY_REVOKED", object_type="api_key",
                 object_id=key_id, payload={"name": rows[0]["name"]})
    return {"revoked": rows[0]}


# ---------------------------------------------------------------------------
# Audit (D12c)
# ---------------------------------------------------------------------------


@router.get("/audit")
def read_audit(
    object_type: str | None = None,
    object_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    user: dict = Depends(requires(Permission.AUDIT_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    where, params = ["1=1"], {}
    if object_type:
        where.append("object_type = %(ot)s")
        params["ot"] = object_type
    if object_id:
        where.append("object_id = %(oid)s")
        params["oid"] = object_id
    params["limit"] = limit
    return {
        "items": _rows(
            conn,
            f"""
            SELECT a.id, a.occurred_at, a.action, a.object_type, a.object_id,
                   a.from_state, a.to_state, a.payload, a.prev_hash, a.hash,
                   u.display_name AS actor, u.role AS actor_role
              FROM audit_log a JOIN users u ON u.id = a.actor_user_id
             WHERE {' AND '.join(where)}
             ORDER BY a.id DESC LIMIT %(limit)s
            """,
            params,
        )
    }


@router.get("/audit/verify")
def verify_audit(
    user: dict = Depends(requires(Permission.AUDIT_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Walk the chain and report the first break.

    Exposed so tamper-evidence is demonstrable rather than asserted: this is the
    endpoint to call in the defence, right after showing that the application
    role cannot UPDATE or DELETE the table it writes to.
    """
    return chain.verify(conn)

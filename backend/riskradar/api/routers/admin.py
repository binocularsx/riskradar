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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import governance
from ...audit import chain
from ...security.passwords import hash_password, new_api_key
from ...security.rbac import Permission, mfa_required
from ...security.tokens import account_token, hash_api_key
from ..deps import current_user, get_conn, requires
from ..schemas import ConfigDecisionIn, ListEntryIn, PromoteModelIn, RuleUpdateIn, ThresholdsIn

router = APIRouter(prefix="/v1/admin", tags=["admin"])


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
    """Propose a rule change (D96). A *different* administrator approves it, and
    only then is a new ruleset version published.

    Until D96 this applied the change directly, and `approved_by` recorded the
    one person who made it (D69e). Enabling, disabling or retuning a rule changes
    how sensitive detection is, so it now takes two administrators — the same
    maker-checker as a fraud finding (D93). The change is still versioned rather
    than edited in place (FR-042); that just happens on approval.
    """
    active = _rows(conn, "SELECT id, version FROM rulesets WHERE is_active LIMIT 1")
    if not active:
        raise HTTPException(500, "no active ruleset")
    current = next(
        (
            r
            for r in _rows(
                conn,
                "SELECT code, power, severity, enabled, params FROM rule_configs WHERE ruleset_id = %s",
                (active[0]["id"],),
            )
            if r["code"] == code
        ),
        None,
    )
    if not current:
        raise HTTPException(404, f"unknown rule {code}")
    if not (body.rationale and body.rationale.strip()):
        raise HTTPException(422, "a rationale is required to propose a rule change")

    changes = []
    if body.enabled is not None and body.enabled != current["enabled"]:
        changes.append("enable" if body.enabled else "disable")
    if body.params is not None:
        changes.append("retune " + ", ".join(body.params))
    if body.severity is not None:
        changes.append(f"severity → {body.severity}")
    summary = f"{code}: " + ("; ".join(changes) if changes else "no-op") + f" (ruleset v{active[0]['version']})"

    payload = {
        "code": code,
        "enabled": body.enabled,
        "params": body.params,
        "severity": body.severity,
        "owner": body.owner,
    }
    try:
        return governance.propose(
            conn, user,
            change_type="RULE_UPDATE",
            target=code,
            summary=summary,
            payload=payload,
            before_snapshot={
                "ruleset_version": active[0]["version"],
                "enabled": current["enabled"],
                "severity": current["severity"],
                "params": current["params"],
            },
            rationale=body.rationale,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


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
    """Propose a new threshold version (D96). A different administrator approves
    it, and only then is it published and activated.

    Thresholds set how much fraud the desk sees against its alert budget (D11d);
    moving them alone is exactly the single-hand tuning D69e closed for rules,
    so it now takes a second signature too. Still versioned, never edited in
    place — that happens on approval.
    """
    if not (body.p_monitor <= body.p_review <= body.p_hold):
        raise HTTPException(400, "thresholds must be ordered: monitor <= review <= hold")
    if not (body.notes and body.notes.strip()):
        raise HTTPException(422, "a reason (notes) is required to propose a threshold change")

    old = _rows(
        conn,
        "SELECT version, p_monitor, p_review, p_hold, alert_min_level FROM threshold_sets WHERE is_active",
    )
    summary = (
        f"thresholds → monitor {body.p_monitor:g} / review {body.p_review:g} / "
        f"hold {body.p_hold:g}, alert ≥ {body.alert_min_level}"
    )
    try:
        return governance.propose(
            conn, user,
            change_type="THRESHOLD_PUBLISH",
            target="thresholds",
            summary=summary,
            payload=body.model_dump(),
            before_snapshot=old[0] if old else {},
            rationale=body.notes,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


# ---------------------------------------------------------------------------
# Privileged-access maker-checker: the second administrator (D96)
# ---------------------------------------------------------------------------


@router.get("/change-requests")
def list_change_requests(
    state: str | None = Query(None, description="PENDING (default view), or a specific state"),
    user: dict = Depends(requires(Permission.METRICS_READ)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The detection-tuning changes awaiting or having had a second signature.

    Readable by anyone who can already read admin metrics; deciding one still
    needs the change's own permission (and not being its proposer).
    """
    states = (state,) if state else ("PENDING",)
    return {"items": governance.list_requests(conn, states=states)}


@router.post("/change-requests/{request_id}/decision")
def decide_change_request(
    request_id: int,
    body: ConfigDecisionIn,
    user: dict = Depends(current_user),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Approve, reject or return a proposed change (D96).

    No fixed permission on the route: `governance.decide` checks that the actor
    holds the *change's own* permission (ADMIN_RULES or ADMIN_THRESHOLDS) and is
    not the proposer, so one guard cannot be right for both change types.
    """
    try:
        return governance.decide(
            conn, user, request_id=request_id, action=body.action, reason=body.reason
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


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
    if not (body.note and body.note.strip()):
        raise HTTPException(422, "a reason (note) is required to propose a list entry")
    # Tokenise at proposal time (D9c): the change request never holds an account
    # number, only the one-way tokens the list will hold.
    token = account_token(body.beneficiary_account_id)
    scope = account_token(body.account_id) if body.account_id else None
    summary = f"add {body.kind} entry" + (" (scoped)" if scope else "")
    try:
        return governance.propose(
            conn, user,
            change_type="LIST_ADD", target=token, summary=summary,
            payload={"kind": body.kind, "token": token, "account_token": scope, "note": body.note},
            before_snapshot={"kind": body.kind, "scoped_to_account": bool(scope)},
            rationale=body.note,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


@router.delete("/lists/{entry_id}")
def remove_list_entry(
    entry_id: int,
    reason: str = Query(..., min_length=1, description="why the entry is being removed"),
    user: dict = Depends(requires(Permission.ADMIN_LISTS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Propose removing a list entry (D98). Removing a sanctions or known-mule
    entry unprotects, so a second administrator approves it."""
    existing = _rows(conn, "SELECT kind, token FROM beneficiary_lists WHERE id = %s", (entry_id,))
    if not existing:
        raise HTTPException(404, "not found")
    try:
        return governance.propose(
            conn, user,
            change_type="LIST_REMOVE", target=str(entry_id),
            summary=f"remove {existing[0]['kind']} entry #{entry_id}",
            payload={"entry_id": entry_id},
            before_snapshot={"kind": existing[0]["kind"], "token": existing[0]["token"]},
            rationale=reason,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


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
    """Propose promoting a model (D98). Promotion repoints the active-model
    pointer and changes the live scoring engine, so a *different* administrator
    approves it; the metrics are still the authority (D15b). Rollback is
    proposing the previous version back — not a redeploy.
    """
    target = _rows(
        conn, "SELECT id, name, version FROM model_versions WHERE id = %s",
        (body.model_version_id,),
    )
    if not target:
        raise HTTPException(404, "model version not found")
    if not (body.reason and body.reason.strip()):
        raise HTTPException(422, "a reason is required to propose a model promotion")
    previous = _rows(conn, "SELECT name, version FROM model_versions WHERE is_active")
    summary = (f"promote {target[0]['name']}:{target[0]['version']}"
               + (f" over {previous[0]['name']}:{previous[0]['version']}" if previous else ""))
    try:
        return governance.propose(
            conn, user,
            change_type="MODEL_PROMOTE", target=str(body.model_version_id), summary=summary,
            payload={"model_version_id": body.model_version_id, "comparison": body.comparison or {}},
            before_snapshot={"active": f"{previous[0]['name']}:{previous[0]['version']}" if previous else None},
            rationale=body.reason,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


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
    reason: str,
    user: dict = Depends(requires(Permission.ADMIN_USERS)),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Propose a new user (D99). A *different* administrator approves it, and only
    then does the account exist — creating an ADMIN is privilege escalation and
    should never be one person's act.

    The password is hashed here, at proposal time, so no plaintext is ever
    stored; the TOTP secret is generated at approval, so no authenticator secret
    waits in the change-request payload. The provisioning URI comes back to the
    approver.
    """
    if role not in ("ANALYST", "FRAUD_OPS_LEAD", "INFOSEC_ANALYST", "ADMIN"):
        raise HTTPException(400, "invalid role")
    if _rows(conn, "SELECT 1 FROM users WHERE lower(email) = lower(%s)", (email,)):
        raise HTTPException(409, "a user with that email already exists")
    try:
        return governance.propose(
            conn, user,
            change_type="USER_CREATE", target=email.lower(),
            summary=f"create {role} {email}",
            payload={"email": email, "display_name": display_name, "role": role,
                     "password_hash": hash_password(password)},
            before_snapshot={"role": role, "mfa_required": mfa_required(role)},
            rationale=reason,
        )
    except governance.MakerCheckerError as exc:
        raise HTTPException(exc.status_code, exc.detail)


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

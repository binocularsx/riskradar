"""D96: privileged-access maker-checker for detection tuning (plan §3, FR-308).

The apply path publishes a new ruleset/threshold version and flips `is_active`,
which would disturb the running demo, so the end-to-end apply is exercised on the
rolled-back `conn` fixture (nothing commits). The HTTP tests cover only the paths
that do not apply — proposing, the proposer being refused their own approval, and
a rejection — so the suite never changes the live detection configuration.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from riskradar import governance
from riskradar.config import settings
from riskradar.security.rbac import permissions_for

from conftest import login

RATIONALE = "disabling this rule to exercise the maker-checker control"


def _actor(conn, email: str) -> dict:
    row = conn.execute(
        "SELECT id, email, role FROM users WHERE email = %s", (email,)
    ).fetchone()
    return {
        "id": row["id"],
        "email": row["email"],
        "permissions": sorted(str(p) for p in permissions_for(row["role"])),
    }


def _active_ruleset(conn):
    return conn.execute("SELECT id, version FROM rulesets WHERE is_active").fetchone()


def _propose_rule_toggle(conn, maker, rule):
    return governance.propose(
        conn, maker,
        change_type="RULE_UPDATE",
        target=rule["code"],
        summary=f"{rule['code']}: toggle enabled",
        payload={"code": rule["code"], "enabled": not rule["enabled"],
                 "params": None, "severity": None, "owner": None},
        before_snapshot={"ruleset_version": _active_ruleset(conn)["version"]},
        rationale=RATIONALE,
    )


# ---------------------------------------------------------------------------
# The control, end to end, on a rolled-back connection
# ---------------------------------------------------------------------------


def test_a_rule_change_takes_two_administrators(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    before = _active_ruleset(conn)
    rule = conn.execute(
        "SELECT code, enabled FROM rule_configs WHERE ruleset_id = %s AND power = 'ESCALATE' LIMIT 1",
        (before["id"],),
    ).fetchone()

    res = _propose_rule_toggle(conn, maker, rule)
    request_id = res["request"]["id"]
    assert res["status"] == "pending"
    # Proposing does not apply: the active ruleset is unchanged.
    assert _active_ruleset(conn)["version"] == before["version"]

    # The proposer cannot approve their own change.
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 403

    # A different administrator approves — now it applies.
    out = governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    assert out["status"] == "approved"
    after = _active_ruleset(conn)
    assert after["version"] == before["version"] + 1
    changed = conn.execute(
        "SELECT enabled, approved_by FROM rule_configs WHERE ruleset_id = %s AND code = %s",
        (after["id"], rule["code"]),
    ).fetchone()
    assert changed["enabled"] == (not rule["enabled"])
    # D69e closed: approved_by is the second signature, not the maker.
    assert changed["approved_by"] == "admin2@riskradar.local"


def test_a_threshold_change_applies_only_on_approval(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    active = conn.execute(
        "SELECT version, p_monitor, p_review, p_hold, alert_min_level FROM threshold_sets WHERE is_active"
    ).fetchone()
    payload = {
        "p_monitor": float(active["p_monitor"]),
        "p_review": float(active["p_review"]),
        "p_hold": float(active["p_hold"]),
        "alert_min_level": active["alert_min_level"],
        "notes": "republishing the same thresholds to test the approval path",
    }
    res = governance.propose(
        conn, maker,
        change_type="THRESHOLD_PUBLISH", target="thresholds",
        summary="thresholds unchanged", payload=payload,
        before_snapshot={"version": active["version"]}, rationale=payload["notes"],
    )
    assert conn.execute(
        "SELECT version FROM threshold_sets WHERE is_active"
    ).fetchone()["version"] == active["version"]

    governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    assert conn.execute(
        "SELECT version FROM threshold_sets WHERE is_active"
    ).fetchone()["version"] == active["version"] + 1


def test_reject_records_the_verdict_without_applying(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    before = _active_ruleset(conn)
    rule = conn.execute(
        "SELECT code, enabled FROM rule_configs WHERE ruleset_id = %s LIMIT 1", (before["id"],)
    ).fetchone()
    res = _propose_rule_toggle(conn, maker, rule)

    out = governance.decide(
        conn, checker, request_id=res["request"]["id"], action="REJECT",
        reason="not warranted by the evidence",
    )
    assert out["status"] == "rejected"
    assert out["applied_version"] is None
    assert _active_ruleset(conn)["version"] == before["version"]


def test_a_reject_requires_a_reason(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    rule = conn.execute(
        "SELECT code, enabled FROM rule_configs WHERE ruleset_id = %s LIMIT 1",
        (_active_ruleset(conn)["id"],),
    ).fetchone()
    res = _propose_rule_toggle(conn, maker, rule)
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, checker, request_id=res["request"]["id"], action="REJECT")
    assert exc.value.status_code == 422


def test_a_short_rationale_is_refused(conn):
    maker = _actor(conn, "admin@riskradar.local")
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.propose(
            conn, maker, change_type="RULE_UPDATE", target="ANY",
            summary="s", payload={"code": "ANY"}, before_snapshot={}, rationale="too short",
        )
    assert exc.value.status_code == 422


def test_an_analyst_permission_cannot_approve_a_rule_change(conn):
    maker = _actor(conn, "admin@riskradar.local")
    analyst = _actor(conn, "analyst@riskradar.local")
    rule = conn.execute(
        "SELECT code, enabled FROM rule_configs WHERE ruleset_id = %s LIMIT 1",
        (_active_ruleset(conn)["id"],),
    ).fetchone()
    res = _propose_rule_toggle(conn, maker, rule)
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, analyst, request_id=res["request"]["id"], action="APPROVE")
    assert exc.value.status_code == 403


def test_the_database_itself_forbids_the_proposer_deciding(conn):
    """The separation is a CHECK, not only application code (like D93)."""
    maker = _actor(conn, "admin@riskradar.local")
    rule = conn.execute(
        "SELECT code, enabled FROM rule_configs WHERE ruleset_id = %s LIMIT 1",
        (_active_ruleset(conn)["id"],),
    ).fetchone()
    res = _propose_rule_toggle(conn, maker, rule)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE config_change_requests SET decided_by = %s, decided_at = now(), state = 'APPROVED' WHERE id = %s",
            (maker["id"], res["request"]["id"]),
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# Over HTTP — only the paths that do not change detection configuration
# ---------------------------------------------------------------------------


def test_proposing_over_http_is_pending_not_applied_and_the_proposer_cannot_approve(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    rules = client.get("/v1/admin/rules").json()
    version_before = rules["ruleset"]["version"]
    code = rules["rules"][0]["code"]

    proposed = client.patch(
        f"/v1/admin/rules/{code}",
        json={"enabled": False, "rationale": RATIONALE},
    )
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["status"] == "pending"
    request_id = body["request"]["id"]

    # Not applied — the active ruleset has not moved.
    assert client.get("/v1/admin/rules").json()["ruleset"]["version"] == version_before

    # It shows in the pending queue.
    pending = client.get("/v1/admin/change-requests").json()["items"]
    assert any(r["id"] == request_id for r in pending)

    # The proposer cannot approve their own change over HTTP either.
    own = client.post(f"/v1/admin/change-requests/{request_id}/decision", json={"action": "APPROVE"})
    assert own.status_code == 403

    # A second administrator rejects it (no apply), which also clears the pending row.
    login(client, "admin2@riskradar.local", "Admin#2026")
    rejected = client.post(
        f"/v1/admin/change-requests/{request_id}/decision",
        json={"action": "REJECT", "reason": "test cleanup — not a real change"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get("/v1/admin/rules").json()["ruleset"]["version"] == version_before


def test_a_non_admin_cannot_propose_a_rule_change(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    r = client.patch("/v1/admin/rules/VELOCITY_BURST_1H", json={"enabled": False, "rationale": RATIONALE})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# D98: the same control on model promotion and the rule lists
# ---------------------------------------------------------------------------


def test_model_promotion_takes_two_administrators(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    inactive = conn.execute(
        "SELECT id, version FROM model_versions WHERE NOT is_active ORDER BY id LIMIT 1"
    ).fetchone()
    if not inactive:
        pytest.skip("no inactive model version to promote")
    active_before = conn.execute("SELECT id FROM model_versions WHERE is_active").fetchone()["id"]

    res = governance.propose(
        conn, maker, change_type="MODEL_PROMOTE", target=str(inactive["id"]),
        summary="promote the retrained model",
        payload={"model_version_id": inactive["id"], "comparison": {}},
        before_snapshot={}, rationale="promoting the retrained model for the test",
    )
    # Not promoted on proposal.
    assert conn.execute("SELECT id FROM model_versions WHERE is_active").fetchone()["id"] == active_before
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=res["request"]["id"], action="APPROVE")
    assert exc.value.status_code == 403

    governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    now = conn.execute("SELECT id, promoted_by FROM model_versions WHERE is_active").fetchone()
    assert now["id"] == inactive["id"]
    assert now["promoted_by"] == checker["id"]


def test_a_list_entry_is_added_only_on_approval(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    token = "tok-add-" + uuid.uuid4().hex[:12]
    res = governance.propose(
        conn, maker, change_type="LIST_ADD", target=token, summary="add SANCTIONED entry",
        payload={"kind": "SANCTIONED", "token": token, "account_token": None, "note": "sanctioned test destination"},
        before_snapshot={}, rationale="adding a sanctioned destination for the test",
    )
    assert conn.execute("SELECT count(*) AS n FROM beneficiary_lists WHERE token = %s", (token,)).fetchone()["n"] == 0
    governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    row = conn.execute("SELECT kind::text AS kind, added_by FROM beneficiary_lists WHERE token = %s", (token,)).fetchone()
    assert row["kind"] == "SANCTIONED"
    assert row["added_by"] == maker["id"]  # the proposer authored it


def test_removing_a_list_entry_takes_two(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    token = "tok-rm-" + uuid.uuid4().hex[:12]
    entry_id = conn.execute(
        "INSERT INTO beneficiary_lists (kind, token, added_by) VALUES ('KNOWN_MULE', %s, %s) RETURNING id",
        (token, maker["id"]),
    ).fetchone()["id"]

    res = governance.propose(
        conn, maker, change_type="LIST_REMOVE", target=str(entry_id), summary=f"remove entry #{entry_id}",
        payload={"entry_id": entry_id}, before_snapshot={"token": token},
        rationale="removing a stale known-mule entry for the test",
    )
    # Still there until approved.
    assert conn.execute("SELECT count(*) AS n FROM beneficiary_lists WHERE id = %s", (entry_id,)).fetchone()["n"] == 1
    governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    assert conn.execute("SELECT count(*) AS n FROM beneficiary_lists WHERE id = %s", (entry_id,)).fetchone()["n"] == 0


def test_proposing_a_list_entry_over_http_is_pending(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    proposed = client.post(
        "/v1/admin/lists",
        json={"kind": "SANCTIONED", "beneficiary_account_id": f"ACC{uuid.uuid4().hex[:10]}",
              "note": "sanctioned destination proposed for the maker-checker test"},
    )
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["status"] == "pending"
    # A second administrator rejects it, so nothing is added and no pending row lingers.
    login(client, "admin2@riskradar.local", "Admin#2026")
    rejected = client.post(
        f"/v1/admin/change-requests/{body['request']['id']}/decision",
        json={"action": "REJECT", "reason": "test cleanup — not a real entry"},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# D99: creating a user, with the credential handled carefully
# ---------------------------------------------------------------------------


def test_creating_a_user_takes_two_administrators(conn):
    from riskradar.security.passwords import hash_password

    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    email = f"newuser_{uuid.uuid4().hex[:10]}@riskradar.local"

    res = governance.propose(
        conn, maker, change_type="USER_CREATE", target=email.lower(),
        summary=f"create ANALYST {email}",
        payload={"email": email, "display_name": "New Analyst", "role": "ANALYST",
                 "password_hash": hash_password("Secret#2026")},
        before_snapshot={"role": "ANALYST"}, rationale="onboarding a new analyst for the test",
    )
    request_id = res["request"]["id"]
    # No account exists on proposal.
    assert conn.execute("SELECT count(*) AS n FROM users WHERE email = %s", (email,)).fetchone()["n"] == 0

    # The request holds a hash, never the plaintext password (D99).
    payload = conn.execute(
        "SELECT payload FROM config_change_requests WHERE id = %s", (request_id,)
    ).fetchone()["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["password_hash"].startswith("$argon2")
    assert "Secret#2026" not in json.dumps(payload)

    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 403

    out = governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    assert out["status"] == "approved"
    # The provisioning URI comes back once, to the approver.
    assert out["totp_uri"].startswith("otpauth://")

    row = conn.execute(
        "SELECT role::text AS role, password_hash, totp_secret, totp_enabled FROM users WHERE email = %s",
        (email,),
    ).fetchone()
    assert row["role"] == "ANALYST"
    assert row["password_hash"].startswith("$argon2")   # a hash, from the request
    assert row["totp_secret"] and row["totp_enabled"]   # the secret was minted at approval


def test_a_non_admin_cannot_propose_a_user(client):
    login(client, "lead@riskradar.local", "OpsLead#2026")
    r = client.post("/v1/admin/users", json={
        "email": f"x_{uuid.uuid4().hex[:8]}@riskradar.local", "display_name": "X",
        "role": "ANALYST", "password": "Passw0rd#2026", "reason": "a sufficiently long reason",
    })
    assert r.status_code == 403


def test_proposing_a_user_over_http_is_pending(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    email = f"proposed_{uuid.uuid4().hex[:8]}@riskradar.local"
    proposed = client.post("/v1/admin/users", json={
        "email": email, "display_name": "Proposed User", "role": "ANALYST",
        "password": "Passw0rd#2026", "reason": "onboarding a proposed analyst for the test",
    })
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["status"] == "pending"
    # The proposer never received a TOTP URI — it is minted only on approval.
    assert "totp_uri" not in body
    # A second admin rejects, so no account is created.
    login(client, "admin2@riskradar.local", "Admin#2026")
    rejected = client.post(
        f"/v1/admin/change-requests/{body['request']['id']}/decision",
        json={"action": "REJECT", "reason": "test cleanup — not a real user"},
    )
    assert rejected.status_code == 200


# ---------------------------------------------------------------------------
# D104 — the rest of an account's life
# ---------------------------------------------------------------------------


def _user_id(conn, email: str) -> int:
    return conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"]


def test_a_role_change_takes_two_administrators(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "infosec@riskradar.local")

    res = governance.propose(
        conn, maker, change_type="USER_ROLE_CHANGE", target=str(uid),
        summary="infosec to analyst",
        payload={"user_id": uid, "email": "infosec@riskradar.local", "role": "ANALYST"},
        before_snapshot={"role": "INFOSEC_ANALYST"},
        rationale="moving them onto the fraud desk for the test",
    )
    request_id = res["request"]["id"]
    # Nothing moves on proposal.
    assert conn.execute("SELECT role::text AS r FROM users WHERE id = %s",
                        (uid,)).fetchone()["r"] == "INFOSEC_ANALYST"

    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 403

    out = governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    assert out["status"] == "approved"
    assert conn.execute("SELECT role::text AS r FROM users WHERE id = %s",
                        (uid,)).fetchone()["r"] == "ANALYST"


def test_an_admin_cannot_propose_a_change_to_their_own_access(client):
    login(client, "admin@riskradar.local", "Admin#2026")
    me = client.get("/v1/auth/me").json()
    r = client.post(f"/v1/admin/users/{me['id']}/role",
                    json={"role": "ANALYST", "reason": "trying to demote myself for the test"})
    assert r.status_code == 403
    r = client.post(f"/v1/admin/users/{me['id']}/active",
                    json={"active": False, "reason": "trying to disable myself for the test"})
    assert r.status_code == 403


def test_an_administrator_cannot_be_disabled_while_only_two_exist(conn):
    """Two admins are seeded (D96), and that pair cannot shrink itself.

    Not because of an arithmetic guard, but because of who is allowed to sign:
    the approver may be neither the proposer nor the subject, so with exactly
    two administrators every proposal to disable one leaves nobody eligible to
    approve it. Removing an administrator means adding a third one first.
    """
    maker = _actor(conn, "admin@riskradar.local")
    subject = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "admin2@riskradar.local")

    res = governance.propose(
        conn, maker, change_type="USER_SET_ACTIVE", target=str(uid),
        summary="disable admin2",
        payload={"user_id": uid, "email": "admin2@riskradar.local", "active": False},
        before_snapshot={"active": True, "role": "ADMIN"},
        rationale="attempting to disable the second administrator for the test",
    )
    request_id = res["request"]["id"]

    # The proposer cannot approve their own proposal (D93).
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 403

    # And the only other administrator is the subject, who cannot sign off a
    # change to their own access.
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, subject, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 409
    assert "their own account" in str(exc.value.detail)

    assert conn.execute("SELECT active FROM users WHERE id = %s", (uid,)).fetchone()["active"]


def test_an_mfa_reset_mints_a_new_secret_only_on_approval(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "analyst@riskradar.local")
    before = conn.execute("SELECT totp_secret FROM users WHERE id = %s",
                          (uid,)).fetchone()["totp_secret"]

    res = governance.propose(
        conn, maker, change_type="USER_MFA_RESET", target=str(uid),
        summary="re-issue authenticator for the analyst",
        payload={"user_id": uid, "email": "analyst@riskradar.local"},
        before_snapshot={"email": "analyst@riskradar.local", "role": "ANALYST"},
        rationale="the analyst lost their phone, for the test",
    )
    request_id = res["request"]["id"]
    # The secret is not minted at proposal, so it cannot sit in the payload.
    payload = conn.execute("SELECT payload FROM config_change_requests WHERE id = %s",
                           (request_id,)).fetchone()["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "totp_secret" not in payload
    assert conn.execute("SELECT totp_secret FROM users WHERE id = %s",
                        (uid,)).fetchone()["totp_secret"] == before

    out = governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    assert out["totp_uri"].startswith("otpauth://")
    after = conn.execute("SELECT totp_secret, totp_enabled FROM users WHERE id = %s",
                         (uid,)).fetchone()
    assert after["totp_secret"] != before      # every old code stops working
    assert after["totp_enabled"]


def test_the_new_user_password_never_travels_in_the_url(client):
    """D104: the fields moved to a request body. A password in a query string
    reaches browser history, the Referer header and every proxy log en route."""
    login(client, "admin@riskradar.local", "Admin#2026")
    r = client.post("/v1/admin/users", params={
        "email": f"q_{uuid.uuid4().hex[:8]}@riskradar.local", "display_name": "Q",
        "role": "ANALYST", "password": "Passw0rd#2026", "reason": "a sufficiently long reason",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# D105 — the code prompt, the password, and the email that identifies an account
# ---------------------------------------------------------------------------


def test_turning_the_code_prompt_off_takes_two_administrators(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "analyst@riskradar.local")
    conn.execute("UPDATE users SET totp_enabled = true WHERE id = %s", (uid,))

    res = governance.propose(
        conn, maker, change_type="USER_SET_MFA", target=str(uid),
        summary="stop asking for a code from analyst@riskradar.local",
        payload={"user_id": uid, "enabled": False},
        before_snapshot={"totp_enabled": True, "role": "ANALYST"},
        rationale="turning the code prompt off for the test",
    )
    request_id = res["request"]["id"]
    assert conn.execute("SELECT totp_enabled FROM users WHERE id = %s",
                        (uid,)).fetchone()["totp_enabled"]

    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, maker, request_id=request_id, action="APPROVE")
    assert exc.value.status_code == 403

    governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    assert not conn.execute("SELECT totp_enabled FROM users WHERE id = %s",
                            (uid,)).fetchone()["totp_enabled"]


def test_the_prompt_cannot_be_turned_on_for_an_account_with_no_secret(conn):
    """Requiring a code from somebody who holds none would lock them out."""
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "analyst@riskradar.local")
    conn.execute("UPDATE users SET totp_enabled = false, totp_secret = NULL WHERE id = %s", (uid,))

    res = governance.propose(
        conn, maker, change_type="USER_SET_MFA", target=str(uid),
        summary="require a code", payload={"user_id": uid, "enabled": True},
        before_snapshot={"totp_enabled": False, "role": "ANALYST"},
        rationale="requiring a code from an account with no secret, for the test",
    )
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    assert exc.value.status_code == 409
    assert "reset their MFA first" in str(exc.value.detail)


def test_the_code_prompt_switch_is_the_one_login_reads(client):
    """The point of D105: the column is no longer inert.

    Under D95 the role forced the prompt and this column was ignored, so an
    administrator could turn MFA "off" and the user would still be asked. The
    change is made and undone here through the database directly, because the
    assertion is about the login path, not about maker-checker.
    """
    import psycopg

    from riskradar.config import settings

    def set_prompt(on: bool) -> None:
        with psycopg.connect(settings().app_dsn) as c:
            c.execute("UPDATE users SET totp_enabled = %s WHERE email = %s",
                      (on, "analyst@riskradar.local"))
            c.commit()

    set_prompt(True)   # the seed has shipped this account both ways
    # With the prompt on, a password alone is not a session.
    r = client.post("/v1/auth/login",
                    json={"email": "analyst@riskradar.local", "password": "Analyst#2026"})
    assert r.status_code == 200 and r.json()["status"] == "mfa_required"

    set_prompt(False)
    try:
        r = client.post("/v1/auth/login",
                        json={"email": "analyst@riskradar.local", "password": "Analyst#2026"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok", "turning the prompt off must actually stop it"
        # And the session works on every subsequent request, which is the gate
        # in api.deps that used to reject a session with no factor.
        me = client.get("/v1/auth/me")
        assert me.status_code == 200 and me.json()["email"] == "analyst@riskradar.local"
    finally:
        set_prompt(True)
        client.post("/v1/auth/logout")


def test_a_password_reset_takes_two_and_replaces_the_hash(conn):
    from riskradar.security.passwords import hash_password, verify_password

    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "analyst@riskradar.local")

    res = governance.propose(
        conn, maker, change_type="USER_PASSWORD_RESET", target=str(uid),
        summary="reset the password for analyst@riskradar.local",
        payload={"user_id": uid, "password_hash": hash_password("Replaced#2026x")},
        before_snapshot={"email": "analyst@riskradar.local", "role": "ANALYST"},
        rationale="the analyst forgot their password, for the test",
    )
    request_id = res["request"]["id"]
    # The plaintext is nowhere in the request.
    payload = conn.execute("SELECT payload FROM config_change_requests WHERE id = %s",
                           (request_id,)).fetchone()["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "Replaced#2026x" not in json.dumps(payload)
    assert payload["password_hash"].startswith("$argon2")

    # Unchanged until a second administrator approves.
    assert verify_password(
        conn.execute("SELECT password_hash FROM users WHERE id = %s", (uid,)).fetchone()["password_hash"],
        "Analyst#2026")

    governance.decide(conn, checker, request_id=request_id, action="APPROVE")
    now = conn.execute("SELECT password_hash FROM users WHERE id = %s", (uid,)).fetchone()["password_hash"]
    assert verify_password(now, "Replaced#2026x")
    assert not verify_password(now, "Analyst#2026")


def test_an_email_correction_moves_the_login_identifier(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "infosec@riskradar.local")

    res = governance.propose(
        conn, maker, change_type="USER_PROFILE_UPDATE", target=str(uid),
        summary="correct the infosec email",
        payload={"user_id": uid, "email": "ngozi.infosec@riskradar.local",
                 "display_name": "Ngozi InfoSec"},
        before_snapshot={"email": "infosec@riskradar.local", "display_name": "Ngozi InfoSec"},
        rationale="correcting a mistyped address on the account, for the test",
    )
    governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    assert conn.execute("SELECT email FROM users WHERE id = %s",
                        (uid,)).fetchone()["email"] == "ngozi.infosec@riskradar.local"


def test_an_email_cannot_collide_with_another_account(conn):
    maker = _actor(conn, "admin@riskradar.local")
    checker = _actor(conn, "admin2@riskradar.local")
    uid = _user_id(conn, "infosec@riskradar.local")

    res = governance.propose(
        conn, maker, change_type="USER_PROFILE_UPDATE", target=str(uid),
        summary="collide the infosec email with the analyst",
        payload={"user_id": uid, "email": "analyst@riskradar.local",
                 "display_name": "Ngozi InfoSec"},
        before_snapshot={"email": "infosec@riskradar.local", "display_name": "Ngozi InfoSec"},
        rationale="attempting to take an address already in use, for the test",
    )
    with pytest.raises(governance.MakerCheckerError) as exc:
        governance.decide(conn, checker, request_id=res["request"]["id"], action="APPROVE")
    assert exc.value.status_code == 409
    assert "already has that email" in str(exc.value.detail)

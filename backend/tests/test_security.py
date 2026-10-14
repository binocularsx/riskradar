"""Separation of duties, audit integrity, and the generator/detector wall.

These are the tests that make PRD §13's claims checkable rather than asserted.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import psycopg
import pytest

from riskradar.audit import chain
from riskradar.config import settings
from riskradar.security.rbac import Permission, has, permissions_for

from conftest import login


# ---------------------------------------------------------------------------
# RBAC (D12b)
# ---------------------------------------------------------------------------


def test_admin_cannot_touch_a_case_at_all():
    """The load-bearing sentence: the account that tunes detection cannot be the
    account that clears what detection misses."""
    for permission in (
        Permission.CASES_READ,
        Permission.CASES_REVIEW,
        Permission.CASES_SUBMIT_OUTCOME,
        Permission.CASES_APPROVE_FRAUD,
        Permission.CASES_CLOSE,
        Permission.CASES_ESCALATE,
    ):
        assert not has("ADMIN", permission), f"ADMIN must not hold {permission}"


def test_analyst_cannot_close_or_administer():
    assert has("ANALYST", Permission.CASES_SUBMIT_OUTCOME)
    # D93: an analyst proposes a fraud finding; a lead decides it.
    assert not has("ANALYST", Permission.CASES_APPROVE_FRAUD)
    assert not has("ANALYST", Permission.CASES_CLOSE)
    assert not has("ANALYST", Permission.ADMIN_THRESHOLDS)
    assert not has("ANALYST", Permission.ADMIN_RULES)


def test_the_infosec_role_no_longer_exists():
    """D111 removed it. An unknown role holds no permission at all, so a session
    that somehow named it could still reach nothing."""
    assert permissions_for("INFOSEC_ANALYST") == frozenset()
    for permission in (Permission.CASES_READ, Permission.CASES_ESCALATE,
                       Permission.CASES_SUBMIT_OUTCOME, Permission.CASES_APPROVE_FRAUD):
        assert not has("INFOSEC_ANALYST", permission)


def test_lead_can_close_but_not_administer():
    assert has("FRAUD_OPS_LEAD", Permission.CASES_CLOSE)
    assert has("FRAUD_OPS_LEAD", Permission.CASES_APPROVE_FRAUD)
    assert not has("FRAUD_OPS_LEAD", Permission.ADMIN_RULES)


def test_separation_is_enforced_over_http_not_just_in_the_ui(client):
    """A hidden button is not a control. The API must refuse too."""
    login(client, "admin@riskradar.local", "Admin#2026")
    r = client.get("/v1/cases")
    assert r.status_code == 403
    assert "separation of duties" in r.json()["detail"]

    # ...and the admin routes it *should* reach still work.
    assert client.get("/v1/admin/rules").status_code == 200


def test_analyst_is_refused_admin_routes(client):
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/admin/rules").status_code == 403
    assert client.get("/v1/cases").status_code == 200


def test_unauthenticated_requests_are_refused(client):
    assert client.get("/v1/cases").status_code == 401
    assert client.get("/v1/auth/me").status_code == 401


def test_login_does_not_reveal_whether_an_account_exists(client):
    unknown = client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
    )
    wrong = client.post(
        "/v1/auth/login", json={"email": "analyst@riskradar.local", "password": "wrong"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_mfa_is_required_for_privileged_roles(client):
    """D12a. Correct password, no code: no session."""
    r = client.post(
        "/v1/auth/login", json={"email": "admin@riskradar.local", "password": "Admin#2026"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "mfa_required"
    assert client.get("/v1/auth/me").status_code == 401


def test_logout_revokes_immediately(client):
    """Revocation is a DELETE — the whole argument against a JWT here."""
    login(client, "analyst@riskradar.local", "Analyst#2026")
    assert client.get("/v1/auth/me").status_code == 200
    client.post("/v1/auth/logout")
    assert client.get("/v1/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Audit (D12c)
# ---------------------------------------------------------------------------


def _db():
    return psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row)


def test_application_role_cannot_rewrite_the_audit_log():
    """The control that carries the tamper-evidence claim.

    Not a trigger, not application code — a database grant. This is the test to
    run in the defence.
    """
    with _db() as c:
        for statement in (
            "UPDATE audit_log SET action = 'TAMPERED'",
            "DELETE FROM audit_log",
            "TRUNCATE audit_log",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(statement)
            c.rollback()


def test_chain_verifies_and_detects_tampering(conn):
    """Append a few rows, verify, then prove a break would be found.

    The tamper is simulated in memory rather than in the table, because the
    grant above makes it impossible to actually perform — which is the point.
    """
    uid = conn.execute("SELECT id FROM users WHERE is_system LIMIT 1").fetchone()["id"]
    for i in range(3):
        chain.append(
            conn,
            actor_user_id=uid,
            action="TEST_EVENT",
            object_type="test",
            object_id=f"{uuid.uuid4().hex[:8]}-{i}",
            payload={"i": i},
        )
    assert chain.verify(conn)["ok"] is True

    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 2"
    ).fetchall()
    tampered = dict(rows[1])
    tampered["action"] = "SOMETHING_ELSE"
    body = chain.canonical_payload(
        tampered["occurred_at"], tampered["actor_user_id"], tampered["action"],
        tampered["object_type"], tampered["object_id"], tampered["from_state"],
        tampered["to_state"], tampered["payload"],
    )
    assert chain._chain_hash(tampered["prev_hash"], body) != tampered["hash"], (
        "editing a field must change the row's hash, or the chain proves nothing"
    )


def test_audit_actor_is_never_null(conn):
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO audit_log (action, object_type, prev_hash, hash) "
            "VALUES ('X','y','a','b')"
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# The generator/detector wall (D10a)
# ---------------------------------------------------------------------------

BACKEND = Path(__file__).resolve().parents[1] / "riskradar"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_detection_code_never_imports_the_simulator():
    """D10a is the integrity control for the whole ML claim.

    If the rules engine could read a generator parameter, the model would be
    learning the thresholds we already have, by hand, and the 0.99 AUC would mean
    nothing. Enforced mechanically because "we agreed not to" is not a control.
    """
    detection = list((BACKEND / "rules").glob("*.py")) + \
        list((BACKEND / "policy").glob("*.py")) + \
        list((BACKEND / "features").glob("*.py")) + \
        list((BACKEND / "model").glob("*.py"))

    offenders = {
        str(p.relative_to(BACKEND)): sorted(m for m in _imports(p) if "sim" in m.lower())
        for p in detection
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"detection code imports simulator modules: {offenders}"


def test_feature_package_never_joins_the_mutable_account_dimension():
    """D22. Point-in-time correctness is enforced by schema shape, not by a test
    that could pass while both paths are wrong — so this checks the shape."""
    for path in (BACKEND / "features").glob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert "from accounts" not in source, (
            f"{path.name} reads the accounts dimension. Joining a mutable table "
            "makes six-week-old decisions reproduce against today's dormancy and "
            "leaks post-fraud state backwards into training."
        )

"""D96: privileged-access maker-checker for detection tuning (plan §3, FR-308).

The apply path publishes a new ruleset/threshold version and flips `is_active`,
which would disturb the running demo, so the end-to-end apply is exercised on the
rolled-back `conn` fixture (nothing commits). The HTTP tests cover only the paths
that do not apply — proposing, the proposer being refused their own approval, and
a rejection — so the suite never changes the live detection configuration.
"""

from __future__ import annotations

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

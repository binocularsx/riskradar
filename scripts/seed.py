"""Seed the reference data the system cannot start without.

Idempotent. Creates:

* the SYSTEM principal, so the audit actor is never null (D12c);
* one user per role, with TOTP enrolled (D12a);
* an API key for the simulator, which queues at the same door a bank would (D8);
* ruleset v1 — the six rules of D25, two of each power;
* threshold set v1 — derived from the alert budget, not from intuition (D11d);
* the stub model, so a fresh database scores rather than alarms.

    python scripts/seed.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import psycopg  # noqa: E402

from riskradar.config import settings  # noqa: E402
from riskradar.features.spec import FEATURE_SPEC_VERSION  # noqa: E402
from riskradar.security.passwords import (  # noqa: E402
    hash_password,
    new_totp_secret,
    totp_uri,
)
from riskradar.security.tokens import hash_api_key  # noqa: E402

# Development credentials. Real deployments create users through the admin API;
# these exist so a fresh clone can be logged into in one command.
USERS = [
    ("analyst@riskradar.local", "Amaka Analyst", "ANALYST", "Analyst#2026"),
    ("lead@riskradar.local", "Femi Fraud-Ops", "FRAUD_OPS_LEAD", "OpsLead#2026"),
    ("infosec@riskradar.local", "Ngozi InfoSec", "INFOSEC_ANALYST", "InfoSec#2026"),
    ("admin@riskradar.local", "Tunde Admin", "ADMIN", "Admin#2026"),
]

SIMULATOR_API_KEY = "rr_dev_simulator_key_do_not_use_in_production"

# D25: six rules, two of each power. Every rule *power* in D11a is retained,
# including suppression — the primary false-positive control, which had to be
# built rather than deferred.
RULES = [
    ("VELOCITY_BURST_1H", "ESCALATE", "HIGH", {"min_count": 10}),  # D62: 5 alone raised 146% of the budget
    (
        "CARD_TESTING_PROBES",
        "ESCALATE",
        "HIGH",
        {"min_decline_rate_24h": 0.5, "min_failed_1h": 3},
    ),
    ("SANCTIONED_BENEFICIARY", "OVERRIDE", "CRITICAL", {}),
    ("KNOWN_MULE_BENEFICIARY", "OVERRIDE", "CRITICAL", {}),
    ("PRE_REGISTERED_BENEFICIARY", "SUPPRESS", "LOW", {}),
    (
        "ESTABLISHED_PAYEE_NORMAL",
        "SUPPRESS",
        "LOW",
        {"min_beneficiary_age_days": 60, "max_amount_ratio": 1.0},
    ),
]

# D11d/D24: these are placeholders until the calibrated model exists, at which
# point scripts/derive_thresholds.py solves them backwards from the 120/day
# alert budget against a held-out scored sample. Written down, versioned, and
# replaced by measurement — never left at "80 sounds high".
THRESHOLDS = {
    "p_monitor": 0.020,
    "p_review": 0.150,
    "p_hold": 0.600,
    "alert_min_level": "MEDIUM",
    "notes": "v1 placeholder pending threshold derivation against the alert budget",
}

CONFIG = {
    "correlation_window_hours": 24,   # D13a
    "alert_budget_per_day": 120,      # D24 — 3 analysts x 40 reviewable alerts
    "analyst_desk_size": 3,
}


def main() -> None:
    printed: list[str] = []
    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            # --- SYSTEM principal ------------------------------------------
            cur.execute(
                """
                INSERT INTO users (email, display_name, role, is_system, active)
                VALUES ('system@riskradar.local', 'Risk Radar (system)', 'SYSTEM', true, true)
                ON CONFLICT (email) DO NOTHING
                """
            )

            # --- human users -----------------------------------------------
            for email, name, role, password in USERS:
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cur.fetchone():
                    continue
                secret = new_totp_secret()
                cur.execute(
                    """
                    INSERT INTO users (email, display_name, password_hash, role,
                                       totp_secret, totp_enabled)
                    VALUES (%s, %s, %s, %s, %s, true)
                    """,
                    (email, name, hash_password(password), role, secret),
                )
                printed.append(f"  {role:<16} {email:<28} {password:<14} {totp_uri(secret, email)}")

            # --- simulator API key -----------------------------------------
            cur.execute(
                """
                INSERT INTO api_keys (name, key_hash) VALUES ('simulator', %s)
                ON CONFLICT (key_hash) DO NOTHING
                """,
                (hash_api_key(SIMULATOR_API_KEY),),
            )

            # --- ruleset v1 -------------------------------------------------
            cur.execute("SELECT id FROM rulesets WHERE is_active")
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO rulesets (version, notes, is_active)
                    VALUES (1, 'initial ruleset — D25: 6 rules, 2 of each power', true)
                    RETURNING id
                    """
                )
                ruleset_id = cur.fetchone()["id"]
                for code, power, severity, params in RULES:
                    cur.execute(
                        """
                        INSERT INTO rule_configs (ruleset_id, code, power, severity, enabled, params)
                        VALUES (%s, %s, %s, %s, true, %s)
                        """,
                        (ruleset_id, code, power, severity, json.dumps(params)),
                    )

            # --- threshold set v1 ------------------------------------------
            cur.execute("SELECT id FROM threshold_sets WHERE is_active")
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO threshold_sets
                        (version, p_monitor, p_review, p_hold, alert_min_level, notes, is_active)
                    VALUES (1, %(p_monitor)s, %(p_review)s, %(p_hold)s,
                            %(alert_min_level)s, %(notes)s, true)
                    """,
                    THRESHOLDS,
                )

            # --- configuration ---------------------------------------------
            for key, value in CONFIG.items():
                cur.execute(
                    """
                    INSERT INTO app_config (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, json.dumps(value)),
                )

            # --- stub model -------------------------------------------------
            # Week 1's constant-returning stub. Kept because it is also the
            # honest state of a fresh database: something must be active, and a
            # constant that says so beats an alarm on every transaction.
            cur.execute("SELECT id FROM model_versions WHERE name = 'stub'")
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO model_versions
                        (name, version, artifact_hash, artifact_path, feature_spec_version,
                         calibration, metrics, is_active)
                    VALUES ('stub', '0.0.1', 'n/a', NULL, %s, 'none', %s, true)
                    """,
                    (
                        FEATURE_SPEC_VERSION,
                        json.dumps(
                            {
                                "note": "constant 0.02; replaced in week 3 by the trained model",
                                "feature_baseline": [0.0] * 12,
                            }
                        ),
                    ),
                )

    print("seed complete")
    if printed:
        print("\n  ROLE             EMAIL                        PASSWORD       TOTP")
        print("\n".join(printed))
        print("\n  TOTP is on for every account (D12a). scripts/totp.py prints a current code.")
    print(f"\n  simulator API key: {SIMULATOR_API_KEY}")


if __name__ == "__main__":
    main()

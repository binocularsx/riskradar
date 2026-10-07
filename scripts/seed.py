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
from riskradar.features.spec import FEATURE_SPEC_VERSION, MODEL_FEATURE_NAMES  # noqa: E402
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
    # D96: detection tuning is maker-checker, so one administrator is not enough
    # — a second proposes-or-approves the other's changes.
    ("admin2@riskradar.local", "Bola Admin", "ADMIN", "Admin#2026"),
]

SIMULATOR_API_KEY = "rr_dev_simulator_key_do_not_use_in_production"
# D109g: the support team's own key, limited to the support endpoints.
SUPPORT_API_KEY = "rr_dev_support_key_do_not_use_in_production"

# D25: six rules, two of each power; D77, D78 and D79 added three escalating rules. Every rule *power* in D11a is retained,
# including suppression — the primary false-positive control, which had to be
# built rather than deferred.
# Chosen by ml/fraud_types_d82.py (fraud-types-d82.json): the most incidents of each rule's
# own fraud type at no more than 5 false alerts a day and precision of at least 0.25.
D82_PARAMS = {
    "SCAM_BENEFICIARY_FANIN": {"min_other_senders": 1, "max_beneficiary_age_days": 3, "min_amount_ratio": 1.5},
    "SIM_SWAP_TRANSFER": {"within_hours": 12, "channels": ["USSD", "MOBILE_APP"], "min_amount_log10": 4.7},
    "DORMANT_ACCOUNT_REACTIVATION": {"min_dormant_days": 60, "min_amount_log10": 5.0},
    "CARD_PRESENT_NEW_REGION_CASHOUT": {"min_count_1h": 3},
}

RULES = [
    # D67: 5 or more in an hour AND the destination first appeared in the bank's
    # traffic within the last day. Speed alone (D62) was mostly traders.
    ("VELOCITY_BURST_1H", "ESCALATE", "HIGH", {"min_count": 5, "new_destination_days": 1, "no_destination_min_count": 10}),
    # D77: a way in taken over (new device bound, SIM or credential changed,
    # failed-login burst), then a payment to a destination new to the account.
    # Two precursors, not one: 7 alerts a day at precision 0.96, against 44 a day
    # at 0.23 for any single one (ml/artifacts/takeover-sequence.json).
    ("ACCOUNT_TAKEOVER_SEQUENCE", "ESCALATE", "HIGH", {"within_hours": 24, "min_failed_logins": 3, "min_precursors": 2}),
    # D78: credits into an account, from many senders, on a day unlike its normal.
    # 3 senders at 5x the account's normal day: 7.3 alerts a day, 2.1 false,
    # 73% of mule rings (ml/artifacts/receiving-side.json).
    ("MULE_INBOUND_FANIN", "ESCALATE", "HIGH", {"min_remitters": 3, "min_count_ratio": 5.0}),
    # D79: the onward payment. At 75/day it lifts mule-ring value flagged from
    # 63.5% to 84.9% and total value detected from 84.8% to 90.4%.
    ("SECOND_LEG_ONWARD_PAYMENT", "ESCALATE", "HIGH",
     {"min_remitters": 3, "min_count_ratio": 5.0, "max_minutes_since_credit": 180, "min_pass_through": 0.5}),
    # D82: four fraud types a Nigerian desk loses money to that the first three
    # did not cover. Parameters from ml/fraud_types_d82.py; identical to
    # ml/offline_rules.py, which a test holds equal.
    ("SCAM_BENEFICIARY_FANIN", "ESCALATE", "HIGH", D82_PARAMS["SCAM_BENEFICIARY_FANIN"]),
    ("SIM_SWAP_TRANSFER", "ESCALATE", "HIGH", D82_PARAMS["SIM_SWAP_TRANSFER"]),
    ("DORMANT_ACCOUNT_REACTIVATION", "ESCALATE", "HIGH", D82_PARAMS["DORMANT_ACCOUNT_REACTIVATION"]),
    ("CARD_PRESENT_NEW_REGION_CASHOUT", "ESCALATE", "HIGH", D82_PARAMS["CARD_PRESENT_NEW_REGION_CASHOUT"]),
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

# D69e: the rules inventory. Every rule has an owner, a reason and a review date
# (base PRD FR-504). Kept beside RULES so a new rule cannot be added without one.
RULE_GOVERNANCE = {
    "VELOCITY_BURST_1H": ("Chidera", "D67: a burst of 5+ payments in an hour to a destination new to the bank; 10+ with no destination. Speed alone was mostly traders.", "2026-09-11"),
    "ACCOUNT_TAKEOVER_SEQUENCE": ("Chidera", "D77: two takeover precursors within 24h (device bound, SIM or credential changed, failed logins), then a new destination. 7 alerts/day, precision 0.96.", "2026-09-14"),
    "MULE_INBOUND_FANIN": ("Chidera", "D78: credits from 3+ senders on a day 5x the account's normal; the receiving side of a mule ring. 7.3 alerts/day, 73% of rings.", "2026-09-15"),
    "SECOND_LEG_ONWARD_PAYMENT": ("Chidera", "D79: an account that took a credit fan-in, paying half or more of it on within 3 hours. Mule value flagged 63.5% -> 84.9% at 75/day.", "2026-09-15"),
    "CARD_TESTING_PROBES": ("Chidera", "D21, D62b: refused small card attempts before a large one. Right 99.8% of the time.", "2026-09-09"),
    "SCAM_BENEFICIARY_FANIN": ("Chidera", "D82: a new destination several other customers paid today; the collection account of a social-engineering scam.", "2026-09-17"),
    "SIM_SWAP_TRANSFER": ("Chidera", "D82: a new destination within hours of the SIM changing; the USSD drain after a SIM swap.", "2026-09-17"),
    "DORMANT_ACCOUNT_REACTIVATION": ("Chidera", "D82: a long-dormant account sending a large sum somewhere new.", "2026-09-17"),
    "CARD_PRESENT_NEW_REGION_CASHOUT": ("Chidera", "D82: card-present attempts in a run, in a region the customer has not used; a cloned card cashed out.", "2026-09-17"),
    "SANCTIONED_BENEFICIARY": ("Chidera", "D11a: a sanctioned destination is not a matter of probability.", "2026-09-09"),
    "KNOWN_MULE_BENEFICIARY": ("Chidera", "D11a: a destination an analyst confirmed as fraudulent.", "2026-09-09"),
    "PRE_REGISTERED_BENEFICIARY": ("Chidera", "D11a: the customer set this payee up on purpose; the main false-alarm control.", "2026-09-09"),
    "ESTABLISHED_PAYEE_NORMAL": ("Chidera", "D64: a long-standing payee receiving a normal amount; never fires without a payee.", "2026-09-11"),
}
assert set(RULE_GOVERNANCE) == {code for code, *_ in RULES}, "every rule needs an owner (D69e)"

# D11d/D24: these are placeholders until the calibrated model exists, at which
# point scripts/derive_thresholds.py solves them backwards from the 75/day
# alert budget (D76) against a held-out scored sample. Written down, versioned, and
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
    # D76 (was 120, D24): 3 analysts x 25 alerts. 6.6 false alerts per incident
    # against 15 at 120, and room in the day for the customer contact and
    # regulatory clock work of D71 and D73.
    "alert_budget_per_day": 75,
    "analyst_desk_size": 3,
    # D92 (per-rule shares): a daily cap per rule, so a noisy low-precision rule
    # (the velocity burst on a market day) cannot spend the rules envelope and
    # defer a better one behind it. cap = ceil(measured alerts/day x 1.5);
    # derived by ml/rule_allocation.py from the published firing rates. OVERRIDE
    # vetoes (sanctions, known mule) are always mandatory and never capped.
    "alert_budget_rule_caps": {
        "VELOCITY_BURST_1H": 32,
        "CARD_TESTING_PROBES": 17,
        "SIM_SWAP_TRANSFER": 12,
        "MULE_INBOUND_FANIN": 11,
        "ACCOUNT_TAKEOVER_SEQUENCE": 9,
        "SECOND_LEG_ONWARD_PAYMENT": 8,
        "DORMANT_ACCOUNT_REACTIVATION": 5,
        "CARD_PRESENT_NEW_REGION_CASHOUT": 5,
        "SCAM_BENEFICIARY_FANIN": 2,
    },
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
            # D109g: the simulator also plays the support team, with its own key.
            cur.execute(
                """
                INSERT INTO api_keys (name, key_hash, scope) VALUES ('support team (simulated)', %s, 'SUPPORT')
                ON CONFLICT (key_hash) DO NOTHING
                """,
                (hash_api_key(SUPPORT_API_KEY),),
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
                    owner, rationale, approved_at = RULE_GOVERNANCE[code]
                    cur.execute(
                        """
                        INSERT INTO rule_configs (ruleset_id, code, power, severity, enabled, params,
                                                  owner, rationale, approved_by, approved_at,
                                                  next_review_at)
                        VALUES (%s, %s, %s, %s, true, %s, %s, %s, 'team decision log',
                                %s::timestamptz, %s::timestamptz + interval '90 days')
                        """,
                        (ruleset_id, code, power, severity, json.dumps(params),
                         owner, rationale, approved_at, approved_at),
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
                                "feature_baseline": [0.0] * len(MODEL_FEATURE_NAMES),
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
    print(f"  support API key:   {SUPPORT_API_KEY}")


if __name__ == "__main__":
    main()

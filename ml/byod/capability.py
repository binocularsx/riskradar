"""What this dataset can feed, before a single number is computed.

Plain English
-------------
A detector can only be as good as the facts it is given. If a dataset has no
declines, the card-testing rule cannot fire; if every sender appears once, no
"unusual for this customer" measurement means anything. A weak result on such
data is a fact about the data, and the report has to say so before it shows the
result, or the result will be read as a fact about the detector.

Two checks, one before and one after:

* **Declared**: which canonical fields the mapping supplies, and therefore which
  measurements and rules could work at all.
* **Measured**: which measurements came out constant on this data anyway (the
  PaySim lesson: a sender column exists, but every sender appears once).
"""

from __future__ import annotations

import numpy as np

from riskradar.features.spec import FEATURE_NAMES

from .mapping import Mapping

# Canonical inputs each measurement reads. "history" means the key must repeat
# over time; "events" means a sign-in/device/SIM event feed, which a transaction
# file does not carry.
REQUIRES: dict[str, tuple[str, ...]] = {
    "amount_log10": ("amount",),
    "amount_ratio_to_account_p95_30d": ("amount", "history"),
    "txn_count_1h_account": ("history",),
    "approved_value_ratio_24h_vs_daily_mean_30d": ("amount", "history"),
    "failed_attempts_1h_account": ("auth_result", "history"),
    "decline_rate_24h_account": ("auth_result", "history"),
    "distinct_beneficiaries_1h_account": ("beneficiary", "history"),
    "beneficiary_is_new_to_account": ("beneficiary", "history"),
    "beneficiary_first_seen_days": ("beneficiary",),
    "device_is_new_to_subject": ("device", "history"),
    "account_age_days": ("account_opened_at",),
    "days_since_account_activity": ("history",),
    "failed_logins_1h_subject": ("events",),
    "device_bound_hours": ("events",),
    "credential_changed_hours": ("events",),
    "sim_changed_hours": ("events",),
    "payee_added_minutes": ("events",),
    "credits_24h_account": ("credits",),
    "distinct_remitters_24h_account": ("credits",),
    "inbound_count_ratio_24h_vs_daily_mean_30d": ("credits",),
    "minutes_since_last_credit": ("credits",),
    "pass_through_ratio_24h": ("credits",),
    "region_is_new_to_subject": ("region", "history"),
    "card_present_count_1h_account": ("channel", "instrument", "history"),
    "beneficiary_distinct_senders_24h": ("beneficiary",),
    "hour_of_day_local": (),
}

# Rules, and the measurements (or transaction fields) each cannot work without.
RULE_NEEDS: dict[str, tuple[str, ...]] = {
    "VELOCITY_BURST_1H": ("history", "beneficiary"),
    "ACCOUNT_TAKEOVER_SEQUENCE": ("events", "beneficiary"),
    "MULE_INBOUND_FANIN": ("credits",),
    "SECOND_LEG_ONWARD_PAYMENT": ("credits",),
    "CARD_TESTING_PROBES": ("auth_result", "instrument"),
    "SCAM_BENEFICIARY_FANIN": ("beneficiary", "history"),
    "SIM_SWAP_TRANSFER": ("events", "beneficiary"),
    "DORMANT_ACCOUNT_REACTIVATION": ("history", "amount"),
    "CARD_PRESENT_NEW_REGION_CASHOUT": ("region", "channel", "instrument", "history"),
    "SANCTIONED_BENEFICIARY": ("lists",),
    "KNOWN_MULE_BENEFICIARY": ("lists",),
    "PRE_REGISTERED_BENEFICIARY": ("lists",),
    "ESTABLISHED_PAYEE_NORMAL": ("beneficiary", "history"),
}

WHY_MISSING = {
    "history": "each account appears too rarely for 'unusual for this customer' to mean anything",
    "events": "no sign-in, device or SIM event feed: a transaction file does not carry one",
    "credits": "no incoming transactions, so the receiving side cannot be seen",
    "lists": "sanctions, confirmed-mule and payee lists belong to the bank, not the dataset",
}


def declared(mapping: Mapping, df) -> dict[str, bool]:
    """Which inputs the mapped data supplies, including the derived ones."""
    have = {name: mapping.has(name) for name in
            ("amount", "beneficiary", "device", "channel", "instrument", "region", "account_opened_at")}
    have["auth_result"] = mapping.has("auth_result") and "constant" not in (mapping.spec("auth_result") or {})
    per_account = df.groupby("account_token", sort=False).size()
    have["history"] = bool(len(per_account)) and float(per_account.median()) >= 3
    have["credits"] = bool((df["direction"] == "INBOUND").any())
    have["events"] = False
    have["lists"] = False
    return have


def feature_table(have: dict[str, bool], X: np.ndarray | None = None) -> list[dict]:
    rows = []
    for j, name in enumerate(FEATURE_NAMES):
        needs = REQUIRES.get(name, ())
        missing = [n for n in needs if not have.get(n, False)]
        row = {"feature": name, "needs": list(needs), "available": not missing,
               "missing": missing, "why": [WHY_MISSING.get(m, f"no `{m}` column mapped") for m in missing]}
        if X is not None:
            col = X[:, j]
            row["constant_on_this_data"] = bool(np.nanstd(col) == 0.0)
            row["share_not_applicable"] = round(float(np.mean(col == -1.0)), 4)
        rows.append(row)
    return rows


def rule_table(have: dict[str, bool], fired: dict[str, int] | None = None) -> list[dict]:
    rows = []
    for code, needs in RULE_NEEDS.items():
        missing = [n for n in needs if not have.get(n, False)]
        rows.append({"rule": code, "can_fire": not missing, "missing": missing,
                     "why": [WHY_MISSING.get(m, f"no `{m}` column mapped") for m in missing],
                     "fired": (fired or {}).get(code, 0)})
    return rows

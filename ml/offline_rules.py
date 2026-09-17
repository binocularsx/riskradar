"""The rules, offline, exactly as they run live (D82).

Plain English
-------------
Several scripts need to know which payments the rules would flag: the held-out
evaluation, the budget menu, the tier study. Each used to carry its own copy of
the rules written as numpy conditions. That is quick, and it is how a new rule
gets left out of one script and nobody notices: the numbers still come out,
they are simply for a different system.

So there is one copy. The ruleset here is the one ``scripts/seed.py`` ships,
and :func:`compute_signals` runs the real ``riskradar.rules`` engine on each
row, with the transaction's own instrument, channel and region. It is slower
than numpy by a minute or two and cannot drift.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.policy.engine import Thresholds, apply as apply_policy  # noqa: E402
from riskradar.rules.engine import RuleContext, Signal, evaluate as evaluate_rules  # noqa: E402
from riskradar.features.types import TxView  # noqa: E402

# The seeded ruleset (scripts/seed.py). A test holds the two equal.
RULE_CONFIGS: dict[str, dict[str, Any]] = {
    "VELOCITY_BURST_1H": {"enabled": True, "params": {"min_count": 5, "new_destination_days": 1, "no_destination_min_count": 10}},  # D67
    "ACCOUNT_TAKEOVER_SEQUENCE": {"enabled": True, "params": {"within_hours": 24, "min_failed_logins": 3, "min_precursors": 2}},  # D77
    "MULE_INBOUND_FANIN": {"enabled": True, "params": {"min_remitters": 3, "min_count_ratio": 5.0}},  # D78 (credits only)
    "SECOND_LEG_ONWARD_PAYMENT": {"enabled": True, "params": {"min_remitters": 3, "min_count_ratio": 5.0,
                                  "max_minutes_since_credit": 180, "min_pass_through": 0.5}},  # D79
    "CARD_TESTING_PROBES": {"enabled": True, "params": {"min_decline_rate_24h": 0.5, "min_failed_1h": 3}},
    # D82: tuned by ml/fraud_types_d82.py; see ml/artifacts/fraud-types-d82.json.
    "SCAM_BENEFICIARY_FANIN": {"enabled": True, "params": {"min_other_senders": 1, "max_beneficiary_age_days": 3, "min_amount_ratio": 1.5}},
    "SIM_SWAP_TRANSFER": {"enabled": True, "params": {"within_hours": 12, "channels": ["USSD", "MOBILE_APP"], "min_amount_log10": 4.7}},
    "DORMANT_ACCOUNT_REACTIVATION": {"enabled": True, "params": {"min_dormant_days": 60, "min_amount_log10": 5.0}},
    "CARD_PRESENT_NEW_REGION_CASHOUT": {"enabled": True, "params": {"min_count_1h": 3}},
    "SANCTIONED_BENEFICIARY": {"enabled": True, "params": {}},
    "KNOWN_MULE_BENEFICIARY": {"enabled": True, "params": {}},
    "PRE_REGISTERED_BENEFICIARY": {"enabled": True, "params": {}},
    "ESTABLISHED_PAYEE_NORMAL": {"enabled": True, "params": {"min_beneficiary_age_days": 60, "max_amount_ratio": 1.0}},
}

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _tx(meta: dict[str, np.ndarray] | None, i: int) -> TxView:
    get = (lambda k, default=None: meta[k][i] if meta is not None and meta.get(k) is not None else default)
    has_ben = get("has_beneficiary", True)
    return TxView(
        transaction_ref="offline",
        occurred_at=_EPOCH,
        amount_minor=0,
        currency="NGN",
        channel=get("channel", "WEB") or "WEB",
        instrument=get("instrument", "ACCOUNT_TRANSFER") or "ACCOUNT_TRANSFER",
        rail="NIP",
        subject_token="s",
        account_token="a",
        beneficiary_token="b" if bool(has_ben) else None,
        ip_region=get("ip_region"),
    )


def compute_signals(X: np.ndarray, meta: dict[str, np.ndarray] | None = None, names=FEATURE_NAMES,
                    configs: dict | None = None) -> list[list[Signal]]:
    """The rules' signals for every row of a feature matrix (payments)."""
    configs = configs or RULE_CONFIGS
    names = list(names)
    out = []
    for i in range(len(X)):
        features = {name: float(X[i, j]) for j, name in enumerate(names)}
        out.append(evaluate_rules(RuleContext(tx=_tx(meta, i), features=features), configs))
        if i and i % 250_000 == 0:
            print(f"      rules {i:,}/{len(X):,}", flush=True)
    return out


_NEVER = Thresholds(id=0, version=0, p_monitor=2.0, p_review=2.0, p_hold=2.0, alert_min_level="MEDIUM")


def rule_masks(signals: list[list[Signal]]) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Which rows the rules alone make actionable, and which rule fired where."""
    n = len(signals)
    actionable = np.array([apply_policy(0.0, s, _NEVER).actionable for s in signals], dtype=bool)
    by_code: dict[str, np.ndarray] = {}
    for i, sigs in enumerate(signals):
        for s in sigs:
            if s.power == "ESCALATE":
                by_code.setdefault(s.code, np.zeros(n, dtype=bool))[i] = True
    return actionable, by_code

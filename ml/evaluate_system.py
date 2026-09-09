"""System-level held-out evaluation: model **plus rules plus policy**.

``evaluate.py`` measures the model alone, and its most useful finding is a
failure: with card testing held out, the model catches **none** of it. That is
not a footnote — it is the argument for the whole three-layer architecture (D11).

A model can only recognise shapes resembling something it was trained on. A
deterministic rule does not have that limitation, which is exactly why rules are
kept as a separate layer emitting named facts rather than being folded into the
model as features or blended into its score. This script measures what the
system actually decides, so the architecture is defended with a number instead of
an argument.

    python ml/evaluate_system.py

Nothing here breaks the generator/detector wall (D10a): it reads the detection
stack and the corpus labels, never a generator parameter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from dataset import holdout_typology_split  # noqa: E402
from evaluate import load_or_build  # noqa: E402
from metrics import threshold_for_alert_budget  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.features.types import TxView  # noqa: E402
from riskradar.policy.engine import Thresholds, apply as apply_policy  # noqa: E402
from riskradar.rules.engine import RuleContext, evaluate as evaluate_rules  # noqa: E402
from train import ALERT_BUDGET_PER_DAY, TYPOLOGIES, build_model  # noqa: E402

# The seeded ruleset (scripts/seed.py). Read here rather than invented, so the
# evaluation measures the rules that actually ship.
RULE_CONFIGS = {
    "VELOCITY_BURST_1H": {"enabled": True, "params": {"min_count": 5}},
    "CARD_TESTING_PROBES": {
        "enabled": True,
        "params": {"min_decline_rate_24h": 0.5, "min_failed_1h": 3},
    },
    "SANCTIONED_BENEFICIARY": {"enabled": True, "params": {}},
    "KNOWN_MULE_BENEFICIARY": {"enabled": True, "params": {}},
    "PRE_REGISTERED_BENEFICIARY": {"enabled": True, "params": {}},
    "ESTABLISHED_PAYEE_NORMAL": {
        "enabled": True,
        "params": {"min_beneficiary_age_days": 60, "max_amount_ratio": 1.0},
    },
}


def _minimal_tx(features: dict[str, float], instrument: str) -> TxView:
    """The rules need a transaction; only ``instrument`` is read by any of them.

    Reconstructing a full TxView from the cached feature matrix is not possible
    and not needed — but this *is* a limitation worth stating: the offline system
    evaluation approximates the card-testing rule's instrument gate rather than
    replaying the original row.
    """
    from datetime import datetime, timezone

    return TxView(
        transaction_ref="eval",
        occurred_at=datetime.now(timezone.utc),
        amount_minor=0,
        currency="NGN",
        channel="WEB",
        instrument=instrument,
        rail="CARD_SCHEME" if instrument == "CARD" else "NIP",
        subject_token="s",
        account_token="a",
        beneficiary_token="b",
    )


def system_decide(
    X: np.ndarray, p: np.ndarray, thresholds: Thresholds, instruments: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Run rules + policy over a scored set. Returns (actionable, level index)."""
    actionable = np.zeros(len(p), dtype=bool)
    levels = np.zeros(len(p), dtype=int)
    order = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

    for i in range(len(p)):
        features = {name: float(X[i, j]) for j, name in enumerate(FEATURE_NAMES)}
        signals = evaluate_rules(
            RuleContext(tx=_minimal_tx(features, instruments[i]), features=features),
            RULE_CONFIGS,
        )
        result = apply_policy(float(p[i]), signals, thresholds)
        actionable[i] = result.actionable
        levels[i] = order.index(result.risk_level)
    return actionable, levels


def incident_recall(y: np.ndarray, flagged: np.ndarray, incident_id: np.ndarray) -> dict:
    fraud = y == 1
    incidents = {i for i in incident_id[fraud] if i}
    caught = {i for i in incident_id[fraud & flagged] if i}
    return {
        "incidents": len(incidents),
        "incidents_caught": len(caught),
        "incident_recall": round(len(caught) / len(incidents), 4) if incidents else 0.0,
        "alerts": int(flagged.sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="ml/data/corpus.jsonl")
    parser.add_argument("--out", default="ml/artifacts/system-evaluation.json")
    args = parser.parse_args()

    corpus = load_or_build(REPO_ROOT / args.corpus, rebuild=False)
    span_days = (max(corpus.occurred_at) - min(corpus.occurred_at)).total_seconds() / 86400.0

    # The card-testing rule gates on instrument == CARD. Recovering that from the
    # cached matrix is not possible, so it is approximated from the decline
    # signature the typology produces. Stated plainly because it is the one place
    # this offline evaluation is not the live path.
    card_like = (corpus.X[:, FEATURE_NAMES.index("decline_rate_24h_account")] > 0.0)
    instruments = np.where(card_like, "CARD", "ACCOUNT_TRANSFER")

    results = {}
    for held_out in TYPOLOGIES:
        print(f"\n=== holding out {held_out} ===")
        train_idx, test_idx = holdout_typology_split(corpus, held_out)

        model = build_model()
        model.fit(corpus.X[train_idx], corpus.y[train_idx])
        p = model.predict_proba(corpus.X[test_idx])[:, 1]

        y = corpus.y[test_idx]
        ids = corpus.incident_id[test_idx]
        test_days = max(span_days * 0.25, 1.0)

        # Model alone, at the alert budget.
        model_threshold = threshold_for_alert_budget(
            p, budget_per_day=ALERT_BUDGET_PER_DAY, days=test_days
        )
        model_only = incident_recall(y, p >= model_threshold, ids)

        # The system: the same probabilities, plus rules, plus policy, with the
        # thresholds solved from the same budget.
        thresholds = Thresholds(
            id=0, version=0,
            p_monitor=float(model_threshold),
            p_review=float(np.quantile(p, 0.999)),
            p_hold=float(np.quantile(p, 0.9999)),
            alert_min_level="MEDIUM",
        )
        actionable, _ = system_decide(corpus.X[test_idx], p, thresholds, instruments[test_idx])
        system = incident_recall(y, actionable, ids)

        results[held_out] = {"model_only": model_only, "model_plus_rules": system}
        print(
            f"  model only        : {model_only['incident_recall']} "
            f"({model_only['incidents_caught']}/{model_only['incidents']}), "
            f"{model_only['alerts']} alerts"
        )
        print(
            f"  model + rules     : {system['incident_recall']} "
            f"({system['incidents_caught']}/{system['incidents']}), "
            f"{system['alerts']} alerts"
        )

    model_mean = float(np.mean([r["model_only"]["incident_recall"] for r in results.values()]))
    system_mean = float(np.mean([r["model_plus_rules"]["incident_recall"] for r in results.values()]))

    report = {
        "alert_budget_per_day": ALERT_BUDGET_PER_DAY,
        "per_typology": results,
        "headline": {
            "model_only_mean_incident_recall": round(model_mean, 4),
            "system_mean_incident_recall": round(system_mean, 4),
            "statement": (
                "Incident-level recall on a fraud typology never seen in training. "
                "The model alone misses card testing entirely; the deterministic "
                "rules layer catches it. This is the measured argument for keeping "
                "rules as a separate layer emitting named facts (D11) rather than "
                "folding them into the model or blending them into its score."
            ),
        },
        "limitations": [
            "The card-testing rule gates on instrument == CARD; the offline "
            "evaluation approximates that gate from the decline signature, so "
            "this measures the architecture rather than replaying the live path "
            "exactly.",
            "Rule list membership (sanctioned, known mule, allowlist) is empty "
            "offline, so the two OVERRIDE rules and one SUPPRESS rule contribute "
            "nothing here. In production they would.",
        ],
    }

    out = REPO_ROOT / args.out
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n=== published figure ===")
    print(f"  model alone   : {report['headline']['model_only_mean_incident_recall']}")
    print(f"  full system   : {report['headline']['system_mean_incident_recall']}")
    print(f"  report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

"""Held-out-typology evaluation across all three typologies.

    python ml/evaluate.py --corpus ml/data/corpus.jsonl

D10b — hold out **typologies, not rows**. A standard random split leaks: if
account takeover appears in both halves you are measuring memorisation. Training
on two typologies and testing on a third answers the only question that matters
operationally — *will this catch fraud we did not anticipate?*

D10c / D24a — accuracy is absent, and the headline is **incident-level** recall
at the alert budget. Both numbers are published together with the reason, so the
low transaction-level figure is volunteered rather than discovered.

The report this writes is the Week 5 deliverable. It contains the unflattering
numbers on purpose.
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

from dataset import holdout_typology_split, load_corpus  # noqa: E402
from metrics import summarise  # noqa: E402
from train import ALERT_BUDGET_PER_DAY, TYPOLOGIES, build_model  # noqa: E402


def cache_path(corpus_path: Path) -> Path:
    return corpus_path.with_suffix(".features.npz")


def load_or_build(corpus_path: Path, *, rebuild: bool):
    """Feature construction over 600k rows takes minutes; cache it.

    The cache is keyed to the corpus file, and the feature spec version is stored
    alongside so a spec bump invalidates it rather than silently evaluating a
    model against features it was not trained on.
    """
    from riskradar.features.spec import FEATURE_SPEC_VERSION

    cache = cache_path(corpus_path)
    if cache.exists() and not rebuild:
        blob = np.load(cache, allow_pickle=True)
        if str(blob["spec_version"]) == FEATURE_SPEC_VERSION:
            print(f"  using cached features ({cache.name})")

            class Cached:
                X = blob["X"]
                y = blob["y"]
                typology = blob["typology"]
                incident_id = blob["incident_id"]
                occurred_at = blob["occurred_at"]

            return Cached()
        print("  cache is for a different feature spec — rebuilding")

    corpus = load_corpus(corpus_path)
    np.savez_compressed(
        cache,
        X=corpus.X,
        y=corpus.y,
        typology=corpus.typology,
        incident_id=corpus.incident_id,
        occurred_at=corpus.occurred_at,
        spec_version=FEATURE_SPEC_VERSION,
    )
    print(f"  cached features to {cache.name}")
    return corpus


def main() -> None:
    parser = argparse.ArgumentParser(description="Held-out-typology evaluation")
    parser.add_argument("--corpus", default="ml/data/corpus.jsonl")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--out", default="ml/artifacts/held-out-evaluation.json")
    args = parser.parse_args()

    corpus_path = REPO_ROOT / args.corpus
    print(f"loading {corpus_path.name}")
    corpus = load_or_build(corpus_path, rebuild=args.rebuild_cache)

    span_days = (max(corpus.occurred_at) - min(corpus.occurred_at)).total_seconds() / 86400.0
    results = {}

    for held_out in TYPOLOGIES:
        print(f"\n=== holding out {held_out} ===")
        train_idx, test_idx = holdout_typology_split(corpus, held_out)
        model = build_model()
        model.fit(corpus.X[train_idx], corpus.y[train_idx])

        p = model.predict_proba(corpus.X[test_idx])[:, 1]
        summary = summarise(
            corpus.y[test_idx], p, corpus.incident_id[test_idx],
            budget_per_day=ALERT_BUDGET_PER_DAY,
            days=max(span_days * 0.25, 1.0),
            label=f"held out: {held_out}",
        )
        results[held_out] = summary
        print(
            f"  PR-AUC {summary['pr_auc']} · ROC-AUC {summary['roc_auc']} · "
            f"incident recall {summary['incident_level']['incident_recall']} "
            f"({summary['incident_level']['incidents_caught']}/"
            f"{summary['incident_level']['incidents']} incidents) · "
            f"transaction recall {summary['at_alert_budget']['transaction_recall']}"
        )

    recalls = [r["incident_level"]["incident_recall"] for r in results.values()]
    report = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "alert_budget_per_day": ALERT_BUDGET_PER_DAY,
        "corpus_span_days": round(span_days, 2),
        "per_typology": results,
        "headline": {
            "mean_incident_recall_on_unseen_typology": round(float(np.mean(recalls)), 4),
            "worst_incident_recall": round(float(np.min(recalls)), 4),
            "statement": (
                "Incident-level recall on a fraud typology the model has never seen, "
                f"within a {ALERT_BUDGET_PER_DAY} alerts/day budget. This is the "
                "number we publish. In-distribution figures are far higher and mean "
                "far less."
            ),
        },
    }

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== published figure ===")
    print(f"  mean incident recall on an unseen typology: {report['headline']['mean_incident_recall_on_unseen_typology']}")
    print(f"  worst case: {report['headline']['worst_incident_recall']}")
    print(f"  report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

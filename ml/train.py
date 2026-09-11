"""Train, calibrate, evaluate and register the fraud model.

Plain English
-------------
Trains the model, then grades it honestly.

The training part is unremarkable: feed a few hundred thousand transactions and
their known outcomes to a standard algorithm. The grading is where the care is.

The model is deliberately trained on only two of the three kinds of fraud, and
then tested on the third — one it has never seen. That number is much worse
than the usual one and it is the number this project publishes, because the
question that matters operationally is not "can it recognise fraud it has seen
before" but "will it catch something new".

    python ml/train.py --corpus ml/data/corpus.jsonl --holdout ACCOUNT_TAKEOVER --promote

Three commitments are enforced here rather than described:

* **Calibrated output** (D11). The model emits a probability that means what it
  says, because alert volume at a threshold is predictable only from a
  calibrated probability — and alert volume is what decides whether analysts
  drown. The reliability curve is reported, not assumed.
* **Held-out typology** (D10b). Trained on two typologies, evaluated on a third
  it has never seen. The published number is that one, not the flattering
  in-distribution figure.
* **Registered, not promoted** (D15b). Training writes a ``model_versions`` row.
  Promotion is a separate, audited act.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

import joblib  # noqa: E402
import psycopg  # noqa: E402

from dataset import Corpus, holdout_typology_split, load_corpus  # noqa: E402
from metrics import summarise  # noqa: E402
from riskradar.config import settings  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION  # noqa: E402

TYPOLOGIES = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")
ALERT_BUDGET_PER_DAY = 120  # D24


def build_model() -> TimeSplitCalibratedBooster:
    """Gradient boosting on older rows, isotonic calibration on newer ones.

    Replaced the previous ``CalibratedClassifierCV(balanced booster, isotonic,
    cv=3)`` after ``ml/calibration.py`` measured it on a fair time-ordered test:
    it stated 16% fewer frauds among its alerts than there were. This version
    states 8% fewer and halves the calibration error where alerts are raised,
    with detection unchanged. See ``riskradar/model/calibrated.py`` for why.

    Callers must pass ``times=`` to ``fit`` whenever rows are shuffled.
    """
    return TimeSplitCalibratedBooster(random_state=20260909)


def register(
    *,
    name: str,
    version: str,
    artifact_path: Path,
    metrics: dict,
    promote: bool,
) -> int:
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    relative = artifact_path.relative_to(REPO_ROOT).as_posix()

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_versions
                    (name, version, artifact_hash, artifact_path, feature_spec_version,
                     calibration, metrics, trained_at, is_active)
                VALUES (%s, %s, %s, %s, %s, 'isotonic', %s, now(), false)
                ON CONFLICT (name, version) DO UPDATE
                   SET artifact_hash = EXCLUDED.artifact_hash,
                       artifact_path = EXCLUDED.artifact_path,
                       metrics       = EXCLUDED.metrics,
                       trained_at    = now()
                RETURNING id
                """,
                (name, version, digest, relative, FEATURE_SPEC_VERSION, json.dumps(metrics)),
            )
            model_id = cur.fetchone()["id"]

            if promote:
                # Convenience for a fresh build only. In normal operation
                # promotion goes through POST /v1/admin/models/promote, which is
                # audited with the comparison attached (D15b).
                cur.execute("UPDATE model_versions SET is_active = false WHERE is_active")
                cur.execute(
                    "UPDATE model_versions SET is_active = true, promoted_at = now() WHERE id = %s",
                    (model_id,),
                )
                sys_uid = cur.execute(
                    "SELECT id FROM users WHERE is_system LIMIT 1"
                ).fetchone()["id"]
                from riskradar.audit import chain

                chain.append(
                    conn,
                    actor_user_id=sys_uid,
                    action="MODEL_PROMOTED",
                    object_type="model_version",
                    object_id=model_id,
                    to_state=f"{name}:{version}",
                    payload={"via": "ml/train.py --promote", "metrics": metrics},
                )
    return model_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the Risk Radar model")
    parser.add_argument("--corpus", default="ml/data/corpus.jsonl")
    parser.add_argument("--holdout", default="ACCOUNT_TAKEOVER", choices=TYPOLOGIES)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--name", default="riskradar-gbm")
    parser.add_argument("--version", default=None)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    print(f"loading corpus {args.corpus}")
    corpus: Corpus = load_corpus(REPO_ROOT / args.corpus, limit=args.limit)

    days = (max(corpus.occurred_at) - min(corpus.occurred_at)).total_seconds() / 86400.0
    print(f"  span {days:.1f} days, base rate {100 * corpus.y.mean():.3f}%")

    train_idx, test_idx = holdout_typology_split(corpus, args.holdout)
    print(
        f"  train {len(train_idx)} rows ({int(corpus.y[train_idx].sum())} fraud, "
        f"typologies {sorted(set(t for t in corpus.typology[train_idx] if t))})"
    )
    print(
        f"  test  {len(test_idx)} rows ({int(corpus.y[test_idx].sum())} fraud, "
        f"HELD-OUT typology {args.holdout})"
    )

    print("training ...")
    started = time.perf_counter()
    model = build_model()
    model.fit(corpus.X[train_idx], corpus.y[train_idx], times=corpus.occurred_at[train_idx])
    print(f"  fit in {time.perf_counter() - started:.1f}s")

    p_test = model.predict_proba(corpus.X[test_idx])[:, 1]
    p_train = model.predict_proba(corpus.X[train_idx])[:, 1]

    test_days = max(days * 0.25, 1.0)
    held_out = summarise(
        corpus.y[test_idx], p_test, corpus.incident_id[test_idx],
        budget_per_day=ALERT_BUDGET_PER_DAY, days=test_days,
        label=f"held-out typology: {args.holdout}",
    )
    in_dist = summarise(
        corpus.y[train_idx], p_train, corpus.incident_id[train_idx],
        budget_per_day=ALERT_BUDGET_PER_DAY, days=max(days * 0.75, 1.0),
        label="in-distribution (training typologies)",
    )

    print("\n=== HELD-OUT TYPOLOGY (the number we publish) ===")
    print(json.dumps({k: v for k, v in held_out.items() if k != "reliability"}, indent=2))
    print("\n=== in-distribution (for comparison only) ===")
    print(f"  PR-AUC {in_dist['pr_auc']}  incident recall "
          f"{in_dist['incident_level']['incident_recall']}")

    version = args.version or datetime.now(timezone.utc).strftime("%Y%m%d.%H%M")
    artifact = REPO_ROOT / "ml" / "artifacts" / f"{args.name}-{version}.joblib"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact)

    metrics = {
        "held_out_typology": args.holdout,
        "held_out": held_out,
        "in_distribution": {
            "pr_auc": in_dist["pr_auc"],
            "incident_recall": in_dist["incident_level"]["incident_recall"],
        },
        "corpus": {
            "rows": int(len(corpus.y)),
            "fraud": int(corpus.y.sum()),
            "span_days": round(days, 2),
        },
        "feature_names": list(FEATURE_NAMES),
        # The ablation reference for local attributions (G3). Medians of the
        # training set: "what this feature usually is".
        "feature_baseline": [float(v) for v in np.median(corpus.X[train_idx], axis=0)],
        "alert_budget_per_day": ALERT_BUDGET_PER_DAY,
    }

    model_id = register(
        name=args.name, version=version, artifact_path=artifact,
        metrics=metrics, promote=args.promote,
    )
    print(f"\nregistered model_versions.id={model_id} ({artifact.name})"
          f"{' and promoted' if args.promote else ' — promote via the admin API'}")

    report = REPO_ROOT / "ml" / "artifacts" / f"evaluation-{version}.json"
    report.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"evaluation report: {report.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

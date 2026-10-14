"""Train Risk Radar's model on all of IEEE-CIS train, for scoring IEEE-CIS test (D81d).

    python ml/datasets/ieee_model.py

The labelled measurement is the time-ordered split inside the train file
(``evaluate_dataset.py run ieee_train_joined.csv``). This is the other half of
"train on train, test on test": the same recipe (``riskradar.features``, the
model's 21 inputs, time-split isotonic calibration) fitted on every labelled
row, saved where the evaluator and the live registry can load it, then used on
``ieee_test_joined.csv``. Kaggle never released test labels, so what it
produces on test is a volume and a ranking, not an accuracy.

The artefact is named ``ieee-gbm-*`` so the demo builder, which registers the
newest ``riskradar-gbm-*``, never picks it up by accident.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from byod import canonical, evaluate  # noqa: E402
from byod.mapping import Mapping  # noqa: E402
from riskradar.features.spec import FEATURE_SPEC_VERSION, MODEL_FEATURE_NAMES  # noqa: E402

DATA = REPO_ROOT / "ml" / "data" / "public" / "ieee_train_joined.csv"
MAPPING = REPO_ROOT / "ml" / "datasets" / "ieee_cis.mapping.yaml"
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"


def main() -> None:
    mapping = Mapping.load(MAPPING)
    df, notes, _ = canonical.load(DATA, mapping, max_rows=None)
    key = evaluate.cache_key(DATA, MAPPING.read_text(encoding="utf-8"), 1_000_000)
    cache = ARTIFACTS / "datasets" / f"ieee_train.features-{key}.npz"
    X = evaluate.features(df, cache)
    Xm = X[:, evaluate.MODEL_COLUMNS]
    y = df["is_fraud"].to_numpy()
    print(f"training on all {len(y):,} labelled rows ({y.mean():.4f} fraud) ...", flush=True)
    model = evaluate.fit_gbm(Xm, y, df["occurred_at"].to_numpy())

    version = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M") + "-ieee"
    artifact = ARTIFACTS / f"ieee-gbm-{version}.joblib"
    joblib.dump(model, artifact)
    report = {
        "trained_on": "IEEE-CIS train, every labelled row (Vesta, card-not-present e-commerce)",
        "evidence": "ml/artifacts/datasets/ieee_train.evaluation.json (time-ordered split of the same file)",
        "feature_spec": FEATURE_SPEC_VERSION,
        "feature_names": list(MODEL_FEATURE_NAMES),
        "feature_baseline": [float(v) for v in np.median(Xm, axis=0)],
        "rows": int(len(y)), "fraud": int(y.sum()),
        "note": "Not a production model for a Nigerian bank: it learned one US e-commerce merchant's customers.",
    }
    (ARTIFACTS / f"evaluation-ieee-gbm-{version}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved {artifact.name}")


if __name__ == "__main__":
    main()

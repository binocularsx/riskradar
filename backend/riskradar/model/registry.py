"""Model loading, promotion and local explanation.

Plain English
-------------
Looks after the trained model file: loading it, checking it is the right one,
and explaining what it did.

The checking matters. Every model records which version of the feature list it
was trained against, and this file refuses to load one that disagrees with the
running code. A model quietly fed slightly different numbers than it learned
from still produces confident answers — just wrong ones.

The explaining is the ``attributions`` function. To work out how much a
feature mattered for one particular transaction, it re-scores that transaction
with that feature swapped for its typical value and measures how much the
answer moved. Do that for all twelve features and you get the bars an analyst
sees on the case screen.

D15a — every artefact is versioned with a content hash, its metrics, the feature
spec version it was trained against and a training-data snapshot id. The
``model_version_id`` on every decision points here, which is what makes a
decision reproducible months later (G4).

D15b — promotion is repointing ``is_active``, audited, with the comparison
attached. Rollback is repointing it back, not a redeploy.

FR-017 — if no model can be loaded the system enters rule-only mode **and raises
an alarm**. Never a silent degradation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION


class Scorer(Protocol):
    def predict_proba_one(self, vector: list[float]) -> float: ...

    def predict_proba_many(self, vectors: list[list[float]]) -> list[float]: ...


@dataclass
class ModelBundle:
    """A loaded, active model plus the provenance a decision must record."""

    id: int
    name: str
    version: str
    feature_spec_version: str
    scorer: Scorer
    calibration: str

    def predict(self, vector: list[float]) -> float:
        p = float(self.scorer.predict_proba_one(vector))
        # A calibrated probability that leaves [0,1] is a bug, not a strong opinion.
        return max(0.0, min(1.0, p))

    def attributions(self, vector: list[float], baseline: list[float]) -> dict[str, float]:
        """Per-feature local attribution by ablation (G3).

        For each feature we re-score with that value replaced by the training
        baseline (median) and report the change in predicted probability. The
        result answers "how much did *this* feature move *this* decision".

        Deliberately not SHAP: ablation needs no additional dependency and is
        explainable to an analyst in one sentence. SHAP's additivity guarantees
        would be nice to have and are not worth a heavyweight dependency plus an
        explanation nobody in the room can audit.

        **All thirteen probes go through the model in a single call.** Scoring
        them one at a time measured 180 ms per transaction — sklearn's per-call
        overhead, thirty-nine tree-ensemble evaluations deep — which alone would
        have put NFR-001 out of reach. Batched, the same work is one matrix.
        """
        probes = [list(vector)]
        for i in range(len(FEATURE_NAMES)):
            probe = list(vector)
            probe[i] = baseline[i]
            probes.append(probe)

        predictions = self.scorer.predict_proba_many(probes)
        base_p = max(0.0, min(1.0, float(predictions[0])))
        return {
            name: round(base_p - max(0.0, min(1.0, float(predictions[i + 1]))), 6)
            for i, name in enumerate(FEATURE_NAMES)
        }


class ConstantScorer:
    """Week 1's stub: returns a constant.

    The point of the vertical slice was that every later task becomes *replacing
    a stub* rather than *integrating a subsystem*. This is that stub, kept
    because it is also the honest fallback for a fresh database with no trained
    model in it yet.
    """

    def __init__(self, value: float = 0.02) -> None:
        self.value = value

    def predict_proba_one(self, vector: list[float]) -> float:
        return self.value

    def predict_proba_many(self, vectors: list[list[float]]) -> list[float]:
        return [self.value] * len(vectors)


class SklearnScorer:
    """A calibrated scikit-learn pipeline loaded from disk."""

    def __init__(self, artifact_path: Path) -> None:
        import joblib  # imported lazily so the API can boot without scikit-learn

        self._model = joblib.load(artifact_path)

    def predict_proba_one(self, vector: list[float]) -> float:
        return self.predict_proba_many([vector])[0]

    def predict_proba_many(self, vectors: list[list[float]]) -> list[float]:
        import numpy as np

        return [
            float(p) for p in self._model.predict_proba(np.asarray(vectors, dtype=float))[:, 1]
        ]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_cache: dict[int, ModelBundle] = {}
_baseline_cache: dict[int, list[float]] = {}
_lock = threading.Lock()


class ModelUnavailable(RuntimeError):
    """Raised when no usable model is active. The caller must alarm, not shrug."""


def active_model_row(conn: Any) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, version, artifact_path, artifact_hash,
                   feature_spec_version, calibration, metrics
              FROM model_versions
             WHERE is_active
             LIMIT 1
            """
        )
        row = cur.fetchone()
    return dict(row) if row else None


def load_active(conn: Any) -> ModelBundle:
    """Load (and cache) the active model.

    Raises :class:`ModelUnavailable` rather than returning a default. A silent
    default here is how a system ends up scoring live traffic with a constant and
    nobody noticing for a week.
    """
    row = active_model_row(conn)
    if row is None:
        raise ModelUnavailable("no active model version")

    with _lock:
        cached = _cache.get(row["id"])
        if cached is not None:
            return cached

        if row["feature_spec_version"] != FEATURE_SPEC_VERSION:
            # Refusing is correct. A model trained on a different feature spec
            # receives vectors that are subtly not what it learned, and the
            # offline metrics keep looking fine while serving quietly rots (D15).
            raise ModelUnavailable(
                f"model {row['name']}:{row['version']} expects feature spec "
                f"{row['feature_spec_version']}, runtime is {FEATURE_SPEC_VERSION}"
            )

        scorer: Scorer
        if not row["artifact_path"]:
            scorer = ConstantScorer()
        else:
            path = Path(row["artifact_path"])
            if not path.is_absolute():
                from ..config import REPO_ROOT

                path = REPO_ROOT / path
            if not path.exists():
                raise ModelUnavailable(f"artifact missing: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if row["artifact_hash"] and digest != row["artifact_hash"]:
                raise ModelUnavailable(
                    "artifact hash mismatch — the file on disk is not the file "
                    "that was evaluated and promoted"
                )
            scorer = SklearnScorer(path)

        bundle = ModelBundle(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            feature_spec_version=row["feature_spec_version"],
            scorer=scorer,
            calibration=row["calibration"] or "none",
        )
        _cache[row["id"]] = bundle

        metrics = row["metrics"] or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        _baseline_cache[row["id"]] = [
            float(v) for v in (metrics.get("feature_baseline") or [0.0] * len(FEATURE_NAMES))
        ]
        return bundle


def baseline_for(bundle: ModelBundle) -> list[float]:
    """The training-set median vector, used as the ablation reference."""
    return _baseline_cache.get(bundle.id, [0.0] * len(FEATURE_NAMES))


def invalidate_cache() -> None:
    """Called after promotion so the next score uses the newly active model."""
    with _lock:
        _cache.clear()
        _baseline_cache.clear()

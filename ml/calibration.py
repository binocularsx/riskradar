"""Measure calibration properly, then compare ways of fixing it.

    python ml/calibration.py

Plain English
-------------
"Calibrated" means the probability tells the truth: of all the payments the
model scores at 0.30, about three in ten really are fraud. The whole alert
budget rests on this — it is how we predict tomorrow's workload from a
threshold. ``diagnose.py`` reported that in the range where alerts are raised
the model was under-confident by about half.

That measurement had a flaw. It scored every row, including the rows the model
trained on, and its test set contained a kind of fraud the model had never
seen. Calibration is an *in-distribution* property: it asks whether the model's
numbers are honest about the traffic it was built for. Testing it against a
brand-new crime mixes two questions together. So this file measures it
properly first:

* train on the first 75% of the corpus **by time**, test on the last 25%;
* every fraud type present on both sides;
* judge only the **alerting region** — everything else is 99.8% of rows and
  near zero, which is why the headline calibration error looked perfect.

Then it compares four versions of the model on that one fair test.

The number that matters operationally
-------------------------------------
For the alerts raised at the budget threshold, add up the model's
probabilities. That sum is the number of real frauds the model *expects*
among its alerts. Compare it to the number there actually were. A ratio of
1.0 means the model's own confidence predicts the analysts' hit rate; 0.5
means it claims half as many frauds as the team will actually find.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from sklearn.calibration import CalibratedClassifierCV  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

from metrics import threshold_for_alert_budget  # noqa: E402
from train import ALERT_BUDGET_PER_DAY  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
SEED = 20260909


def base_booster(balanced: bool) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=40,
        l2_regularization=1.0, class_weight="balanced" if balanced else None,
        random_state=SEED,
    )


class Prefit:
    """Booster fitted on one slice, calibrator fitted on a later, separate slice.

    ``CalibratedClassifierCV`` fits its calibrator on shuffled folds of the
    training data. For payments that is subtly wrong twice over: it lets the
    calibrator see the same period the booster learned from, and it averages
    three boosters, which pulls probabilities toward the middle. Here the
    booster learns from the oldest data and the calibrator learns from newer
    data it never saw — the same order a real deployment runs in.
    """

    def __init__(self, balanced: bool, method: str):
        self.booster = base_booster(balanced)
        self.method = method
        self.cal = None

    def fit(self, X_fit, y_fit, X_cal, y_cal):
        self.booster.fit(X_fit, y_fit)
        raw = self.booster.predict_proba(X_cal)[:, 1]
        if self.method == "isotonic":
            self.cal = IsotonicRegression(out_of_bounds="clip").fit(raw, y_cal)
        else:
            # Platt: logistic regression on the log-odds of the raw score.
            z = np.log(np.clip(raw, 1e-7, 1 - 1e-7) / np.clip(1 - raw, 1e-7, 1))
            self.cal = LogisticRegression().fit(z.reshape(-1, 1), y_cal)
        return self

    def predict_proba(self, X):
        raw = self.booster.predict_proba(X)[:, 1]
        if self.method == "isotonic":
            p = self.cal.predict(raw)
        else:
            z = np.log(np.clip(raw, 1e-7, 1 - 1e-7) / np.clip(1 - raw, 1e-7, 1))
            p = self.cal.predict_proba(z.reshape(-1, 1))[:, 1]
        return np.column_stack([1 - p, p])


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * np.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def judge(y: np.ndarray, p: np.ndarray, days: float) -> dict:
    t = threshold_for_alert_budget(p, budget_per_day=ALERT_BUDGET_PER_DAY, days=days)
    alerting = p >= t
    n_alerts = int(alerting.sum())
    expected = float(p[alerting].sum())
    actual = int(y[alerting].sum())

    # Reliability over the alerting region only, in bins that each hold a
    # decent number of alerts rather than fixed-width bins that leave most empty.
    bins = []
    if n_alerts:
        order = np.argsort(p[alerting])
        pa, ya = p[alerting][order], y[alerting][order]
        for chunk_p, chunk_y in zip(np.array_split(pa, 5), np.array_split(ya, 5)):
            if len(chunk_p) == 0:
                continue
            k, n = int(chunk_y.sum()), len(chunk_y)
            lo, hi = wilson(k, n)
            bins.append({
                "n": n, "stated": round(float(chunk_p.mean()), 4),
                "observed": round(k / n, 4), "observed_ci95": [round(lo, 3), round(hi, 3)],
            })

    ece_alert = (
        sum(b["n"] * abs(b["stated"] - b["observed"]) for b in bins) / max(n_alerts, 1)
    )
    return {
        "threshold": round(float(t), 5),
        "alerts": n_alerts,
        "expected_frauds_in_alerts": round(expected, 1),
        "actual_frauds_in_alerts": actual,
        "confidence_ratio": round(expected / actual, 3) if actual else None,
        "ece_alerting_region": round(float(ece_alert), 4),
        "reliability_alerting_region": bins,
    }


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    X, y = d["X"], d["y"].astype(int)
    occurred = d["occurred_at"]

    # Time order. Everything below respects it.
    order = np.argsort(occurred)
    X, y, occurred = X[order], y[order], occurred[order]
    n = len(y)
    a, b = int(0.60 * n), int(0.75 * n)
    test_days = (occurred[-1] - occurred[b]).total_seconds() / 86400.0

    X_fit, y_fit = X[:a], y[:a]            # oldest 60%: the booster learns here
    X_cal, y_cal = X[a:b], y[a:b]          # next 15%:   the calibrator learns here
    X_all, y_all = X[:b], y[:b]            # first 75%:  for the CV-style variants
    X_test, y_test = X[b:], y[b:]          # newest 25%: nobody learns here

    print(f"{n:,} rows · test = newest 25% = {len(y_test):,} rows, "
          f"{int(y_test.sum()):,} fraud, {test_days:.1f} days\n")

    variants = {
        "A  current: balanced + isotonic, 3-fold CV": lambda: (
            CalibratedClassifierCV(base_booster(True), method="isotonic", cv=3),
            "all"),
        "B  unweighted + isotonic, 3-fold CV": lambda: (
            CalibratedClassifierCV(base_booster(False), method="isotonic", cv=3),
            "all"),
        "C  balanced + isotonic on a later slice": lambda: (
            Prefit(True, "isotonic"), "prefit"),
        "D  unweighted + isotonic on a later slice": lambda: (
            Prefit(False, "isotonic"), "prefit"),
        "E  balanced + Platt on a later slice": lambda: (
            Prefit(True, "sigmoid"), "prefit"),
        # The class that actually ships, fitted exactly as train.py fits it:
        # on the first 75% with timestamps, splitting 80/20 internally.
        "F  SHIPPED: TimeSplitCalibratedBooster": lambda: (
            TimeSplitCalibratedBooster(random_state=SEED), "shipped"),
    }

    results = {}
    for label, make in variants.items():
        model, mode = make()
        t0 = time.perf_counter()
        if mode == "shipped":
            model.fit(X_all, y_all, times=occurred[:b])
        elif mode == "all":
            model.fit(X_all, y_all)
        else:
            model.fit(X_fit, y_fit, X_cal, y_cal)
        p = model.predict_proba(X_test)[:, 1]
        r = judge(y_test, p, test_days)
        r["fit_seconds"] = round(time.perf_counter() - t0, 1)
        results[label] = r
        print(f"{label}")
        print(f"   alerts {r['alerts']:,} · expected frauds {r['expected_frauds_in_alerts']:.0f} "
              f"· actual {r['actual_frauds_in_alerts']} · ratio {r['confidence_ratio']} "
              f"· error in alert range {r['ece_alerting_region']:.4f}")
        for bin_ in r["reliability_alerting_region"]:
            print(f"      says {bin_['stated']:.3f}  observed {bin_['observed']:.3f} "
                  f"[{bin_['observed_ci95'][0]:.2f}, {bin_['observed_ci95'][1]:.2f}]  "
                  f"n={bin_['n']:,}")
        print()

    best = min(
        (k for k in results if results[k]["confidence_ratio"]),
        key=lambda k: results[k]["ece_alerting_region"],
    )
    print(f"best in the alerting region: {best}")

    out = REPO_ROOT / "ml" / "artifacts" / "calibration.json"
    out.write_text(json.dumps({"best": best, "variants": results}, indent=2), encoding="utf-8")
    print(f"written to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

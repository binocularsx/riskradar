"""The fraud model: a booster that learns from older data, calibrated on newer.

Plain English
-------------
The model's job is to say how likely a payment is to be fraud, and to say it
honestly — "0.30" should mean about three in ten. The alert budget depends on
that honesty: it is how tomorrow's workload is predicted from a threshold.

The previous version wrapped the booster in scikit-learn's standard
calibration, which fits the calibrator on shuffled slices of the training data
and averages three separately-trained boosters. Measured properly — trained on
the oldest 75% of the corpus by time, tested on the newest 25% — that version
claimed 16% fewer frauds among its alerts than there actually were.

This version does what a real deployment does:

1. The booster learns from the **older** part of the data.
2. The calibrator learns from a **newer** slice the booster never saw, so it
   corrects the booster's actual mistakes on unfamiliar data rather than its
   mistakes on data it has memorised.
3. No class weighting. Weighting helps a model *rank* rare fraud, but it
   distorts the probabilities the calibrator then has to undo; with a separate
   calibration slice, the unweighted booster ranked just as well and calibrated
   better.

Measured on the same fair test (``ml/calibration.py``): stated-versus-actual
frauds among alerts went from 0.84 to 0.92, and calibration error in the range
where alerts are raised halved, from 0.062 to 0.030. Frauds caught among alerts
were unchanged (1,374 vs 1,360, inside the noise).

This lives in the backend package rather than in ``ml/`` because the saved
model file stores a reference to this class, and the live scoring worker must
be able to import it to load the model.
"""

from __future__ import annotations

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

# Share of the (time-ordered) training data held back to fit the calibrator.
# Large enough to contain several hundred frauds at a 0.3% base rate.
CALIBRATION_SHARE = 0.20


class TimeSplitCalibratedBooster:
    """Gradient boosting fitted on older rows, isotonic calibration on newer.

    Exposes ``fit`` and ``predict_proba`` like any scikit-learn classifier, so
    every existing caller keeps working. ``fit`` needs to know the order in
    which the rows happened; see its docstring.
    """

    def __init__(self, random_state: int = 20260909, calibration_share: float = CALIBRATION_SHARE):
        self.random_state = random_state
        self.calibration_share = calibration_share
        self.booster_ = None
        self.calibrator_ = None
        self.classes_ = np.array([0, 1])

    def _new_booster(self) -> HistGradientBoostingClassifier:
        return HistGradientBoostingClassifier(
            max_iter=250,
            learning_rate=0.08,
            max_leaf_nodes=31,
            min_samples_leaf=40,
            l2_regularization=1.0,
            random_state=self.random_state,
        )

    def fit(self, X, y, times=None) -> "TimeSplitCalibratedBooster":
        """Fit the booster on older rows and the calibrator on newer ones.

        ``times`` is when each row happened. Pass it whenever the rows have been
        shuffled — the typology holdout split shuffles them, for instance.
        Without it the rows are assumed to already be in time order, and if they
        are not, the calibrator is fitted on a random slice rather than a later
        one and quietly loses the reason it exists.
        """
        X = np.asarray(X)
        y = np.asarray(y).astype(int)
        order = np.argsort(np.asarray(times), kind="stable") if times is not None else np.arange(len(y))
        cut = int(len(order) * (1 - self.calibration_share))
        fit_idx, cal_idx = order[:cut], order[cut:]

        if y[cal_idx].sum() == 0 or y[fit_idx].sum() == 0:
            raise ValueError(
                "both the fitting slice and the calibration slice need fraud in "
                "them; the corpus is too small or too short for this split"
            )

        self.booster_ = self._new_booster().fit(X[fit_idx], y[fit_idx])
        raw = self.booster_.predict_proba(X[cal_idx])[:, 1]
        self.calibrator_ = IsotonicRegression(out_of_bounds="clip").fit(raw, y[cal_idx])
        return self

    def predict_proba(self, X) -> np.ndarray:
        raw = self.booster_.predict_proba(np.asarray(X))[:, 1]
        p = np.clip(self.calibrator_.predict(raw), 0.0, 1.0)
        return np.column_stack([1.0 - p, p])

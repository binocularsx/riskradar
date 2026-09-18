"""D89: champion against challenger on the same rows, same budget, same measures."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ml"))

from retrain_from_outcomes import compare, verdict  # noqa: E402


def test_the_better_ranker_wins_and_budget_is_exact():
    rng = np.random.default_rng(3)
    y = (rng.random(5000) < 0.02).astype(int)
    good = y * 0.8 + rng.random(5000) * 0.3
    noise = rng.random(5000)
    result = compare(y, noise, good, days=10, budget_per_day=10)
    assert result["alerts_at_budget"] == 100
    assert result["challenger"]["pr_auc"] > result["champion"]["pr_auc"]
    assert verdict(result) == "CHALLENGER_BETTER"
    assert verdict(compare(y, good, noise, days=10, budget_per_day=10)) == "CHAMPION_BETTER"


def test_without_both_classes_it_cannot_tell():
    y = np.zeros(100, dtype=int)
    assert verdict(compare(y, np.random.rand(100), np.random.rand(100), days=1, budget_per_day=5)) == "CANNOT_TELL"

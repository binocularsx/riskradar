"""IEEE-CIS, measured over several time folds instead of one split (D81e).

    python ml/datasets/ieee_benchmark.py

Plain English
-------------
D81c measured IEEE-CIS once: train on the older 70% of the file, test on the
newest 30%. One split gives one number, and one number cannot say how much of
it is luck of the calendar. This walks forward through the file's 182 days:

    fold 1: learn from days 0-60,   test on days 60-90
    fold 2: learn from days 0-90,   test on days 90-120
    fold 3: learn from days 0-120,  test on days 120-150
    fold 4: learn from days 0-150,  test on days 150-182

Each fold is what a bank would see after running for that long and then
meeting the next month. Two models are fitted per fold with the same recipe:
on Risk Radar's 21 measurements, and on those plus Vesta's own C and D columns
(the ceiling, D81). Reported per fold and as mean and spread: ROC-AUC, PR-AUC
as a multiple of the fraud rate (lift), and at 100 alerts a day the share of
fraud incidents caught and the share of alerts that were fraud.

Features come from the evaluator's cache; nothing here recomputes them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from byod import canonical, evaluate  # noqa: E402
from byod.mapping import Mapping  # noqa: E402

DATA = REPO_ROOT / "ml" / "data" / "public" / "ieee_train_joined.csv"
MAPPING = REPO_ROOT / "ml" / "datasets" / "ieee_cis.mapping.yaml"
OUT = REPO_ROOT / "ml" / "artifacts" / "datasets" / "ieee_benchmark.json"
FOLDS = [(60, 90), (90, 120), (120, 150), (150, 183)]
BUDGET_PER_DAY = 100


def _cache(df: pd.DataFrame) -> Path:
    text = MAPPING.read_text(encoding="utf-8")
    base = REPO_ROOT / "ml" / "artifacts" / "datasets"
    for stem, rows in (("ieee_train", None), ("ieee_train", 1_000_000), ("ieee_train_budget100", None)):
        path = base / f"{stem}.features-{evaluate.cache_key(DATA, text, rows)}.npz"
        if path.exists() and np.load(path)["X"].shape[0] == len(df):
            return path
    raise SystemExit("no cached IEEE features for this mapping; run ml/evaluate_dataset.py run first (about 9 GB)")


def measure(y: np.ndarray, p: np.ndarray, incidents: np.ndarray, days: float) -> dict:
    from sklearn.metrics import average_precision_score, roc_auc_score

    k = max(1, int(round(BUDGET_PER_DAY * days)))
    top = np.argsort(-p, kind="stable")[:k]  # exactly the budget, ties by order (D85)
    flagged = np.zeros(len(y), dtype=bool)
    flagged[top] = True
    fraud_incidents = set(incidents[y == 1])
    caught = set(incidents[flagged & (y == 1)])
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 4),
        "pr_auc_lift": round(float(average_precision_score(y, p) / y.mean()), 2),
        "incident_recall_at_budget": round(len(caught) / max(len(fraud_incidents), 1), 4),
        "precision_at_budget": round(float(y[flagged].mean()), 4),
    }


def main() -> None:
    mapping = Mapping.load(MAPPING)
    df, _, native = canonical.load(DATA, mapping, max_rows=None)
    X = evaluate.features(df, _cache(df))[:, evaluate.MODEL_COLUMNS]
    nat = native.apply(pd.to_numeric, errors="coerce").fillna(-999).to_numpy(dtype=float)
    Xn = np.hstack([X, nat])
    y = df["is_fraud"].to_numpy().astype(int)
    t = df["occurred_at"].to_numpy()
    incidents = df["incident_id"].fillna("").astype(str).to_numpy()
    day = (pd.to_datetime(df["occurred_at"]) - pd.to_datetime(df["occurred_at"]).min()).dt.total_seconds().to_numpy() / 86400

    folds = []
    for start, end in FOLDS:
        train, test = day < start, (day >= start) & (day < end)
        days = float(day[test].max() - day[test].min())
        row = {"train_days": [0, start], "test_days": [start, end], "train_rows": int(train.sum()),
               "test_rows": int(test.sum()), "test_fraud_rows": int(y[test].sum()),
               "test_incidents": int(len(set(incidents[test & (y == 1)])))}
        for name, M in (("risk_radar_21", X), ("plus_vesta_c_d", Xn)):
            model = evaluate.fit_gbm(M[train], y[train], t[train])
            p = model.predict_proba(M[test])[:, 1]
            row[name] = measure(y[test], p, incidents[test], days)
        folds.append(row)
        print(json.dumps(row), flush=True)

    summary = {}
    for name in ("risk_radar_21", "plus_vesta_c_d"):
        summary[name] = {m: {"mean": round(float(np.mean([f[name][m] for f in folds])), 4),
                             "min": round(float(np.min([f[name][m] for f in folds])), 4),
                             "max": round(float(np.max([f[name][m] for f in folds])), 4)}
                         for m in folds[0][name]}
    out = {"dataset": mapping.dataset, "budget_per_day": BUDGET_PER_DAY, "folds": folds, "summary": summary,
           "note": "walk-forward: each fold learns from every earlier day and is tested on the next month"}
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

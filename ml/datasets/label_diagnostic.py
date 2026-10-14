"""Where did this dataset's fraud labels come from? A diagnostic, never a result (D81).

    python ml/datasets/label_diagnostic.py

The evaluator found that nothing we are allowed to use predicts the labels in
``nigerian_transactions_clean.csv`` better than a random order. Two explanations
fit: the labels were assigned without regard to behaviour, or they were built
from the score and risk columns we exclude (D10f). This separates them.

Three measurements on a time-ordered split (train older 70%, test newer 30%):

1. Each column on its own: ROC-AUC of the raw value against the label, and
   the share of fraud in the column's top 1% (versus the base rate).
2. A model on the behavioural columns only (what the evaluator used).
3. The same model with the excluded score and risk columns added.

If (3) separates fraud and (2) does not, the labels were derived from those
scores, and the dataset is circular: a detector trained on it learns the
generator's formula. If neither does, the labels are unrelated to anything in
the file. Either way this is a statement about the dataset, published as such,
and the score columns stay excluded from every evaluation arm.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

SRC = Path(r"C:\Users\CHIDERA\Downloads\processed\processed\nigerian_transactions_clean.csv")
OUT = Path(__file__).resolve().parents[1] / "artifacts" / "datasets" / "nigerian_clean.label-diagnostic.json"

BEHAVIOUR = ["amount_ngn", "time_since_last_transaction", "txn_hour", "is_weekend", "is_salary_week", "is_night_txn",
             "txn_count_last_1h", "txn_count_last_24h", "total_amount_last_1h", "time_since_last",
             "device_seen_count", "is_device_shared", "ip_seen_count", "is_ip_shared", "user_txn_count_total",
             "user_avg_txn_amt", "user_std_txn_amt", "user_txn_frequency_24h", "avg_gap_between_txns"]
SCORES = ["spending_deviation_score", "velocity_score", "geo_anomaly_score", "geospatial_velocity_anomaly",
          "merchant_fraud_rate", "channel_risk_score", "persona_fraud_risk", "location_fraud_risk",
          "is_high_risk_state", "is_high_risk_lga", "is_ato_risk", "bvn_linked", "new_device_transaction"]
CATEGORIES = ["transaction_type", "merchant_category", "location", "device_used", "payment_channel",
              "sender_persona", "state", "source"]


def to_num(s: pd.Series) -> pd.Series:
    if s.dtype == object or str(s.dtype) in ("str", "string", "bool"):
        lower = s.astype(str).str.lower()
        if set(lower.dropna().unique()) <= {"true", "false", "nan", "none"}:
            return lower.map({"true": 1.0, "false": 0.0})
    return pd.to_numeric(s, errors="coerce")


def fit_score(X_tr, y_tr, X_te, y_te) -> dict:
    model = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.08, random_state=20260917)
    model.fit(X_tr, y_tr)
    p = model.predict_proba(X_te)[:, 1]
    ap = average_precision_score(y_te, p)
    return {"roc_auc": round(float(roc_auc_score(y_te, p)), 4), "pr_auc": round(float(ap), 4),
            "pr_auc_lift_over_random": round(float(ap / y_te.mean()), 2)}


def main() -> None:
    cols = ["timestamp", "is_fraud", "fraud_type"] + BEHAVIOUR + SCORES + CATEGORIES
    parts = []
    for i, chunk in enumerate(pd.read_csv(SRC, usecols=cols, chunksize=500_000, low_memory=False)):
        parts.append(chunk.sample(frac=0.07, random_state=i))  # ~350k rows spread across the whole year
    df = pd.concat(parts, ignore_index=True)
    df["t"] = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce")
    df = df.dropna(subset=["t"]).sort_values("t").reset_index(drop=True)
    y = df["is_fraud"].astype(str).str.lower().eq("true").astype(int).to_numpy()
    print(f"{len(df):,} sampled rows, fraud share {y.mean():.4f}")

    numeric = {c: to_num(df[c]) for c in BEHAVIOUR + SCORES}
    per_column = []
    for c, v in numeric.items():
        ok = v.notna().to_numpy()
        if ok.sum() < 1000 or v[ok].nunique() < 2:
            continue
        auc = roc_auc_score(y[ok], v[ok].to_numpy())
        top = v[ok].rank(pct=True, method="first").to_numpy() >= 0.99
        per_column.append({"column": c, "kind": "score/risk (excluded)" if c in SCORES else "behaviour",
                           "roc_auc": round(float(max(auc, 1 - auc)), 4),
                           "fraud_share_in_top_1pct": round(float(y[ok][top].mean()), 4)})
    for c in CATEGORIES:
        rates = df.groupby(c)["is_fraud"].apply(lambda s: s.astype(str).str.lower().eq("true").mean())
        per_column.append({"column": c, "kind": "category", "fraud_share_min": round(float(rates.min()), 4),
                           "fraud_share_max": round(float(rates.max()), 4), "categories": int(len(rates))})
    per_column.sort(key=lambda r: -r.get("roc_auc", 0))

    cut = int(0.7 * len(df))
    Xb = pd.DataFrame(numeric)[BEHAVIOUR].to_numpy(dtype=float)
    Xs = pd.DataFrame(numeric)[BEHAVIOUR + SCORES].to_numpy(dtype=float)
    cats = pd.get_dummies(df[CATEGORIES].astype(str), dtype=float).to_numpy()
    behaviour = fit_score(Xb[:cut], y[:cut], Xb[cut:], y[cut:])
    with_scores = fit_score(Xs[:cut], y[:cut], Xs[cut:], y[cut:])
    everything = fit_score(np.hstack([Xs, cats])[:cut], y[:cut], np.hstack([Xs, cats])[cut:], y[cut:])

    if with_scores["pr_auc_lift_over_random"] >= 2 and behaviour["pr_auc_lift_over_random"] < 2:
        verdict = ("CIRCULAR: the labels are predictable from the excluded score and risk columns and from nothing "
                   "behavioural. They were derived from those scores; a detector trained on this file learns the "
                   "generator's formula.")
    elif max(behaviour["pr_auc_lift_over_random"], everything["pr_auc_lift_over_random"]) < 2:
        verdict = ("UNRELATED: no column, score, category or combination predicts the labels. They behave as if "
                   "assigned independently of every recorded fact about the transaction.")
    else:
        verdict = "MIXED: see the per-column table."
    report = {"dataset": SRC.name, "rows_sampled": int(len(df)), "fraud_share": round(float(y.mean()), 4),
              "split": "time-ordered, train older 70%, test newer 30%",
              "model_behaviour_only": behaviour, "model_with_excluded_scores": with_scores,
              "model_with_scores_and_categories": everything, "per_column": per_column, "verdict": verdict,
              "note": "Diagnostic of the dataset only. The score columns stay excluded from every evaluation arm (D10f)."}
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_column"}, indent=2))
    for r in per_column[:12]:
        print(r)


if __name__ == "__main__":
    main()

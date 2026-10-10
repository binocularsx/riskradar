"""
Reconciles our random-split results against Chidera's temporal-split finding
(train on older 70%, test on newer 30% by time). Same feature set as
05_gbm_baseline.ipynb (leaky columns already excluded).

Reports ROC-AUC and PR-AUC (vs. random baseline) for:
1. The full model, temporal split (directly comparable to Chidera's numbers)
2. is_ato_risk alone, temporal split (isolates whether OUR strongest single
   feature specifically survives a temporal split or not)
3. The full model, random stratified split (our original methodology, for
   side-by-side comparison in the same run)
"""

import pandas as pd
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, average_precision_score

SOURCE = "../data/processed/nigerian_transactions_clean.csv"

FEATURES = [
    "spending_deviation_score", "velocity_score", "geo_anomaly_score", "amount_ngn",
    "geospatial_velocity_anomaly", "txn_hour", "is_weekend", "is_salary_week", "is_night_txn",
    "device_seen_count", "is_device_shared", "ip_seen_count", "is_ip_shared",
    "user_txn_count_total", "user_avg_txn_amt", "user_std_txn_amt", "user_txn_frequency_24h",
    "txn_count_last_1h", "txn_count_last_24h", "total_amount_last_1h",
    "time_since_last", "avg_gap_between_txns",
    "is_high_risk_state", "is_high_risk_lga", "is_ato_risk",
]

USECOLS = FEATURES + ["is_fraud", "timestamp"]


def load():
    print("Loading...")
    df = pd.read_csv(SOURCE, usecols=USECOLS, parse_dates=["timestamp"])
    print("Loaded:", df.shape)
    return df


def report(name, y_true, y_score, fraud_rate):
    auc = roc_auc_score(y_true, y_score)
    pr_auc = average_precision_score(y_true, y_score)
    pr_auc_ratio = pr_auc / fraud_rate
    print(f"\n--- {name} ---")
    print(f"ROC-AUC: {auc:.4f}  (0.5 = random)")
    print(f"PR-AUC:  {pr_auc:.4f}  ({pr_auc_ratio:.2f}x random baseline of {fraud_rate:.4f})")
    return auc, pr_auc_ratio


def main():
    df = load()

    # Sample down to 1,000,000 rows for speed, same scale as our other work.
    # Stratified sampling here does NOT scramble chronological order - we
    # sort by timestamp AFTER sampling, for both the temporal and random
    # comparisons below.
    df_sample, _ = train_test_split(df, train_size=1_000_000, random_state=42, stratify=df["is_fraud"])
    del df
    fraud_rate = df_sample["is_fraud"].mean()
    print(f"\nSample fraud rate: {fraud_rate:.4f}")

    # ============ TEMPORAL SPLIT (Chidera's methodology) ============
    print("\n" + "=" * 60)
    print("TEMPORAL SPLIT: train on older 70%, test on newer 30%")
    print("=" * 60)

    df_sorted = df_sample.sort_values("timestamp").reset_index(drop=True)
    cutoff = int(len(df_sorted) * 0.7)
    train_t = df_sorted.iloc[:cutoff]
    test_t = df_sorted.iloc[cutoff:]
    print(f"Train: {train_t.shape}, date range {train_t['timestamp'].min()} to {train_t['timestamp'].max()}")
    print(f"Test:  {test_t.shape}, date range {test_t['timestamp'].min()} to {test_t['timestamp'].max()}")

    X_train_t, y_train_t = train_t[FEATURES], train_t["is_fraud"]
    X_test_t, y_test_t = test_t[FEATURES], test_t["is_fraud"]

    model_t = HistGradientBoostingClassifier(
        max_iter=200, max_depth=8, learning_rate=0.1,
        class_weight="balanced", random_state=42
    )
    model_t.fit(X_train_t, y_train_t)
    y_proba_t = model_t.predict_proba(X_test_t)[:, 1]
    report("Full model, TEMPORAL split", y_test_t, y_proba_t, y_test_t.mean())

    # is_ato_risk ALONE, temporal split - isolates whether our strongest
    # single feature specifically survives a temporal split.
    report("is_ato_risk ALONE, TEMPORAL split", y_test_t, X_test_t["is_ato_risk"], y_test_t.mean())

    # ============ RANDOM STRATIFIED SPLIT (our original methodology) ============
    print("\n" + "=" * 60)
    print("RANDOM STRATIFIED SPLIT (our original methodology, same data)")
    print("=" * 60)

    X = df_sample[FEATURES]
    y = df_sample["is_fraud"]
    X_train_r, X_test_r, y_train_r, y_test_r = train_test_split(
        X, y, test_size=0.3, random_state=42, stratify=y
    )
    model_r = HistGradientBoostingClassifier(
        max_iter=200, max_depth=8, learning_rate=0.1,
        class_weight="balanced", random_state=42
    )
    model_r.fit(X_train_r, y_train_r)
    y_proba_r = model_r.predict_proba(X_test_r)[:, 1]
    report("Full model, RANDOM split", y_test_r, y_proba_r, y_test_r.mean())
    report("is_ato_risk ALONE, RANDOM split", y_test_r, X_test_r["is_ato_risk"], y_test_r.mean())


if __name__ == "__main__":
    main()

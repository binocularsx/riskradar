"""
Regenerates training data by running the RAW dataset through
ml/features.py's compute_features() - the same function the live backend
will call - instead of relying on the HuggingFace dataset's own pre-baked
aggregate columns (user_avg_txn_amt, etc.), which were never guaranteed to
match our module's logic.

Processes whole customers (all of a customer's transactions, in time order)
rather than random individual rows, because the rolling features (velocity,
spending baseline, device history) only make sense computed over a real
chronological sequence.

Usage:
    venv/Scripts/python.exe ml/regenerate_training_data.py
"""

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.features import Transaction, compute_features_batch, FEATURE_NAMES

SOURCE_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "nigerian_transactions_clean.csv"
OUTPUT_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "nigerian_transactions_v2_features.csv"

TARGET_ROWS = 1_000_000  # same scale as the earlier training sample, for a fair comparison

RAW_COLUMNS = [
    "transaction_id", "sender_account", "timestamp", "amount_ngn",
    "device_hash", "state", "location", "is_fraud", "fraud_type", "source",
]


def load_raw():
    print("Loading raw columns from", SOURCE_CSV)
    df = pd.read_csv(SOURCE_CSV, usecols=RAW_COLUMNS, parse_dates=["timestamp"])
    print("Loaded:", df.shape)
    return df


MIN_HISTORY_LENGTH = 8  # a customer needs at least this many transactions for
# the rolling behavioural features (velocity, spending baseline, device
# history) to mean anything - see docs/updates/ml-progress-update-4.md for
# why this was added (median history length was 2 without this filter,
# collapsing is_ato_risk and other features to near-zero signal).


def pick_customers_up_to_target(df: pd.DataFrame, target_rows: int) -> pd.DataFrame:
    """Select whole customers (all their rows) until we hit ~target_rows,
    prioritising customers with enough history for behavioural features to
    be meaningful, so every included customer keeps a complete, real,
    USABLE history - not just a technically-complete but too-short one."""
    counts = df.groupby("sender_account").size()
    counts = counts[counts >= MIN_HISTORY_LENGTH]
    print(f"Customers with >= {MIN_HISTORY_LENGTH} transactions: {len(counts)}")
    counts = counts.sample(frac=1.0, random_state=42)

    running_total = 0
    chosen = []
    for customer_id, n in counts.items():
        if running_total >= target_rows:
            break
        chosen.append(customer_id)
        running_total += n
    print(f"Selected {len(chosen)} customers, ~{running_total} rows")
    return df[df["sender_account"].isin(chosen)]


def to_feature_location(row) -> str | None:
    """Lekki-vs-rest LGA detail only applies to Lagos rows (see
    02_feature_engineering.ipynb); everywhere else, use the state."""
    if row["state"] == "Lagos":
        return row["location"]  # already an LGA name for Lagos rows
    return row["state"]


def build_transactions(customer_df: pd.DataFrame) -> list[Transaction]:
    customer_df = customer_df.sort_values("timestamp")
    txns = []
    for _, row in customer_df.iterrows():
        txns.append(Transaction(
            transaction_reference=str(row["transaction_id"]),
            customer_token=str(row["sender_account"]),
            amount_minor=int(round(row["amount_ngn"] * 100)),
            occurred_at=row["timestamp"].to_pydatetime(),
            channel="UNKNOWN",     # not used by compute_features; kept for contract completeness
            instrument="UNKNOWN",
            payment_rail="UNKNOWN",
            device_id=str(row["device_hash"]) if pd.notna(row["device_hash"]) else None,
            location=to_feature_location(row),
        ))
    return txns, customer_df


def main():
    start = time.time()
    df = load_raw()
    df = pick_customers_up_to_target(df, TARGET_ROWS)

    all_rows = []
    n_customers = df["sender_account"].nunique()
    print(f"Processing {n_customers} customers through compute_features()...")

    for i, (customer_id, customer_df) in enumerate(df.groupby("sender_account")):
        txns, sorted_df = build_transactions(customer_df)
        feature_dicts = compute_features_batch(txns)

        for feat, (_, raw_row) in zip(feature_dicts, sorted_df.iterrows()):
            feat["is_fraud"] = raw_row["is_fraud"]
            feat["fraud_type"] = raw_row["fraud_type"]
            feat["source"] = raw_row["source"]
            feat["transaction_id"] = raw_row["transaction_id"]
            all_rows.append(feat)

        if (i + 1) % 500 == 0:
            elapsed = time.time() - start
            print(f"  {i + 1}/{n_customers} customers done ({elapsed:.1f}s elapsed)")

    result = pd.DataFrame(all_rows)
    result = result[FEATURE_NAMES + ["is_fraud", "fraud_type", "source", "transaction_id"]]
    result.to_csv(OUTPUT_CSV, index=False)

    print(f"\nDone in {time.time() - start:.1f}s")
    print(f"Wrote {result.shape[0]} rows, {result.shape[1]} columns to {OUTPUT_CSV}")
    print(f"Fraud rate: {result['is_fraud'].mean():.4f}")


if __name__ == "__main__":
    main()

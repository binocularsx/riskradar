"""Join the v2 engineered-feature table back to the transactions it describes (D81).

    python ml/datasets/join_v2_features.py

``nigerian_transactions_v2_features.csv`` carries engineered columns and a
``transaction_id`` but no time, accounts or channel, so nothing behavioural can
be computed from it alone. This takes those 1,000,002 transactions' raw fields
from ``nigerian_transactions_clean.csv`` and the engineered columns (prefixed
``v2_``) from the feature table, and writes one file the evaluator can read.
"""

from pathlib import Path

import pandas as pd

SRC = Path(r"C:\Users\CHIDERA\Downloads\processed\processed")
OUT = Path(__file__).resolve().parents[1] / "data" / "public" / "nigerian_v2_joined.csv"

features = pd.read_csv(SRC / "nigerian_transactions_v2_features.csv")
features = features.rename(columns={c: f"v2_{c}" for c in features.columns if c != "transaction_id"})
wanted = set(features["transaction_id"])
parts = [c[c["transaction_id"].isin(wanted)]
         for c in pd.read_csv(SRC / "nigerian_transactions_clean.csv", chunksize=500_000, low_memory=False)]
raw = pd.concat(parts, ignore_index=True)
joined = raw.merge(features, on="transaction_id", how="inner", validate="one_to_one")
disagree = int((joined["is_fraud"].astype(str) != joined["v2_is_fraud"].astype(str)).sum())
joined.to_csv(OUT, index=False)
print(f"{len(features):,} feature rows, {len(raw):,} matched transactions, {len(joined):,} joined; "
      f"labels disagree on {disagree:,} rows -> {OUT}")

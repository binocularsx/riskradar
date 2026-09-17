"""Join IEEE-CIS transactions to their identity rows (D81).

    python ml/datasets/join_ieee.py

Kaggle's IEEE-CIS Fraud Detection (Vesta) ships ``train_transaction.csv`` and
``train_identity.csv``. Only about a quarter of transactions have an identity
row; the device fields live there. This writes one file with the transaction
columns and ``DeviceType``/``DeviceInfo`` beside them, for
``ml/evaluate_dataset.py`` with ``ml/datasets/ieee_cis.mapping.yaml``.

Put the two CSVs in ``ml/data/public/`` (named ``train_transaction.csv`` and
``train_identity.csv``, or with an ``ieee_`` prefix) first.
"""

from pathlib import Path

import pandas as pd

PUBLIC = Path(__file__).resolve().parents[1] / "data" / "public"


def find(name: str) -> Path:
    for candidate in (PUBLIC / name, PUBLIC / f"ieee_{name}"):
        if candidate.exists():
            return candidate
    raise SystemExit(f"missing {PUBLIC / name}: download it from the Kaggle competition page")


tx = pd.read_csv(find("train_transaction.csv"))
ident = pd.read_csv(find("train_identity.csv"), usecols=["TransactionID", "DeviceType", "DeviceInfo"])
joined = tx.merge(ident, on="TransactionID", how="left", validate="one_to_one")
out = PUBLIC / "ieee_joined.csv"
joined.to_csv(out, index=False)
print(f"{len(tx):,} transactions, {joined['DeviceInfo'].notna().sum():,} with device info, "
      f"fraud share {joined['isFraud'].mean():.4f} -> {out}")

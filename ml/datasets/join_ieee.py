"""Join IEEE-CIS transactions to their identity rows (D81).

    python ml/datasets/join_ieee.py            # both splits
    python ml/datasets/join_ieee.py train

Kaggle's IEEE-CIS Fraud Detection (Vesta) ships ``{train,test}_transaction.csv``
and ``{train,test}_identity.csv``. About a quarter of transactions have an
identity row, which is where the device lives. This writes
``ieee_{split}_joined.csv`` with the columns ``ieee_cis.mapping.yaml`` reads,
plus ``DeviceType`` and ``DeviceInfo``.

The test split has **no labels**: the competition never published them. It
can be scored and replayed, not measured.
"""

import sys
from pathlib import Path

import pandas as pd

PUBLIC = Path(__file__).resolve().parents[1] / "data" / "public"
KEEP = (["TransactionID", "isFraud", "TransactionDT", "TransactionAmt", "ProductCD", "card1", "card2", "card3",
         "card4", "card5", "card6", "addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain"]
        + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 16)])


def join(split: str) -> None:
    tx_path = PUBLIC / f"{split}_transaction.csv"
    if not tx_path.exists():
        raise SystemExit(f"missing {tx_path}: download it from the Kaggle competition page")
    header = pd.read_csv(tx_path, nrows=0).columns
    tx = pd.read_csv(tx_path, usecols=[c for c in KEEP if c in header])
    ident = pd.read_csv(PUBLIC / f"{split}_identity.csv", usecols=["TransactionID", "DeviceType", "DeviceInfo"])
    joined = tx.merge(ident, on="TransactionID", how="left", validate="one_to_one")
    # Kaggle hides card numbers. card1-card5 name a card range, not a card, so on
    # their own they lump many cardholders together and every "card" looks like a
    # burst. D1 is days since the card relationship began; the transaction's day
    # minus D1 is the same for every transaction of one card, which is the
    # standard way to recover a card on this dataset. It is a fact about the
    # account, known when the transaction is made, not a label.
    joined["card_start_day"] = (joined["TransactionDT"] // 86400 - joined["D1"]).astype("Int64")
    out = PUBLIC / f"ieee_{split}_joined.csv"
    joined.to_csv(out, index=False)
    fraud = f"fraud share {joined['isFraud'].mean():.4f}" if "isFraud" in joined else "no labels (competition test set)"
    print(f"{split}: {len(joined):,} transactions, {joined['DeviceInfo'].notna().sum():,} with device info, "
          f"TransactionDT {joined['TransactionDT'].min():,} to {joined['TransactionDT'].max():,}, {fraud} -> {out.name}")


if __name__ == "__main__":
    for split in (sys.argv[1:] or ["train", "test"]):
        join(split)

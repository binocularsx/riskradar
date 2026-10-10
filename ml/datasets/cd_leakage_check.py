"""
Look-ahead leakage check for IEEE-CIS's C1-C14 / D1-D15 columns (the
"ceiling" arm that jumps ROC-AUC 0.755 -> 0.873, never before audited -
see D81c/D81e in docs/decisions.md).

Logic: reconstruct each card using the same identity scheme join_ieee.py
already uses (card1 + addr1 + P_emaildomain + card_start_day). For a card
with multiple transactions spread over time, a genuinely point-in-time
"count so far" column should usually change as more transactions happen
(and should never look identical at the card's very first transaction and
its last, weeks later, if it's tracking anything real). A column that's
constant across a card's entire history regardless of transaction date is
a signature of a value computed over the WHOLE dataset (including future
rows relative to any given transaction) rather than "as of now."

This is a signature check, not a certainty - it flags suspects for further
scrutiny, the same way we've done at every leakage check in this project.
"""

import pandas as pd
import numpy as np

df = pd.read_csv("ml/data/public/ieee_train_joined.csv")
print("Loaded:", df.shape)

df["card_id"] = (
    df["card1"].astype(str) + "_" + df["addr1"].astype(str) + "_" +
    df["P_emaildomain"].astype(str) + "_" + df["card_start_day"].astype(str)
)

# Only cards with enough transactions spread over a real time window are
# informative - a card with 1-2 transactions can't tell us anything either way.
card_counts = df.groupby("card_id").size()
multi_txn_cards = card_counts[card_counts >= 5].index
sub = df[df["card_id"].isin(multi_txn_cards)].copy()
print(f"Cards with >=5 transactions: {len(multi_txn_cards)}, covering {len(sub)} rows")

c_cols = [f"C{i}" for i in range(1, 15)]
d_cols = [f"D{i}" for i in range(1, 16)]

print("\n=== Fraction of multi-transaction cards where this column is CONSTANT across all their transactions ===")
print("(High fraction = suspicious: a real point-in-time count should vary as more transactions happen)\n")

for col in c_cols + d_cols:
    if col not in sub.columns:
        continue
    nunique_per_card = sub.groupby("card_id")[col].nunique(dropna=False)
    frac_constant = (nunique_per_card == 1).mean()
    print(f"{col:>5}: {frac_constant:.1%} of multi-txn cards have a CONSTANT value across their whole history")

# Direct check: for cards spanning a long time window, does the column value
# at their FIRST transaction differ from their LAST?
print("\n=== For cards spanning >14 days, does C/D value change from first to last transaction? ===")
sub_sorted = sub.sort_values(["card_id", "TransactionDT"])
first_last = sub_sorted.groupby("card_id").agg(
    first_dt=("TransactionDT", "first"), last_dt=("TransactionDT", "last"),
    **{f"{c}_first": (c, "first") for c in c_cols[:3]},
    **{f"{c}_last": (c, "last") for c in c_cols[:3]},
)
first_last["span_days"] = (first_last["last_dt"] - first_last["first_dt"]) / 86400
long_span = first_last[first_last["span_days"] > 14]
print(f"Cards spanning >14 days: {len(long_span)}")
for c in c_cols[:3]:
    same = (long_span[f"{c}_first"] == long_span[f"{c}_last"]).mean()
    print(f"{c}: identical at first vs last transaction for {same:.1%} of these long-span cards")

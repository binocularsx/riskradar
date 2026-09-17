"""
PaySim mule fan-out validation.

REAL DATA: step, type, amount, nameOrig, nameDest, isFraud - loaded as-is
from Kaggle's PaySim1 export, no modification to their values.

DROPPED (leakage, per docs/updates/ml-progress-update-3.md's standard):
- oldbalanceOrg, newbalanceOrig, oldbalanceDest, newbalanceDest: PaySim's own
  fraud-generation process drains these to near-zero as part of simulating
  fraud, making them a near-direct giveaway rather than a genuine signal.
- isFlaggedFraud: this is the ORIGINAL PAYSIM PAPER'S OWN detection rule
  output (transfer > 200,000 in one hop), not a real label - training on it
  would be leakage of someone else's already-computed fraud flag.

DERIVED / HAND-BUILT (not a real "mule fan-out" label - PaySim's `isFraud`
is a general fraud flag, not mule-specific):
- distinct_dest_last5: for each sender (nameOrig), the number of distinct
  recipients among their last 5 outbound TRANSFER/CASH_OUT transactions.
- is_fan_out: distinct_dest_last5 >= 3 (heuristic threshold, not tuned).

This script only MEASURES whether the hand-built signal correlates with
PaySim's real isFraud label. It does not claim PaySim confirms "mule
fan-out" specifically - see the markdown note in the companion notebook
for the full disclosure.
"""

import pandas as pd

SOURCE = r"data/public/paysim.csv"

REAL_COLUMNS = ["step", "type", "amount", "nameOrig", "nameDest", "isFraud"]


def load_clean():
    print("Loading REAL PaySim columns only (leaky balance/flag columns excluded)...")
    df = pd.read_csv(
        SOURCE,
        usecols=REAL_COLUMNS,
        dtype={
            "step": "int32", "type": "category", "amount": "float32",
            "nameOrig": "str", "nameDest": "str", "isFraud": "int8",
        },
    )
    print("Loaded:", df.shape)
    print("Real fraud rate (PaySim's own isFraud label):", df["isFraud"].mean())
    return df


def build_fan_out_signal(df: pd.DataFrame) -> pd.DataFrame:
    # Only TRANSFER/CASH_OUT are outbound-movement types - the shape a mule's
    # outward redistribution would take, per the original request's own
    # framing.
    mask = df["type"].isin(["TRANSFER", "CASH_OUT"])
    out_df = df[mask].copy()
    print(f"TRANSFER/CASH_OUT rows: {out_df.shape[0]} of {df.shape[0]} total")

    out_df["dest_code"] = out_df["nameDest"].astype("category").cat.codes
    out_df = out_df.sort_values(["nameOrig", "step"])

    # DERIVED: distinct destinations among this sender's last 5 outbound
    # transactions (by transaction order, not by time-step). This is a hand-
    # built proxy for "fanning money out to many different destinations."
    out_df["distinct_dest_last5"] = (
        out_df.groupby("nameOrig")["dest_code"]
        .rolling(window=5, min_periods=1)
        .apply(lambda x: len(set(x)), raw=True)
        .reset_index(level=0, drop=True)
    )

    # DERIVED: heuristic threshold, not tuned against any label.
    out_df["is_fan_out"] = (out_df["distinct_dest_last5"] >= 3).astype(int)

    return out_df


def validate(out_df: pd.DataFrame):
    print("\n=== Validation: does the hand-built fan-out signal correlate with PaySim's real isFraud? ===")
    print(out_df.groupby("is_fan_out")["isFraud"].agg(["mean", "count"]))

    print("\nRecall: of all real fraud rows (TRANSFER/CASH_OUT only), what fraction does is_fan_out catch?")
    fraud_rows = out_df[out_df["isFraud"] == 1]
    recall = fraud_rows["is_fan_out"].mean()
    print(f"Recall: {recall:.4f} ({fraud_rows['is_fan_out'].sum()} of {len(fraud_rows)} real fraud rows flagged)")

    print("\nPrecision: of all rows flagged is_fan_out, what fraction are real fraud?")
    flagged = out_df[out_df["is_fan_out"] == 1]
    precision = flagged["isFraud"].mean()
    print(f"Precision: {precision:.4f} ({flagged['isFraud'].sum()} of {len(flagged)} flagged rows are real fraud)")


def main():
    df = load_clean()
    out_df = build_fan_out_signal(df)
    validate(out_df)


if __name__ == "__main__":
    main()

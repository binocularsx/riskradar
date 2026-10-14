"""Run the pipeline against public datasets we did not generate.

Plain English
-------------
Everything else in this project grades itself. We wrote the simulator, we wrote
the detector, and no amount of care changes the fact that both encode the same
person's idea of what fraud looks like. This script is the first thing here that
uses data somebody else made.

    python ml/validate_public.py --dataset paysim
    python ml/validate_public.py --dataset ieee

What each dataset can and cannot settle is stated in the output, because the
difference matters more than the numbers:

* **PaySim** is synthetic, so it cannot validate detection. What it *can* do is
  prove the feature package computes sane values on foreign data, and exercise
  imbalance handling and threshold discipline at a 0.13% base rate. It has one
  limitation that is rarely mentioned in the papers that use it, and that we
  measured rather than assumed: **its accounts have no history.** Mean
  transactions per sender is 1.001 and the maximum is 3, so every sender-side
  behavioural feature is inert. Destinations do repeat, so destination-novelty
  features are exercised.

* **IEEE-CIS** has **real fraud labels** from real e-commerce chargebacks. It is
  the only rung on the ladder that changes what we are allowed to claim. Its
  features are anonymised, so it validates *methodology* — imbalance, calibration,
  threshold derivation, temporal splitting — not our feature semantics.

Nothing here is scored with the simulator-trained model. Each dataset trains its
own model with the same pipeline, because a model trained on Nigerian retail
transfers has no business scoring anonymised e-commerce.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

import pandas as pd  # noqa: E402

from metrics import (  # noqa: E402
    pr_auc,
    recall_at_budget,
    reliability_curve,
    roc_auc,
    value_weighted_recall,
)
from train import build_model  # noqa: E402

PUBLIC = REPO_ROOT / "ml" / "data" / "public"


# ---------------------------------------------------------------------------
# PaySim
# ---------------------------------------------------------------------------


def load_paysim(limit: int | None) -> tuple[pd.DataFrame, dict]:
    """Map PaySim onto our canonical shape and run our own feature package.

    PaySim's ``step`` is an hour index over roughly 30 days, so it becomes a
    real timestamp. ``nameOrig`` is the account, ``nameDest`` the beneficiary.
    There is no device, no decline and no account-opening date, which means
    five of our twelve features are structurally unavailable — reported rather
    than quietly defaulted.
    """
    path = PUBLIC / "paysim.csv"
    if not path.exists():
        raise SystemExit(f"missing {path}. See docs/validation-plan.md for how to fetch it.")

    print("  reading paysim.csv …")
    df = pd.read_csv(
        path,
        usecols=["step", "type", "amount", "nameOrig", "nameDest", "isFraud"],
        nrows=limit,
    )

    # Fraud in PaySim only ever appears in TRANSFER and CASH_OUT. Keeping the
    # other types in is correct — they are the negative population a real system
    # has to see past — but it is worth stating, because a paper that filters to
    # only those two types is reporting a much easier problem.
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df["occurred_at"] = [origin + timedelta(hours=int(s)) for s in df["step"]]
    df["amount_minor"] = (df["amount"] * 100).round().astype("int64")
    df["account_token"] = df["nameOrig"]
    df["subject_token"] = df["nameOrig"]      # PaySim has no customer above the account
    df["beneficiary_token"] = df["nameDest"]
    df["device_token"] = None
    df["auth_result"] = "APPROVED"            # PaySim records no declines
    df["is_fraud"] = df["isFraud"].astype(int)
    # PaySim has no incident grouping; each fraudulent transaction stands alone,
    # so incident-level recall collapses to transaction-level here.
    df["incident_id"] = np.where(df["is_fraud"] == 1, df.index.astype(str), None)

    notes = {
        "dataset": "PaySim (Lopez-Rojas), CC BY-SA 4.0",
        "synthetic": True,
        "rows": int(len(df)),
        "fraud_rate_pct": round(100 * float(df["is_fraud"].mean()), 4),
        "measured_limitations": {
            "sender_history": "mean 1.001 transactions per sender, max 3 — every "
                              "sender-side behavioural feature is inert",
            "destination_history": "mean 2.34 per destination, max 113 — "
                                   "destination-novelty features are exercised",
            "no_declines": "auth_result is always APPROVED, so the two features "
                           "added by D21 cannot fire",
            "no_account_age": "no opening date, so account_age_days and dormancy "
                              "report NEVER_SEEN throughout",
            "no_incidents": "fraud is per-transaction; incident-level recall "
                            "degenerates to transaction-level",
        },
        "therefore": "validates that the pipeline runs on foreign data and that "
                     "threshold discipline holds at a realistic base rate. It "
                     "cannot validate detection, and it cannot validate the "
                     "behavioural features that are this system's actual claim.",
    }
    return df, notes


# ---------------------------------------------------------------------------
# IEEE-CIS
# ---------------------------------------------------------------------------


def load_ieee(limit: int | None) -> tuple[pd.DataFrame, dict]:
    """Real chargeback labels. The only dataset here that changes the claim.

    The features are anonymised (``V1``…``V339``, ``card1``…``card6``), so our
    feature package cannot run on it — and pretending otherwise would be the
    exact self-deception this whole exercise exists to avoid. Instead we run the
    *same modelling discipline* on their features: temporal split, isotonic
    calibration, threshold from an alert budget, PR-AUC and a reliability curve.
    """
    path = PUBLIC / "ieee_train_transaction.csv"
    if not path.exists():
        raise SystemExit(
            f"missing {path}. IEEE-CIS is behind Kaggle competition rules; see "
            "docs/validation-plan.md."
        )

    print("  reading ieee_train_transaction.csv …")
    df = pd.read_csv(path, nrows=limit)
    df = df.rename(columns={"isFraud": "is_fraud", "TransactionAmt": "amount"})
    # TransactionDT is seconds from an unstated reference point; only ordering
    # and relative spacing matter, both of which it preserves.
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df["occurred_at"] = [origin + timedelta(seconds=int(s)) for s in df["TransactionDT"]]
    df["amount_minor"] = (df["amount"] * 100).round().astype("int64")
    df["incident_id"] = np.where(df["is_fraud"] == 1, df.index.astype(str), None)

    notes = {
        "dataset": "IEEE-CIS Fraud Detection (Vesta), Kaggle competition rules",
        "synthetic": False,
        "rows": int(len(df)),
        "fraud_rate_pct": round(100 * float(df["is_fraud"].mean()), 4),
        "measured_limitations": {
            "anonymised_features": "V1-V339 and card1-card6 have no published "
                                   "semantics, so our feature package cannot run "
                                   "on it and its features cannot transfer to ours",
            "domain": "card-not-present e-commerce, not Nigerian retail transfers",
            "no_incidents": "labels are per-transaction",
        },
        "therefore": "the only rung here with real labels. Validates imbalance "
                     "handling, calibration quality, temporal splitting and "
                     "threshold discipline against ground truth somebody else "
                     "produced.",
    }
    return df, notes


# ---------------------------------------------------------------------------
# Shared evaluation
# ---------------------------------------------------------------------------


def our_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Run **our** feature package over a foreign dataset.

    This is the part that proves the plumbing: the same twelve functions the
    live worker calls, fed rows we did not generate.
    """
    from riskradar.features import (
        FEATURE_NAMES,
        PandasHistorySource,
        TxView,
        compute_features,
        to_vector,
    )

    rows = df[[
        "occurred_at", "amount_minor", "auth_result",
        "subject_token", "account_token", "beneficiary_token", "device_token",
    ]].to_dict("records")

    print("  indexing history …")
    source = PandasHistorySource(rows)

    print("  computing features with riskradar.features …")
    X = np.zeros((len(rows), len(FEATURE_NAMES)), dtype=float)
    for i, r in enumerate(rows):
        tx = TxView(
            transaction_ref=str(i),
            occurred_at=r["occurred_at"],
            amount_minor=int(r["amount_minor"]),
            currency="NGN",
            channel="API",
            instrument="ACCOUNT_TRANSFER",
            rail="NIP",
            subject_token=r["subject_token"],
            account_token=r["account_token"],
            beneficiary_token=r["beneficiary_token"],
            device_token=r["device_token"],
            auth_result=r["auth_result"],
        )
        X[i] = to_vector(compute_features(tx, source.load(tx)))
        if i and i % 200_000 == 0:
            print(f"    {i} / {len(rows)}")
    return X, list(FEATURE_NAMES)


def native_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Use the dataset's own numeric columns. For IEEE-CIS, where ours cannot run."""
    drop = {"is_fraud", "incident_id", "occurred_at", "TransactionID", "amount"}
    cols = [
        c for c in df.columns
        if c not in drop and pd.api.types.is_numeric_dtype(df[c])
    ]
    X = df[cols].fillna(-999).to_numpy(dtype=float)
    return X, cols


def evaluate(df: pd.DataFrame, X: np.ndarray, names: list[str], notes: dict,
             budget_per_day: int) -> dict:
    """Temporal split, calibrated model, budget-derived threshold."""
    y = df["is_fraud"].to_numpy(dtype=int)
    times = df["occurred_at"].to_numpy()
    order = np.argsort(times)
    X, y, times = X[order], y[order], times[order]
    amounts = df["amount_minor"].to_numpy()[order]

    # A **temporal** split, not a random one. Fraud is non-stationary; shuffling
    # lets the model see the future and is the commonest way a fraud paper
    # reports a number it could never reproduce in production.
    cut = int(0.75 * len(y))
    print(f"  temporal split: {cut} train / {len(y) - cut} test")

    model = build_model()
    model.fit(X[:cut], y[:cut])
    p = model.predict_proba(X[cut:])[:, 1]

    y_test = y[cut:]
    span_days = max(
        (pd.Timestamp(times[-1]) - pd.Timestamp(times[cut])).total_seconds() / 86400.0, 1.0
    )

    result = {
        **notes,
        "features_used": len(names),
        "feature_source": "riskradar.features" if names[0] == "amount_log10" else "dataset native",
        "test_rows": int(len(y_test)),
        "test_span_days": round(span_days, 2),
        "test_base_rate_pct": round(100 * float(y_test.mean()), 4),
        "pr_auc": round(pr_auc(y_test, p), 4),
        "roc_auc": round(roc_auc(y_test, p), 4),
        "random_baseline_pr_auc": round(float(y_test.mean()), 4),
        "at_alert_budget": recall_at_budget(
            y_test, p, budget_per_day=budget_per_day, days=span_days
        ),
        "value_weighted": value_weighted_recall(
            y_test, p, amounts[cut:], budget_per_day=budget_per_day, days=span_days
        ),
        "reliability": reliability_curve(y_test, p),
    }

    # A feature that never varies told us nothing and should be named, not
    # silently carried. On PaySim this is how the "no account history" finding
    # surfaces on its own.
    inert = [n for i, n in enumerate(names) if float(np.std(X[:, i])) == 0.0]
    if inert:
        result["inert_features"] = inert
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate against public data")
    parser.add_argument("--dataset", choices=["paysim", "ieee"], required=True)
    parser.add_argument("--limit", type=int, default=1_200_000)
    parser.add_argument("--budget", type=int, default=120)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    print(f"=== {args.dataset} ===")
    if args.dataset == "paysim":
        df, notes = load_paysim(args.limit)
        X, names = our_features(df)
    else:
        df, notes = load_ieee(args.limit)
        X, names = native_features(df)

    result = evaluate(df, X, names, notes, args.budget)

    print()
    print(json.dumps({k: v for k, v in result.items() if k != "reliability"}, indent=2))

    out = REPO_ROOT / (args.out or f"ml/artifacts/public-validation-{args.dataset}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

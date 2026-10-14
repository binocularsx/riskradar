"""Run both fraud pipelines and compare them on a common footing.

    python ml/compare_pipelines.py --rows 1000000

Why this exists
---------------
Two people on this team have built fraud models. They sit on different datasets
whose fraud rates differ by a factor of twelve, use different features, and are
evaluated at different operating points. Their headline numbers are therefore
not comparable at all: one reports 97.6% recall, the other 0.73 incident recall,
and putting those side by side would mean nothing.

So this does not compare headline numbers. It runs five arms designed so the
*differences between arms* are attributable to one thing each.

    A  IRE as documented        reproduce the reported result faithfully
    B  minus the device flag    is the model reading a real signal or a switch?
    C  temporal split           does a random split let it see the future?
    D  our protocol, their data  same data, our evaluation discipline
    E  ours on ours             reference point

Arm B is the one that matters, and it is written so it can exonerate IRE's
approach. If precision holds up without ``new_device_transaction``, the model
learned real structure and the objection to the dataset is wrong. If it
collapses, the model was reading the generator's own flag. Either way the
experiment answers it, not an argument.

There is also a per-column audit: train a model on each single column alone and
report its AUC. Any column that scores near 1.0 by itself is a copy of the
label, whatever its name suggests.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

import pandas as pd  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402

from metrics import (  # noqa: E402
    reliability_curve,
    threshold_for_alert_budget,
    value_weighted_recall,
)

warnings.filterwarnings("ignore", category=UserWarning)

DEFAULT_DATA = REPO_ROOT / "ml" / "data" / "public" / "electricsheep.csv"

# IRE's documented feature set: the dataset's own numeric and boolean columns.
# There are exactly 29 of them, which matches "Feature set (29 features)" in
# Progress Update #2 — so this is a faithful reproduction rather than a guess.
#
# Not reproduced: the geographic augmentation from Update #1, which added
# synthetic rows for 27 missing states. That affects two engineered columns we
# cannot rebuild without IRE's notebook, and it cannot explain a precision
# ceiling that Update #2 attributes squarely to device novelty.
IRE_FEATURES = [
    "time_since_last_transaction", "spending_deviation_score", "velocity_score",
    "geo_anomaly_score", "amount_ngn", "bvn_linked", "new_device_transaction",
    "geospatial_velocity_anomaly", "txn_hour", "is_weekend", "is_salary_week",
    "is_night_txn", "device_seen_count", "is_device_shared", "ip_seen_count",
    "is_ip_shared", "user_txn_count_total", "user_avg_txn_amt", "user_std_txn_amt",
    "user_txn_frequency_24h", "txn_count_last_1h", "txn_count_last_24h",
    "total_amount_last_1h", "time_since_last", "avg_gap_between_txns",
    "merchant_fraud_rate", "channel_risk_score", "persona_fraud_risk",
    "location_fraud_risk",
]

# The switch, plus anything that trivially encodes it.
DEVICE_COLUMNS = ["new_device_transaction"]

# Global per-customer aggregates. These are computed over the whole file, so for
# any given row they include that customer's *later* transactions. A model using
# them has seen the future — the same defect D22 exists to prevent in our schema.
LOOKAHEAD_COLUMNS = ["user_txn_count_total", "user_avg_txn_amt", "user_std_txn_amt"]

LOAD_COLUMNS = IRE_FEATURES + ["is_fraud", "fraud_type", "timestamp"]

# IRE's configuration, verbatim from Progress Update #2 §3.
IRE_MODEL = dict(
    n_estimators=100, max_depth=20, min_samples_leaf=5,
    class_weight="balanced_subsample", random_state=42, n_jobs=-1,
)

ALERT_BUDGET_PER_DAY = 120  # D24


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load(rows: int | None, path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"missing {path}")

    print(f"  reading {path.name} …")
    # Same memory discipline IRE documented: only the needed columns, narrow dtypes.
    df = pd.read_csv(path, usecols=LOAD_COLUMNS, nrows=rows)
    df["is_fraud"] = df["is_fraud"].astype(int)
    for c in IRE_FEATURES:
        if df[c].dtype == bool:
            df[c] = df[c].astype("int8")
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    df[IRE_FEATURES] = df[IRE_FEATURES].fillna(-999)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    print(f"  {len(df):,} rows · fraud rate {100 * df.is_fraud.mean():.3f}%")
    return df


# ---------------------------------------------------------------------------
# Evaluation shared by every arm
# ---------------------------------------------------------------------------


def at_threshold(y: np.ndarray, p: np.ndarray, t: float) -> dict:
    flagged = p >= t
    tp = int(np.sum(flagged & (y == 1)))
    n_flag = int(np.sum(flagged))
    return {
        "threshold": round(float(t), 4),
        "flagged": n_flag,
        "precision": round(tp / n_flag, 4) if n_flag else 0.0,
        "recall": round(tp / max(int(np.sum(y == 1)), 1), 4),
    }


def run_arm(
    label: str, df: pd.DataFrame, features: list[str], *, temporal: bool, note: str
) -> dict:
    X = df[features].to_numpy(dtype=np.float32)
    y = df["is_fraud"].to_numpy(dtype=int)

    if temporal:
        order = np.argsort(df["timestamp"].to_numpy())
        X, y = X[order], y[order]
        cut = int(0.8 * len(y))
        Xtr, Xte, ytr, yte = X[:cut], X[cut:], y[:cut], y[cut:]
        split = "temporal 80/20"
    else:
        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, stratify=y, random_state=42
        )
        split = "stratified random 80/20"

    print(f"\n[{label}] {note}")
    print(f"  {len(features)} features · {split} · training …")

    model = RandomForestClassifier(**IRE_MODEL)
    model.fit(Xtr, ytr)
    p = model.predict_proba(Xte)[:, 1]

    importances = sorted(
        zip(features, model.feature_importances_), key=lambda kv: -kv[1]
    )[:6]

    result = {
        "arm": label,
        "note": note,
        "features": len(features),
        "split": split,
        "test_rows": int(len(yte)),
        "test_fraud_rate_pct": round(100 * float(yte.mean()), 4),
        "pr_auc": round(float(average_precision_score(yte, p)), 4),
        "roc_auc": round(float(roc_auc_score(yte, p)), 4),
        "random_baseline_pr_auc": round(float(yte.mean()), 4),
        # IRE's reported operating points, so the reproduction is checkable.
        "at_thresholds": [at_threshold(yte, p, t) for t in (0.1, 0.3, 0.5, 0.6)],
        "top_features": [{"name": n, "importance": round(float(v), 4)} for n, v in importances],
    }

    for row in result["at_thresholds"]:
        print(f"    t={row['threshold']}  precision {row['precision']:.4f}  "
              f"recall {row['recall']:.4f}  flagged {row['flagged']:,}")
    print(f"    top feature: {importances[0][0]} ({importances[0][1]:.3f})")
    return result, yte, p


def our_protocol(label: str, y: np.ndarray, p: np.ndarray, amounts: np.ndarray,
                 days: float) -> dict:
    """Our evaluation discipline applied to somebody else's scores.

    The difference that matters is the threshold. IRE picks 0.3 on a
    cost argument; we solve backwards from how many alerts a team can actually
    review in a day (D11d). On a million transactions those are wildly
    different operating points, and the gap is the point.
    """
    t = threshold_for_alert_budget(p, budget_per_day=ALERT_BUDGET_PER_DAY, days=days)
    out = {
        "arm": label,
        "alert_budget_per_day": ALERT_BUDGET_PER_DAY,
        "days_covered": round(days, 2),
        "budget_threshold": round(float(t), 6),
        "at_budget": at_threshold(y, p, t),
        "value_weighted": value_weighted_recall(
            y, p, amounts, budget_per_day=ALERT_BUDGET_PER_DAY, days=days
        ),
        "reliability": reliability_curve(y, p, bins=10),
    }
    b = out["at_budget"]
    print(f"\n[{label}] alert budget {ALERT_BUDGET_PER_DAY}/day over {days:.1f} days")
    print(f"    threshold {t:.6f}  precision {b['precision']:.4f}  recall {b['recall']:.4f}")
    print(f"    value recall {out['value_weighted']['value_recall']:.4f} "
          f"vs count recall {out['value_weighted']['count_recall']:.4f}")
    return out


def single_column_audit(df: pd.DataFrame, features: list[str]) -> list[dict]:
    """Audit each column alone, on two different questions.

    **AUC** answers "how well does this column rank fraud above non-fraud". It is
    the usual measure, and on its own it misses the failure mode that matters
    here.

    **Free exclusion at full recall** answers something else: *how much
    legitimate traffic can this one column discard while still keeping every
    single fraud case?* A genuine behavioural signal cannot do that — real fraud
    always overlaps real behaviour somewhere. A column that throws away a
    meaningful slice of legitimate traffic at 100% recall is not describing
    behaviour; it is the switch the generator flipped when it decided to write a
    fraud row.

    That distinction is exactly why a routine audit would have passed
    ``new_device_transaction``: as a *classifier* it is unremarkable (AUC ≈ 0.59,
    because it is true for most legitimate rows too), while as a *filter* it is
    perfect. IRE found this by hand — "0% fraud when False" — and this turns that
    lucky observation into a check that runs on every column.
    """
    print("\n[audit] per-column: AUC, and how much legitimate traffic each one can")
    print("        discard while still keeping 100% of the fraud")
    y = df["is_fraud"].to_numpy(dtype=int)
    n_neg = int(np.sum(y == 0))
    out = []

    for c in features:
        v = df[c].to_numpy(dtype=np.float64)
        if np.std(v) == 0:
            out.append({"column": c, "auc": None, "free_exclusion": None,
                        "verdict": "constant"})
            continue

        auc = float(roc_auc_score(y, v))

        # Every fraud row lies between min and max of the column's fraud values.
        # Whichever side is tighter gives the best single cut that keeps all of
        # them; measure how many legitimate rows that cut removes for free.
        fraud_v = v[y == 1]
        lo, hi = float(fraud_v.min()), float(fraud_v.max())
        excl_below = float(np.sum((y == 0) & (v < lo)))
        excl_above = float(np.sum((y == 0) & (v > hi)))
        free = max(excl_below, excl_above) / max(n_neg, 1)

        verdict = ("GENERATOR SWITCH" if free > 0.05 else
                   "very strong" if max(auc, 1 - auc) > 0.75 else
                   "useful" if max(auc, 1 - auc) > 0.55 else "noise")
        out.append({
            "column": c,
            "auc": round(auc, 4),
            "free_exclusion": round(free, 4),
            "verdict": verdict,
        })

    out.sort(key=lambda r: -(r["free_exclusion"] or 0))
    for r in out[:8]:
        fe = "n/a" if r["free_exclusion"] is None else f"{r['free_exclusion']:.1%}"
        print(f"    {r['column']:32s} auc {r['auc']}  discards {fe:>6s} of legit "
              f"at 100% recall  {r['verdict']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare both pipelines fairly")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--out", default="ml/artifacts/pipeline-comparison.json")
    ap.add_argument("--data", default=None, help="override the CSV path (used for smoke tests)")
    args = ap.parse_args()

    df = load(args.rows, Path(args.data) if args.data else DEFAULT_DATA)
    span_days = float(
        (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 86400.0
    )
    amounts_all = (df["amount_ngn"].to_numpy(dtype=float) * 100).astype("int64")

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection"
                   " (HuggingFace) — the source rejected by D10e",
        "rows_used": int(len(df)),
        "fraud_rate_pct": round(100 * float(df.is_fraud.mean()), 4),
        "span_days": round(span_days, 1),
        "model_config": {k: v for k, v in IRE_MODEL.items() if k != "n_jobs"},
        "arms": {},
    }

    # ---- A: reproduce IRE as documented ----------------------------------
    a, ya, pa = run_arm(
        "A", df, IRE_FEATURES, temporal=False,
        note="IRE as documented — all 29 features, stratified random split",
    )
    report["arms"]["A_ire_as_documented"] = a

    # ---- B: the decisive probe -------------------------------------------
    without_device = [c for c in IRE_FEATURES if c not in DEVICE_COLUMNS]
    b, _, _ = run_arm(
        "B", df, without_device, temporal=False,
        note="the same, minus new_device_transaction — the leakage probe",
    )
    report["arms"]["B_without_device_flag"] = b

    # ---- B2: minus the look-ahead aggregates too --------------------------
    clean = [c for c in without_device if c not in LOOKAHEAD_COLUMNS]
    b2, _, _ = run_arm(
        "B2", df, clean, temporal=False,
        note="minus the device flag AND the whole-file per-customer aggregates",
    )
    report["arms"]["B2_without_device_or_lookahead"] = b2

    # ---- C: temporal split ------------------------------------------------
    c, _, _ = run_arm(
        "C", df, IRE_FEATURES, temporal=True,
        note="all 29 features, but split by time instead of at random",
    )
    report["arms"]["C_temporal_split"] = c

    # ---- D: our protocol on their data ------------------------------------
    _, ya_idx = train_test_split(
        np.arange(len(df)), test_size=0.2,
        stratify=df["is_fraud"].to_numpy(), random_state=42,
    )
    report["arms"]["D_our_protocol_their_data"] = our_protocol(
        "D", ya, pa, amounts_all[ya_idx], span_days * 0.2
    )

    # ---- audit ------------------------------------------------------------
    report["single_column_audit"] = single_column_audit(df, IRE_FEATURES)

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()

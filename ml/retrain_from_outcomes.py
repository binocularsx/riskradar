"""Retrain on this bank's own outcomes, and say honestly whether it is better (D89).

    python ml/retrain_from_outcomes.py                  # measure only
    python ml/retrain_from_outcomes.py --register       # also register the challenger, inactive

Plain English
-------------
The shipped model learned from the simulator. A bank's real teacher is its own
desk: every case an analyst closes as confirmed fraud or false positive is a
label about a payment this bank actually saw. This command learns from those.

1. **Labels.** A payment in a case closed CONFIRMED_FRAUD is fraud. A payment
   in a case closed FALSE_POSITIVE is not. A payment nobody alerted on, old
   enough that a customer would have reported it by now (``--label-delay-days``,
   default 30), is taken as not fraud. Payments of a customer with confirmed
   fraud close to that fraud but never alerted are left out: they may be fraud
   the system missed, and calling them clean would teach the model to miss it
   again.
2. **Inputs.** The feature snapshot stored with each decision (D7a), the exact
   numbers the model saw in production. No recomputation, so no train/serve
   skew by construction.
3. **Champion against challenger.** The newest quarter of the labelled period is
   held out. The challenger is trained on the older rows only; both it and the
   model in force (its stored probabilities) are scored on the held-out rows
   with the same measures: PR-AUC, ROC-AUC, and share of fraud caught within
   the alert budget.
4. **No automatic promotion.** ``--register`` records the challenger as an
   inactive model version with the comparison attached. An administrator
   promotes it through ``POST /v1/admin/models/promote``, and thresholds are
   re-derived afterwards (``POST /v1/admin/thresholds/derive``), because a new
   model scores on a new scale.

Label maturity (plan §7.1). Four different things are kept apart and only one
of them is used here as truth:

* what the **system** recommended — stored on the decision, never a label;
* what an **analyst proposed** — a submission, which may be pending or
  rejected, and is never training data;
* what a **lead adjudicated** — an approved submission, which is what this
  command learns from;
* what the **world** later said — a customer's report (D90), which arrives as
  an alert and becomes a label the same way, through an approval.

Known bias, stated: only alerted payments get a human label, so the positives
are the frauds the old system could see. Unalerted fraud that customers later
report reaches these labels only when the bank records it as a case.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

import psycopg  # noqa: E402

from riskradar.config import settings  # noqa: E402
from riskradar.features.spec import FEATURE_SPEC_VERSION, MODEL_FEATURE_NAMES  # noqa: E402

ARTIFACTS = REPO_ROOT / "ml" / "artifacts"

LABELS_SQL = """
    WITH closed AS (
        -- D93/D95: a label is a *lead's adjudication*, not one person's opinion.
        -- The outcome must have an approved submission behind it, so a pending
        -- or rejected proposal can never become training data (plan §7.1).
        SELECT a.transaction_id, c.outcome, c.subject_token
          FROM alerts a JOIN cases c ON c.id = a.case_id
         WHERE c.outcome IN ('CONFIRMED_FRAUD', 'FALSE_POSITIVE')
           AND EXISTS (SELECT 1 FROM fraud_submissions s
                        WHERE s.case_id = c.id AND s.state = 'APPROVED'
                          AND s.proposed_outcome = c.outcome)
    ),
    fraud_customers AS (
        SELECT c.subject_token, min(t.occurred_at) AS first_fraud, max(t.occurred_at) AS last_fraud
          FROM closed c JOIN transactions t ON t.id = c.transaction_id
         WHERE c.outcome = 'CONFIRMED_FRAUD'
         GROUP BY c.subject_token
    )
    SELECT t.id, t.occurred_at, d.features, d.p_fraud, d.model_version_id,
           CASE WHEN cl.outcome = 'CONFIRMED_FRAUD' THEN 1
                WHEN cl.outcome = 'FALSE_POSITIVE' THEN 0
                ELSE 0 END AS y,
           cl.outcome IS NOT NULL AS reviewed
      FROM decisions d
      JOIN transactions t ON t.id = d.transaction_id
      LEFT JOIN closed cl ON cl.transaction_id = t.id
      LEFT JOIN fraud_customers f ON f.subject_token = t.subject_token
     WHERE t.direction = 'OUTBOUND'
       AND d.rule_only_mode = false
       AND (cl.outcome IS NOT NULL OR t.occurred_at < %(mature)s)
       -- unalerted payments near a confirmed fraud: possibly missed fraud, not clean
       AND NOT (cl.outcome IS NULL AND f.subject_token IS NOT NULL
                AND t.occurred_at BETWEEN f.first_fraud - interval '7 days' AND f.last_fraud + interval '7 days')
     ORDER BY t.occurred_at
     LIMIT %(limit)s
"""


def load(conn, *, label_delay_days: float, limit: int) -> dict[str, np.ndarray]:
    mature = datetime.now(timezone.utc) - timedelta(days=label_delay_days)
    rows = conn.execute(LABELS_SQL, {"mature": mature, "limit": limit}).fetchall()
    X = np.full((len(rows), len(MODEL_FEATURE_NAMES)), np.nan)
    for i, r in enumerate(rows):
        f = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"] or "{}")
        for j, name in enumerate(MODEL_FEATURE_NAMES):
            v = f.get(name)
            if v is not None:
                X[i, j] = float(v)
    return {
        "X": np.nan_to_num(X, nan=0.0),
        "y": np.array([int(r["y"]) for r in rows]),
        "p": np.array([float(r["p_fraud"]) for r in rows]),
        "t": np.array([r["occurred_at"] for r in rows], dtype="datetime64[us]"),
        "model": np.array([r["model_version_id"] or 0 for r in rows]),
        "reviewed": np.array([bool(r["reviewed"]) for r in rows]),
    }


def compare(y: np.ndarray, champion: np.ndarray, challenger: np.ndarray, *, days: float,
            budget_per_day: float) -> dict:
    """Same rows, same measures, same budget for both models."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    k = max(1, int(round(budget_per_day * max(days, 1e-9))))

    def at_budget(score: np.ndarray) -> float:
        # Rank-based, ties broken by order: exactly k alerts, never more (D85).
        top = np.argsort(-score, kind="stable")[:k]
        return float(y[top].sum() / max(y.sum(), 1))

    def measures(score: np.ndarray) -> dict:
        both = len(np.unique(y)) == 2
        return {
            "pr_auc": float(average_precision_score(y, score)) if both else None,
            "roc_auc": float(roc_auc_score(y, score)) if both else None,
            "fraud_caught_at_budget": at_budget(score),
        }

    return {"rows": int(len(y)), "fraud": int(y.sum()), "alerts_at_budget": k,
            "champion": measures(champion), "challenger": measures(challenger)}


def verdict(result: dict, *, min_gain: float = 0.02) -> str:
    ch, cl = result["champion"], result["challenger"]
    if ch["pr_auc"] is None or cl["pr_auc"] is None:
        return "CANNOT_TELL"
    if cl["pr_auc"] >= ch["pr_auc"] + min_gain and cl["fraud_caught_at_budget"] >= ch["fraud_caught_at_budget"]:
        return "CHALLENGER_BETTER"
    if cl["pr_auc"] + min_gain < ch["pr_auc"]:
        return "CHAMPION_BETTER"
    return "NO_CLEAR_DIFFERENCE"


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrain on the desk's own outcomes (D89)")
    parser.add_argument("--label-delay-days", type=float, default=30.0)
    parser.add_argument("--holdout-share", type=float, default=0.25)
    parser.add_argument("--min-fraud", type=int, default=30, help="confirmed frauds needed in training")
    parser.add_argument("--min-test-fraud", type=int, default=10)
    parser.add_argument("--limit", type=int, default=2_000_000)
    parser.add_argument("--register", action="store_true", help="record the challenger as an inactive version")
    args = parser.parse_args()

    with psycopg.connect(settings().app_dsn, row_factory=psycopg.rows.dict_row) as conn:
        budget = int(conn.execute("SELECT value FROM app_config WHERE key = 'alert_budget_per_day'")
                     .fetchone()["value"])
        active = conn.execute("SELECT id, name, version FROM model_versions WHERE is_active").fetchone()
        data = load(conn, label_delay_days=args.label_delay_days, limit=args.limit)

    n, fraud, reviewed = len(data["y"]), int(data["y"].sum()), int(data["reviewed"].sum())
    print(f"labelled payments {n:,}: {fraud} confirmed fraud, {reviewed - fraud} confirmed false positive, "
          f"{n - reviewed:,} unalerted and older than {args.label_delay_days:g} days")
    order = np.argsort(data["t"], kind="stable")
    cut = int(len(order) * (1 - args.holdout_share))
    train, test = order[:cut], order[cut:]
    y_train, y_test = data["y"][train], data["y"][test]
    if y_train.sum() < args.min_fraud or y_test.sum() < args.min_test_fraud:
        print(f"REFUSING: {int(y_train.sum())} confirmed frauds to learn from and {int(y_test.sum())} to test on; "
              f"need {args.min_fraud} and {args.min_test_fraud}. Close more cases with an outcome, or lengthen "
              "the period. A model trained on a handful of labels would be noise with a version number.")
        raise SystemExit(2)

    from train import build_model

    challenger = build_model()
    challenger.fit(data["X"][train], y_train, times=data["t"][train])
    p_challenger = np.asarray(challenger.predict_proba(data["X"][test])[:, 1])
    # The champion's own probabilities, as stored when it scored these payments.
    champion_rows = data["model"][test] == (active["id"] if active else -1)
    days = float((data["t"][test].max() - data["t"][test].min()) / np.timedelta64(1, "D"))
    result = compare(y_test, data["p"][test], p_challenger, days=days, budget_per_day=budget)
    result["champion_scored_share"] = float(champion_rows.mean())
    result["verdict"] = verdict(result)
    print(json.dumps(result, indent=2))

    version = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M") + "-outcomes"
    report = {
        "trained_on": "this bank's case outcomes (D89)",
        "feature_spec": FEATURE_SPEC_VERSION,
        "feature_names": list(MODEL_FEATURE_NAMES),
        "feature_baseline": [float(v) for v in np.median(data["X"][train], axis=0)],
        "label_delay_days": args.label_delay_days,
        "champion": f"{active['name']}:{active['version']}" if active else None,
        "comparison": result,
    }
    (ARTIFACTS / f"evaluation-{version}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if not args.register:
        print("\n(measure only: pass --register to record the challenger as an inactive model version)")
        return
    # Refit on every labelled row for the version that would be promoted.
    final = build_model()
    final.fit(data["X"], data["y"], times=data["t"])
    # Not "riskradar-gbm-*": the demo builder registers the newest of those by name.
    artifact = ARTIFACTS / f"outcomes-gbm-{version}.joblib"
    joblib.dump(final, artifact)
    from train import register

    model_id = register(name="riskradar-gbm", version=version, artifact_path=artifact, metrics=report,
                        promote=False)
    print(f"\nregistered model_version {model_id} (inactive). Promote with POST /v1/admin/models/promote "
          f"and the comparison above, then re-derive thresholds.")


if __name__ == "__main__":
    main()

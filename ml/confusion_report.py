"""Precision, recall and false positives, counted row by row and incident by incident.

    python ml/confusion_report.py

The same time-ordered test as ``budget_menu.py`` (train on the oldest 75% of
the 150-day corpus, test on the newest 37 days, every fraud type present, the
shipped rules run by the real engine), reported as a confusion matrix for each
arm at each budget, so the headline figures can be read in the terms a data
scientist expects:

* **precision**: of the payments alerted, the share that were fraud;
* **recall** (payments): of the fraud payments, the share alerted;
* **recall** (incidents): of the fraud incidents, the share with at least one
  payment alerted; one alert opens the case, so this is the desk's recall;
* **false-positive rate**: of the legitimate payments, the share alerted;
* ROC-AUC and PR-AUC of the model's probability on the same rows.

Credits (the receiving side, D78) are measured separately in
``receiving-side.json``; they are added to the budget, not to these rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402

from evaluate_system import CREDIT_FEATURES  # noqa: E402
from offline_rules import RULE_CONFIGS, compute_signals, rule_masks  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES  # noqa: E402
from riskradar.model.calibrated import TimeSplitCalibratedBooster  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
OUT = REPO_ROOT / "ml" / "artifacts" / "confusion-report.json"
BUDGETS = (120, 75, 60)


def counts(flag: np.ndarray, fraud: np.ndarray, inc: np.ndarray, typ: np.ndarray, days: float) -> dict:
    tp = int((flag & fraud).sum())
    fp = int((flag & ~fraud).sum())
    fn = int((~flag & fraud).sum())
    tn = int((~flag & ~fraud).sum())
    incidents = {i for i in inc[fraud] if i}
    caught = {i for i in inc[flag & fraud] if i}
    per_type = {}
    for t in sorted({x for x in typ[fraud] if x}):
        m = fraud & (typ == t)
        ti = {i for i in inc[m] if i}
        per_type[t] = {
            "payment_recall": round(float((flag & m).sum()) / max(int(m.sum()), 1), 3),
            "incident_recall": round(len({i for i in inc[flag & m] if i}) / max(len(ti), 1), 3),
            "incidents": len(ti),
        }
    return {
        "alerts_per_day": round((tp + fp) / days, 1),
        "true_positives": tp, "false_positives": fp, "false_negatives": fn, "true_negatives": tn,
        "precision": round(tp / max(tp + fp, 1), 4),
        "payment_recall": round(tp / max(tp + fn, 1), 4),
        "incident_recall": round(len(caught) / max(len(incidents), 1), 4),
        "false_positive_rate": round(fp / max(fp + tn, 1), 6),
        "false_alerts_per_day": round(fp / days, 1),
        "legitimate_payments_per_false_alert": round((fp + tn) / max(fp, 1)),
        "per_type": per_type,
    }


def main() -> None:
    d = np.load(CORPUS, allow_pickle=True)
    X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
    keep = [j for j, n in enumerate(FEATURE_NAMES) if n not in CREDIT_FEATURES]
    meta = {k: d[k] for k in ("instrument", "channel", "ip_region", "has_beneficiary")}
    order = np.argsort(occ)
    X, y, typ, occ, inc = (a[order] for a in (X, y, typ, occ, inc))
    meta = {k: v[order] for k, v in meta.items()}
    cut = int(0.75 * len(y))
    days = (occ[-1] - occ[cut]).total_seconds() / 86400.0

    print(f"training on the oldest {cut:,} rows ...", flush=True)
    p = TimeSplitCalibratedBooster().fit(X[:cut][:, keep], y[:cut], times=occ[:cut]).predict_proba(X[cut:][:, keep])[:, 1]
    yt, tt, it = y[cut:], typ[cut:], inc[cut:]
    fraud = yt == 1
    print("running the rules ...", flush=True)
    rules, _ = rule_masks(compute_signals(X[cut:], {k: v[cut:] for k, v in meta.items()}, FEATURE_NAMES,
                                          dict(RULE_CONFIGS)))
    credit_per_day = json.loads((REPO_ROOT / "ml" / "artifacts" / "receiving-side.json").read_text())["chosen"]

    report = {
        "test": {"rows": int(len(yt)), "days": round(days, 1), "fraud_payments": int(fraud.sum()),
                 "legitimate_payments": int((~fraud).sum()), "fraud_share": round(float(fraud.mean()), 5),
                 "incidents": len({i for i in it[fraud] if i})},
        "model_ranking": {"roc_auc": round(float(roc_auc_score(yt, p)), 4),
                          "pr_auc": round(float(average_precision_score(yt, p)), 4),
                          "pr_auc_lift": round(float(average_precision_score(yt, p) / yt.mean()), 1)},
        "rules_only": counts(rules, fraud, it, tt, days),
        "credits_measured_separately": credit_per_day,
        "budgets": {},
    }
    for budget in BUDGETS:
        spare = int((budget - credit_per_day["alerts_per_day"]) * days - rules.sum())
        free = np.flatnonzero(~rules)
        ranked = free[np.argsort(-p[free], kind="stable")]
        model_flag = np.zeros(len(yt), bool)
        model_flag[ranked[:max(spare, 0)]] = True
        # The model alone, at the same number of payment alerts as the whole system.
        alone = np.zeros(len(yt), bool)
        alone[np.argsort(-p, kind="stable")[:int((rules | model_flag).sum())]] = True
        report["budgets"][budget] = {
            "system": counts(rules | model_flag, fraud, it, tt, days),
            "model_alone_same_volume": counts(alone, fraud, it, tt, days),
        }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["budgets"][75]["system"]
    print(json.dumps({k: v for k, v in s.items() if k != "per_type"}, indent=1))
    print(json.dumps(report["model_ranking"]), json.dumps(report["test"]))


if __name__ == "__main__":
    main()

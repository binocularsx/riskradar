"""Evaluation metrics.

D10c — **accuracy is banned.** At a 0.3% base rate, predicting "never fraud"
scores 99.7%, and a number that rewards doing nothing is worse than no number.

D24a — the headline recall figure is measured **per incident**, not per
transaction. Correlation means one alert is enough to open the case, and case
detail then puts the subject's whole timeline in front of the analyst (FR-024),
so catching 1 of an incident's 11 transactions catches the incident.
Transaction-level recall at the alert budget lands near 20% and is a misleading
denominator. Both are reported, always, with this explanation attached — so the
low number is volunteered rather than discovered by an examiner.
"""

from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score


def pr_auc(y_true: np.ndarray, p: np.ndarray) -> float:
    """Average precision. The right summary under heavy class imbalance."""
    return float(average_precision_score(y_true, p))


def roc_auc(y_true: np.ndarray, p: np.ndarray) -> float:
    """Reported for comparability only. ROC-AUC flatters imbalanced problems
    because the false-positive rate barely moves when negatives dominate."""
    return float(roc_auc_score(y_true, p))


def threshold_for_alert_budget(
    p: np.ndarray, *, budget_per_day: int, days: float
) -> float:
    """The probability at which alert volume equals the budget.

    D11d: thresholds are derived **backwards from an alert budget** — analysts
    multiplied by reviewable alerts per day — never from "80 sounds high". This
    is that arithmetic.
    """
    allowed = max(1, int(round(budget_per_day * days)))
    if allowed >= len(p):
        return 0.0
    return float(np.partition(p, -allowed)[-allowed])


def recall_at_budget(
    y_true: np.ndarray, p: np.ndarray, *, budget_per_day: int, days: float
) -> dict[str, float]:
    threshold = threshold_for_alert_budget(p, budget_per_day=budget_per_day, days=days)
    flagged = p >= threshold
    tp = int(np.sum(flagged & (y_true == 1)))
    positives = int(np.sum(y_true == 1))
    return {
        "threshold": threshold,
        "alerts": int(np.sum(flagged)),
        "true_positives": tp,
        "transaction_recall": round(tp / positives, 4) if positives else 0.0,
        "precision": round(tp / max(1, int(np.sum(flagged))), 4),
    }


def incident_recall_at_budget(
    y_true: np.ndarray,
    p: np.ndarray,
    incident_id: np.ndarray,
    *,
    budget_per_day: int,
    days: float,
) -> dict[str, float]:
    """The headline number (D24a).

    An incident counts as caught if **any** of its transactions is alerted,
    because that is operationally what happens: one alert opens the case and the
    analyst then sees the rest.
    """
    threshold = threshold_for_alert_budget(p, budget_per_day=budget_per_day, days=days)
    flagged = p >= threshold

    fraud_rows = y_true == 1
    incidents = {i for i in incident_id[fraud_rows] if i}
    caught = {i for i in incident_id[fraud_rows & flagged] if i}

    return {
        "threshold": threshold,
        "incidents": len(incidents),
        "incidents_caught": len(caught),
        "incident_recall": round(len(caught) / len(incidents), 4) if incidents else 0.0,
    }


def reliability_curve(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Predicted probability against observed frequency.

    A calibrated probability is what lets you predict alert volume at a
    threshold, and alert volume is what decides whether analysts drown (D11).
    Reporting the curve is how that claim is checked rather than asserted.
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[dict] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        n = int(np.sum(mask))
        if not n:
            continue
        out.append(
            {
                "bin_low": round(float(lo), 3),
                "bin_high": round(float(hi), 3),
                "n": n,
                "mean_predicted": round(float(np.mean(p[mask])), 5),
                "observed_rate": round(float(np.mean(y_true[mask])), 5),
            }
        )
    return out


def summarise(
    y_true: np.ndarray,
    p: np.ndarray,
    incident_id: np.ndarray,
    *,
    budget_per_day: int,
    days: float,
    label: str,
) -> dict:
    return {
        "label": label,
        "n": int(len(y_true)),
        "positives": int(np.sum(y_true == 1)),
        "base_rate_pct": round(100 * float(np.mean(y_true)), 4),
        "pr_auc": round(pr_auc(y_true, p), 4),
        "roc_auc": round(roc_auc(y_true, p), 4),
        "at_alert_budget": recall_at_budget(
            y_true, p, budget_per_day=budget_per_day, days=days
        ),
        "incident_level": incident_recall_at_budget(
            y_true, p, incident_id, budget_per_day=budget_per_day, days=days
        ),
        "reliability": reliability_curve(y_true, p),
        "note": (
            "Accuracy is deliberately absent (D10c). Transaction-level recall is "
            "a misleading denominator here — see incident_level, which is the "
            "headline figure (D24a)."
        ),
    }

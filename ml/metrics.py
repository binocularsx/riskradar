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


# ---------------------------------------------------------------------------
# Metrics a fraud desk asks for that a textbook does not
# ---------------------------------------------------------------------------


def value_weighted_recall(
    y_true: np.ndarray,
    p: np.ndarray,
    amount_minor: np.ndarray,
    *,
    budget_per_day: int,
    days: float,
) -> dict[str, float]:
    """What fraction of the **money** at risk did we catch?

    Transaction-count recall treats a NGN 900 airtime top-up and a NGN 4.2m
    transfer as the same event. A fraud desk is judged on losses, not on counts,
    so a model that catches many small frauds and misses the large ones can look
    good on recall and be worthless in practice.

    This is the number a Head of Fraud would actually ask for.
    """
    threshold = threshold_for_alert_budget(p, budget_per_day=budget_per_day, days=days)
    flagged = p >= threshold
    fraud = y_true == 1

    total_value = float(np.sum(amount_minor[fraud]))
    caught_value = float(np.sum(amount_minor[fraud & flagged]))

    return {
        "threshold": threshold,
        "fraud_value_minor": int(total_value),
        "caught_value_minor": int(caught_value),
        "value_recall": round(caught_value / total_value, 4) if total_value else 0.0,
        # Reported beside it deliberately: when count-recall is much higher than
        # value-recall, the model is catching the cheap fraud and missing the
        # expensive fraud, which is the failure mode that matters.
        "count_recall": round(
            float(np.sum(fraud & flagged)) / max(float(np.sum(fraud)), 1), 4
        ),
    }


def time_to_detection(
    y_true: np.ndarray,
    p: np.ndarray,
    incident_id: np.ndarray,
    amount_minor: np.ndarray,
    occurred_at: np.ndarray,
    *,
    budget_per_day: int,
    days: float,
) -> dict[str, float]:
    """How far into an incident do we catch it — and how much was still ahead?

    Catching a fan-out on its second transfer and catching it on its eleventh
    are both "detected" and are not remotely the same outcome. The first
    recovers most of the money; the second writes it off.

    Two figures come out of this:

    * **position** — which transaction of the incident first alerted (1 is best).
    * **value still preventable** — the share of the incident's value that had
      *not yet moved* at the moment of the first alert. This is the closest
      honest proxy for loss avoided, and it is the number that justifies the
      whole system economically.
    """
    threshold = threshold_for_alert_budget(p, budget_per_day=budget_per_day, days=days)
    flagged = p >= threshold
    fraud = y_true == 1

    positions: list[int] = []
    preventable: list[float] = []
    minutes: list[float] = []
    missed = 0

    for incident in {i for i in incident_id[fraud] if i}:
        rows = np.flatnonzero(fraud & (incident_id == incident))
        if rows.size == 0:
            continue
        # Chronological, because "how far in" is a question about time.
        rows = rows[np.argsort(occurred_at[rows])]

        hit = np.flatnonzero(flagged[rows])
        if hit.size == 0:
            missed += 1
            continue

        first = int(hit[0])
        positions.append(first + 1)

        total = float(np.sum(amount_minor[rows]))
        after = float(np.sum(amount_minor[rows[first:]]))
        preventable.append(after / total if total else 0.0)

        span = (occurred_at[rows[first]] - occurred_at[rows[0]])
        minutes.append(getattr(span, "total_seconds", lambda: 0.0)() / 60.0)

    if not positions:
        return {"detected_incidents": 0, "missed_incidents": missed}

    return {
        "detected_incidents": len(positions),
        "missed_incidents": missed,
        "median_position": float(np.median(positions)),
        "caught_on_first_transaction": round(
            float(np.mean(np.array(positions) == 1)), 4
        ),
        "median_minutes_into_incident": round(float(np.median(minutes)), 1),
        "median_value_still_preventable": round(float(np.median(preventable)), 4),
        "mean_value_still_preventable": round(float(np.mean(preventable)), 4),
    }


def false_positive_rate_by_segment(
    y_true: np.ndarray, p: np.ndarray, segment: np.ndarray,
    *, budget_per_day: int, days: float,
) -> list[dict]:
    """Who bears the cost of being wrong?

    A model that alerts disproportionately on USSD is alerting
    disproportionately on the customers least able to absorb the friction — in
    this market, the poorest ones. Reporting one global false-positive rate
    hides that completely.

    Not a fairness *guarantee*. It is the measurement that makes the question
    answerable, which is the least a system touching people's money should do.
    """
    threshold = threshold_for_alert_budget(p, budget_per_day=budget_per_day, days=days)
    flagged = p >= threshold
    out = []
    for value in sorted({str(s) for s in segment}):
        mask = segment.astype(str) == value
        legit = mask & (y_true == 0)
        n_legit = int(np.sum(legit))
        if not n_legit:
            continue
        out.append({
            "segment": value,
            "legitimate": n_legit,
            "false_positives": int(np.sum(legit & flagged)),
            "false_positive_rate": round(float(np.sum(legit & flagged)) / n_legit, 5),
        })
    return sorted(out, key=lambda r: -r["false_positive_rate"])

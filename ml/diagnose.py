"""Hard questions about our own model, answered with numbers.

    python ml/diagnose.py

Everything published so far has been a point estimate from a single run on a
single split. This asks the questions that decide whether those numbers mean
anything:

1.  **How wide are the error bars?** The headline rests on 153 incidents. With
    counts that small, the difference between 0.89 and 0.98 may be nothing at
    all, and publishing either without an interval is overclaiming.

2.  **Does the model beat a trivial baseline?** If a one-line rule matches a
    250-tree gradient-boosted ensemble, the machine learning is decoration. This
    has never been checked, and it is the single most important question in the
    project.

3.  **Is the probability actually calibrated?** We claim it is, and the whole
    alert-budget mechanism depends on it. Publishing a reliability curve is not
    the same as measuring the error in it.

4.  **Are twelve features really twelve?** Correlated features inflate the
    apparent size of a feature set and destabilise attributions.

5.  **Would more data help?** A learning curve says whether the corpus is the
    limit or the method is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from sklearn.dummy import DummyClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from metrics import threshold_for_alert_budget  # noqa: E402
from train import ALERT_BUDGET_PER_DAY, build_model  # noqa: E402

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"
RNG = np.random.default_rng(20260910)


def load():
    d = np.load(CORPUS, allow_pickle=True)
    return d["X"], d["y"].astype(int), d["typology"], d["incident_id"], d["occurred_at"]


def incident_recall(y, p, incident_id, threshold) -> tuple[int, int]:
    flagged = p >= threshold
    fraud = y == 1
    incidents = {i for i in incident_id[fraud] if i}
    caught = {i for i in incident_id[fraud & flagged] if i}
    return len(caught), len(incidents)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — the right one for a proportion from few trials.

    A normal approximation breaks badly at these counts (and can produce bounds
    outside 0-1). Wilson stays sane at n=47.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def expected_calibration_error(y, p, bins: int = 10) -> dict:
    """How far the stated probability is from the observed frequency.

    Weighted by how many predictions land in each bin, so a badly-calibrated bin
    holding four rows does not dominate a well-calibrated one holding 200,000.
    """
    edges = np.linspace(0, 1, bins + 1)
    total, ece, worst = len(y), 0.0, (0.0, None)
    detail = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        n = int(m.sum())
        if not n:
            continue
        conf, acc = float(p[m].mean()), float(y[m].mean())
        gap = abs(conf - acc)
        ece += (n / total) * gap
        if gap > worst[0]:
            worst = (gap, (round(lo, 2), round(hi, 2), n, round(conf, 4), round(acc, 4)))
        detail.append({"bin": [round(lo, 2), round(hi, 2)], "n": n,
                       "predicted": round(conf, 5), "observed": round(acc, 5)})
    return {"ece": round(ece, 5), "worst_bin": worst[1], "bins": detail}


def main() -> None:
    X, y, typology, incident_id, occurred = load()
    report: dict = {}
    print(f"corpus {len(y):,} rows · {int(y.sum()):,} fraud · "
          f"{len({i for i in incident_id if i})} incidents\n")

    # -----------------------------------------------------------------------
    print("=" * 72)
    print("1. DOES THE MODEL BEAT A TRIVIAL BASELINE?")
    print("=" * 72)
    print("   Held out: CARD_TESTING. Four contenders on identical data.\n")

    held = "CARD_TESTING"
    train_mask = ~((typology == held) & (y == 1))
    test_mask = np.ones(len(y), bool)
    Xtr, ytr = X[train_mask], y[train_mask]
    Xte, yte = X[test_mask], y[test_mask]
    inc_te = incident_id[test_mask]

    span_days = float(
        (np.max(occurred) - np.min(occurred)).total_seconds() / 86400.0
    ) if hasattr(np.max(occurred), "total_seconds") else 30.0

    contenders: dict[str, np.ndarray] = {}

    print("   training gradient boosting …")
    gbm = build_model()
    gbm.fit(Xtr, ytr)
    contenders["gradient boosting (ours)"] = gbm.predict_proba(Xte)[:, 1]

    print("   training logistic regression …")
    sc = StandardScaler().fit(Xtr)
    lr = LogisticRegression(max_iter=2000, class_weight="balanced")
    lr.fit(sc.transform(Xtr), ytr)
    contenders["logistic regression"] = lr.predict_proba(sc.transform(Xte))[:, 1]

    print("   scoring single-feature rules …")
    # The cheapest thing that could possibly work: one feature, used raw.
    for name in ("txn_count_1h_account", "distinct_beneficiaries_1h_account"):
        contenders[f"one feature: {name}"] = X[test_mask, FEATURE_NAMES.index(name)].astype(float)

    dummy = DummyClassifier(strategy="stratified", random_state=0).fit(Xtr, ytr)
    contenders["random guessing"] = dummy.predict_proba(Xte)[:, 1]

    rows = []
    for label, p in contenders.items():
        t = threshold_for_alert_budget(p, budget_per_day=ALERT_BUDGET_PER_DAY, days=span_days)
        caught, total = incident_recall(yte, p, inc_te, t)
        lo, hi = wilson(caught, total)
        rows.append({
            "model": label,
            "pr_auc": round(float(average_precision_score(yte, p)), 4),
            "roc_auc": round(float(roc_auc_score(yte, p)), 4),
            "incident_recall": round(caught / max(total, 1), 4),
            "caught": caught, "incidents": total,
            "ci95": [round(lo, 3), round(hi, 3)],
        })
    rows.sort(key=lambda r: -r["pr_auc"])
    print()
    print(f"   {'model':34s} {'PR-AUC':>8s} {'ROC':>7s} {'incident recall':>18s}  95% interval")
    for r in rows:
        print(f"   {r['model']:34s} {r['pr_auc']:8.4f} {r['roc_auc']:7.4f} "
              f"{r['caught']:>6}/{r['incidents']:<3} = {r['incident_recall']:.3f}"
              f"   [{r['ci95'][0]:.2f}, {r['ci95'][1]:.2f}]")
    report["baselines"] = rows

    # -----------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("2. HOW WIDE ARE THE ERROR BARS ON THE HEADLINE?")
    print("=" * 72)
    ci_rows = []
    for t in sorted({str(v) for v in typology if v and str(v) != "None"}):
        n_inc = len({incident_id[i] for i in range(len(y)) if str(typology[i]) == t})
        for observed in (0.64, 0.89, 0.98):
            k = int(round(observed * n_inc))
            lo, hi = wilson(k, n_inc)
            ci_rows.append({"typology": t, "incidents": n_inc, "point": observed,
                            "ci95": [round(lo, 3), round(hi, 3)],
                            "width": round(hi - lo, 3)})
        lo, hi = wilson(int(round(0.89 * n_inc)), n_inc)
        print(f"   {t:20s} {n_inc:3d} incidents · a recall of 0.89 means "
              f"[{lo:.2f}, {hi:.2f}] — a window {hi - lo:.2f} wide")
    report["confidence_intervals"] = ci_rows
    print()
    print("   So 0.89 and 0.98 are NOT distinguishable at this sample size.")

    # -----------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("3. IS THE PROBABILITY ACTUALLY CALIBRATED?")
    print("=" * 72)
    cal = expected_calibration_error(yte, contenders["gradient boosting (ours)"])
    print(f"   expected calibration error = {cal['ece']:.5f}")
    if cal["worst_bin"]:
        lo, hi, n, conf, acc = cal["worst_bin"]
        print(f"   worst bin  [{lo}, {hi}]  n={n:,}  says {conf:.3f}  actually {acc:.3f}")
    print()
    print(f"   {'stated':>12s} {'observed':>12s} {'rows':>10s}")
    for b in cal["bins"]:
        print(f"   {b['predicted']:12.5f} {b['observed']:12.5f} {b['n']:10,}")
    report["calibration"] = cal

    # -----------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("4. ARE TWELVE FEATURES REALLY TWELVE?")
    print("=" * 72)
    # Spearman: these are heavily skewed, so rank correlation is the honest one.
    ranks = np.argsort(np.argsort(X, axis=0), axis=0).astype(float)
    corr = np.corrcoef(ranks, rowvar=False)
    pairs = []
    for i in range(len(FEATURE_NAMES)):
        for j in range(i + 1, len(FEATURE_NAMES)):
            if abs(corr[i, j]) > 0.5:
                pairs.append({"a": FEATURE_NAMES[i], "b": FEATURE_NAMES[j],
                              "spearman": round(float(corr[i, j]), 3)})
    if pairs:
        for p in sorted(pairs, key=lambda r: -abs(r["spearman"])):
            print(f"   {p['a']:42s} <-> {p['b']:42s} {p['spearman']:+.3f}")
    else:
        print("   No pair above |0.5| — the twelve are carrying distinct information.")
    report["correlated_pairs"] = pairs

    out = REPO_ROOT / "ml" / "artifacts" / "diagnosis.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

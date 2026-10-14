"""Does each layer of this system earn its place? Six arms, one budget.

    python ml/evaluate_system.py

Why this was rewritten
----------------------
The first version compared two things: the model alone, and the model plus
rules. It reported a large gap and concluded the architecture was justified.

That comparison could never have shown the opposite. It never asked what the
**rules alone** would do, so "model plus rules beats model" was compatible with
the model contributing nothing whatsoever. And it never asked whether a simpler
model would do as well, so the choice of a 250-tree gradient-boosted ensemble
was never tested against anything.

A separate diagnostic then found that a plain logistic regression *beat* our
ensemble on the metric we publish, and that a single raw feature was
statistically indistinguishable from both. That made this rewrite unavoidable.

Six arms now run on identical data at an identical alert budget:

    rules only              no model at all
    one feature             txn_count_1h_account, raw, no model at all
    logistic regression     a linear model, alone
    gradient boosting       ours, alone
    rules + logistic
    rules + gradient boosting   what currently ships

Reading it:

* If **rules only** matches **rules + gradient boosting**, the model is
  decoration and should be cut.
* If **logistic regression** matches **gradient boosting**, ship the linear
  model — it is faster, its coefficients can be read aloud, and defending it is
  easier.
* If **one feature** matches everything, we have been overselling the whole
  thing and should say so.

Every figure carries a Wilson 95% interval, because the corpus holds 153
incidents and differences smaller than the intervals are not differences.

Nothing here breaks the generator/detector wall (D10a): it reads the detection
stack and the corpus labels, never a generator parameter.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from dataset import holdout_typology_split  # noqa: E402
from evaluate import load_or_build  # noqa: E402
from metrics import threshold_for_alert_budget  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION  # noqa: E402
from riskradar.policy.engine import Thresholds, apply as apply_policy  # noqa: E402
from train import ALERT_BUDGET_PER_DAY, TYPOLOGIES, build_model  # noqa: E402

# D77. The features read from non-payment events. ``--without-events`` removes
# them and the rule built on them, so the same corpus measures the system as it
# was before WP-02.
EVENT_FEATURES = ("failed_logins_1h_subject", "device_bound_hours", "credential_changed_hours",
                  "sim_changed_hours", "payee_added_minutes")
EVENT_RULES = ("ACCOUNT_TAKEOVER_SEQUENCE", "SIM_SWAP_TRANSFER")
# D78. The receiving-side features; ``--without-credits`` removes them.
CREDIT_FEATURES = ("credits_24h_account", "distinct_remitters_24h_account",
                   "inbound_count_ratio_24h_vs_daily_mean_30d", "minutes_since_last_credit",
                   "pass_through_ratio_24h")

# The cheapest thing that could possibly work, used as the floor everything else
# has to clear. Chosen before seeing any result: it is the single feature with
# the most obvious operational meaning.
BASELINE_FEATURE = "txn_count_1h_account"

# The seeded ruleset (scripts/seed.py), kept in ml/offline_rules.py so every
# offline script measures the same rules (D82). Re-exported here for callers.
from offline_rules import RULE_CONFIGS, compute_signals  # noqa: E402,F401


# A probability the model can never reach, used to switch the model layer off
# entirely so the rules can be measured on their own.
MODEL_DISABLED = 2.0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion from few trials.

    At 47 incidents the normal approximation is badly wrong and can produce
    bounds outside 0 to 1. Wilson stays sensible.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def decide(p: np.ndarray, signals: list, thresholds: Thresholds) -> np.ndarray:
    """Apply the policy layer over pre-computed signals. Cheap, so run per arm."""
    actionable = np.zeros(len(p), dtype=bool)
    for i in range(len(p)):
        actionable[i] = apply_policy(float(p[i]), signals[i], thresholds).actionable
    return actionable


def score(y: np.ndarray, flagged: np.ndarray, incident_id: np.ndarray, label: str) -> dict:
    fraud = y == 1
    incidents = {i for i in incident_id[fraud] if i}
    caught = {i for i in incident_id[fraud & flagged] if i}
    k, n = len(caught), len(incidents)
    lo, hi = wilson(k, n)
    return {
        "arm": label,
        "incidents": n,
        "incidents_caught": k,
        "incident_recall": round(k / n, 4) if n else 0.0,
        "ci95": [round(lo, 3), round(hi, 3)],
        "alerts": int(flagged.sum()),
    }


def _size_limitation(counts: list[int]) -> str:
    """The sample-size caveat, from the incidents actually counted.

    It used to be a fixed sentence written for a corpus of about 50 incidents a
    typology, and stayed in every report after the corpus grew fourfold.
    """
    lo, hi = min(counts), max(counts)
    width = 2 * 1.96 * math.sqrt(0.25 / lo)  # widest Wilson-like interval, at p = 0.5
    return (f"{lo} to {hi} incidents per typology. 95% intervals are up to about "
            f"{width:.2f} wide, so arms whose intervals overlap are not "
            "distinguishable on this corpus.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="ml/data/corpus.jsonl")
    parser.add_argument("--out", default="ml/artifacts/system-evaluation.json")
    parser.add_argument("--budget", type=int, default=ALERT_BUDGET_PER_DAY, help="alerts a day (D76: 75)")
    parser.add_argument("--without-events", action="store_true",
                        help="D77 ablation: drop the event features and the rule built on them")
    parser.add_argument("--with-credit-features", action="store_true",
                        help="D78 rejected option, kept measurable: give the model the receiving-side features")
    args = parser.parse_args()
    budget = args.budget

    corpus = load_or_build(REPO_ROOT / args.corpus, rebuild=False)
    # D79: rules read every feature (the second leg reads the receiving side);
    # the model is given only its own inputs (D78b). Two matrices, one row order.
    X_rules, rule_names = corpus.X, list(FEATURE_NAMES)
    names = list(FEATURE_NAMES)
    configs = dict(RULE_CONFIGS)
    if args.without_events:
        keep = [j for j, n in enumerate(names) if n not in EVENT_FEATURES]
        corpus.X = corpus.X[:, keep]
        names = [names[j] for j in keep]
        configs = {k: v for k, v in configs.items() if k not in EVENT_RULES}
        X_rules = corpus.X
        rule_names = list(names)
        print(f"  without events: {len(names)} features, rules {sorted(configs)}")
    if not args.with_credit_features:
        # D78: as shipped, the model is not given the receiving-side features.
        keep = [j for j, n in enumerate(names) if n not in CREDIT_FEATURES]
        corpus.X = corpus.X[:, keep]
        names = [names[j] for j in keep]
        print(f"  model features: {len(names)} (receiving-side features left to the rules)")
    span_days = (max(corpus.occurred_at) - min(corpus.occurred_at)).total_seconds() / 86400.0

    # D82: the transaction's own instrument, channel and region travel with the
    # cached matrix now, so the rules read what the live path reads.
    meta = {"instrument": corpus.instrument, "channel": corpus.channel, "ip_region": corpus.ip_region,
            "has_beneficiary": corpus.has_beneficiary}
    baseline_col = names.index(BASELINE_FEATURE)

    results: dict = {}
    for held_out in TYPOLOGIES:
        print(f"\n=== holding out {held_out} ===")
        train_idx, test_idx = holdout_typology_split(corpus, held_out)
        Xtr, ytr = corpus.X[train_idx], corpus.y[train_idx]
        Xte, y = corpus.X[test_idx], corpus.y[test_idx]
        ids = corpus.incident_id[test_idx]
        test_days = max(span_days * 0.25, 1.0)

        print("   fitting gradient boosting …")
        gbm = build_model()
        gbm.fit(Xtr, ytr, times=corpus.occurred_at[train_idx])
        p_gbm = gbm.predict_proba(Xte)[:, 1]

        print("   fitting logistic regression …")
        scaler = StandardScaler().fit(Xtr)
        lr = LogisticRegression(max_iter=2000, class_weight="balanced")
        lr.fit(scaler.transform(Xtr), ytr)
        p_lr = lr.predict_proba(scaler.transform(Xte))[:, 1]

        # The trivial floor. Not a probability, just a count used as a score —
        # which is the point: no model, no fitting, no training data at all.
        p_one = Xte[:, baseline_col].astype(float)

        print("   evaluating rules (once, reused by every arm) …")
        signals = compute_signals(X_rules[test_idx], {k: v[test_idx] for k, v in meta.items()}, rule_names, configs)

        arms: list[dict] = []

        # --- model-free arms ------------------------------------------------
        off = Thresholds(id=0, version=0, p_monitor=MODEL_DISABLED,
                         p_review=MODEL_DISABLED, p_hold=MODEL_DISABLED,
                         alert_min_level="MEDIUM")
        arms.append(score(y, decide(np.zeros(len(y)), signals, off), ids, "rules only"))

        t_one = threshold_for_alert_budget(p_one, budget_per_day=budget,
                                           days=test_days)
        arms.append(score(y, p_one >= t_one, ids, f"one feature ({BASELINE_FEATURE})"))

        # --- model-only arms ------------------------------------------------
        thresholds_by_arm = {}
        for label, p in (("logistic regression", p_lr), ("gradient boosting (ours)", p_gbm)):
            t = threshold_for_alert_budget(p, budget_per_day=budget,
                                           days=test_days)
            thresholds_by_arm[label] = t
            arms.append(score(y, p >= t, ids, label))

        # --- combined arms --------------------------------------------------
        for label, p in (("logistic regression", p_lr), ("gradient boosting (ours)", p_gbm)):
            t = thresholds_by_arm[label]
            th = Thresholds(id=0, version=0, p_monitor=float(t),
                            p_review=float(np.quantile(p, 0.999)),
                            p_hold=float(np.quantile(p, 0.9999)),
                            alert_min_level="MEDIUM")
            arms.append(score(y, decide(p, signals, th), ids, f"rules + {label}"))

        results[held_out] = arms
        print()
        for a in arms:
            print(f"   {a['arm']:34s} {a['incidents_caught']:>3}/{a['incidents']:<3} "
                  f"= {a['incident_recall']:.3f}  [{a['ci95'][0]:.2f}, {a['ci95'][1]:.2f}]"
                  f"   {a['alerts']:>7,} alerts")

    # ---- means across the three held-out typologies ------------------------
    arm_names = [a["arm"] for a in next(iter(results.values()))]
    means = {
        name: round(float(np.mean([
            next(a["incident_recall"] for a in arms if a["arm"] == name)
            for arms in results.values()
        ])), 4)
        for name in arm_names
    }

    # Comparing the means against a fixed tolerance was the first version of this
    # and it was wrong: it ignored the very intervals computed above. With 47-57
    # incidents per typology those intervals are ~0.17 wide, so a gap of 0.08
    # between two means can easily be nothing. Two arms count as *distinguishable*
    # only where their intervals fail to overlap on at least one typology.
    ships = "rules + gradient boosting (ours)"

    def separates(better: str, worse: str) -> list[str]:
        """Typologies where `better` beats `worse` with non-overlapping intervals."""
        wins = []
        for t, arms in results.items():
            b = next(a for a in arms if a["arm"] == better)
            w = next(a for a in arms if a["arm"] == worse)
            if b["ci95"][0] > w["ci95"][1]:
                wins.append(t)
        return wins

    verdict = []

    model_wins = separates(ships, "rules only")
    if model_wins:
        verdict.append(
            "The model earns its place, but on "
            f"{len(model_wins)} of {len(results)} typologies only: {', '.join(model_wins)}. "
            "Everywhere else the rules alone match the full system inside the "
            "measurement error, so the architecture is justified by those "
            f"{len(model_wins)} rather than by all {len(results)}."
        )
    else:
        verdict.append(
            "The rules alone match the full system on every typology, within the "
            "intervals. Nothing here shows the model earning its place."
        )

    if not separates(ships, "rules + logistic regression"):
        verdict.append(
            "Gradient boosting is NOT distinguishable from logistic regression "
            "inside the system on any typology. Our choice of a 250-tree ensemble "
            "over a linear model is currently unsupported by evidence; the linear "
            "model is faster and its coefficients can be read aloud."
        )

    one = f"one feature ({BASELINE_FEATURE})"
    if means[one] > means["gradient boosting (ours)"]:
        verdict.append(
            f"Our model ALONE ({means['gradient boosting (ours)']:.3f}) scores below "
            f"a single raw feature ({means[one]:.3f}) and below the rules alone "
            f"({means['rules only']:.3f}). The model is only useful in combination; "
            "it must never be described as the system's engine."
        )

    report = {
        "alert_budget_per_day": budget,
        "feature_spec": ("without event features (D77 ablation)" if args.without_events
                         else f"{FEATURE_SPEC_VERSION} with receiving-side features in the model (D78 rejected)"
                         if args.with_credit_features else FEATURE_SPEC_VERSION),
        "features": names,
        "rules": sorted(configs),
        "incidents_per_typology": {t: arms[0]["incidents"] for t, arms in results.items()},
        "per_typology": results,
        "mean_incident_recall": means,
        "verdict": verdict,
        "limitations": [
            "Rule list membership (sanctioned, known mule, allowlist) is empty "
            "offline, so the two OVERRIDE rules and one SUPPRESS rule contribute "
            "nothing here. In production they would.",
            _size_limitation([arms[0]["incidents"] for arms in results.values()]),
        ],
    }

    out = REPO_ROOT / args.out
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"MEAN INCIDENT RECALL ACROSS THE {len(results)} HELD-OUT TYPOLOGIES")
    print("=" * 72)
    for name in arm_names:
        bar = "#" * int(round(means[name] * 40))
        print(f"   {name:34s} {means[name]:.3f}  {bar}")
    print()
    for line in verdict:
        print(f"   >>> {line}")
    print(f"\n   report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

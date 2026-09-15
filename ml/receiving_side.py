"""The receiving side: what the fan-in rule catches on credits, and what it costs (D78).

    python ml/receiving_side.py

A credit is judged by rules, not by the model (no model has learned incoming
fraud), so the question here is the one a rule answers: of the mule rings in
the corpus, how many had at least one credit flagged before the money fanned
out, and how many legitimate credits a day did the desk pay for that.

Every credit in the corpus is scored with the shared feature package, history
included (payments, credits and events), exactly as the worker scores one.
Variants of the rule's two settings are measured side by side; the chosen one
is the one seeded. Nothing is trained, so nothing is held out: the rule reads
features only.

Measured after a 30-day warm-up. In the corpus's first month no account has a
credit history, so every trader's first busy day reads as "unlike normal" and
the rule fired 174 times a day at precision 0.02. A bank always has that
history; the warm-up measures the rule as a bank would run it, and says what a
new deployment needs before trusting it (D78).
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from riskradar.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.jsonl"
CACHE = REPO_ROOT / "ml" / "data" / "corpus.credits.features.npz"
WARM_UP_DAYS = 30
CHOSEN = (3, 5.0)  # min_remitters, min_count_ratio: as seeded


def load() -> dict:
    if CACHE.exists():
        blob = np.load(CACHE, allow_pickle=True)
        if str(blob["spec_version"]) == FEATURE_SPEC_VERSION:
            print(f"  using cached credit features ({CACHE.name})")
            return {k: blob[k] for k in ("X", "y", "typology", "incident_id", "occurred_at")}
    from dataset import load_corpus

    c = load_corpus(CORPUS, target="credits")
    np.savez_compressed(CACHE, X=c.X, y=c.y, typology=c.typology, incident_id=c.incident_id,
                        occurred_at=c.occurred_at, spec_version=FEATURE_SPEC_VERSION)
    return {"X": c.X, "y": c.y, "typology": c.typology, "incident_id": c.incident_id, "occurred_at": c.occurred_at}


def main() -> None:
    d = load()
    X, y, inc, occ = d["X"], d["y"].astype(int), d["incident_id"], d["occurred_at"]
    measured_from = min(occ) + timedelta(days=WARM_UP_DAYS)
    keep = np.array([t >= measured_from for t in occ])
    X, y, inc, occ = X[keep], y[keep], inc[keep], occ[keep]
    days = (max(occ) - measured_from).total_seconds() / 86400.0
    col = lambda name: X[:, FEATURE_NAMES.index(name)]  # noqa: E731
    senders, ratio = col("distinct_remitters_24h_account"), col("inbound_count_ratio_24h_vs_daily_mean_30d")
    rings = {i for i in inc[y == 1] if i}

    rows = []
    print(f"after a {WARM_UP_DAYS}-day warm-up: {len(y):,} credits over {days:.1f} days; {len(rings)} mule rings with receipts; "
          f"{int(y.sum()):,} fraudulent credits\n")
    print(f"{'min senders':>12}{'min ratio':>10}{'alerts/day':>11}{'false/day':>10}{'precision':>10}{'rings caught':>14}")
    for min_senders in (2, 3, 4, 5, 6):
        for min_ratio in (3.0, 5.0, 10.0):
            flag = (senders >= min_senders) & (ratio >= min_ratio)
            caught = {i for i in inc[flag & (y == 1)] if i}
            row = {
                "min_remitters": min_senders, "min_count_ratio": min_ratio,
                "alerts_per_day": round(float(flag.sum()) / days, 2),
                "false_alerts_per_day": round(float((flag & (y == 0)).sum()) / days, 2),
                "precision": round(float((flag & (y == 1)).sum()) / max(int(flag.sum()), 1), 3),
                "rings_caught": len(caught), "ring_recall": round(len(caught) / max(len(rings), 1), 3),
                "chosen": (min_senders, min_ratio) == CHOSEN,
            }
            rows.append(row)
            print(f"{min_senders:>12}{min_ratio:>10}{row['alerts_per_day']:>11}{row['false_alerts_per_day']:>10}"
                  f"{row['precision']:>10}{row['rings_caught']:>7}/{len(rings)} {row['ring_recall']:.3f}")

    out = REPO_ROOT / "ml" / "artifacts" / "receiving-side.json"
    chosen = next(r for r in rows if r["chosen"])
    out.write_text(json.dumps({"warm_up_days": WARM_UP_DAYS, "credits": int(len(y)), "days": round(days, 1),
                               "mule_rings_with_receipts": len(rings), "fraudulent_credits": int(y.sum()),
                               "chosen": chosen, "variants": rows}, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()

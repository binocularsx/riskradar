"""Point the generator-switch detector at our own corpus.

    python ml/audit_own_corpus.py

Why this exists
---------------
We built ``free exclusion at full recall`` to catch a defect in somebody else's
dataset: a column that discards a slice of legitimate traffic while keeping
100% of fraud is not describing behaviour, it is the switch the generator
flipped when it decided to write a fraud row.

We have never run it on our own data, and we have an obvious reason to worry.
Our account-takeover typology is a state machine that goes *compromise → new
device → reconnaissance → burst*. If it sets a new device on **every** takeover
and legitimate customers change device rarely, then
``device_is_new_to_subject`` is a switch in our corpus for exactly the same
reason it was one in theirs — and we would have no standing to criticise
anybody.

D10a forbids detection code from reading a generator *parameter*. It does not
by itself prevent a feature from being perfectly determined by generator
behaviour. That is a different failure and this is the test for it.

The audit is run per typology as well as overall, because a switch can hide in
the pooled numbers: a feature can look honest across 150 incidents while being
perfectly determined within one typology.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sklearn.metrics import roc_auc_score  # noqa: E402

from riskradar.features.spec import FEATURE_NAMES  # noqa: E402

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"

# Above this share of legitimate traffic discarded at full recall, a column is
# not a behavioural signal. Chosen to match the threshold used against the
# Electric Sheep dataset, where new_device_transaction scored 18.6%.
SWITCH_THRESHOLD = 0.05


def audit(X: np.ndarray, y: np.ndarray, label: str) -> list[dict]:
    n_pos, n_neg = int(np.sum(y == 1)), int(np.sum(y == 0))
    if n_pos == 0 or n_neg == 0:
        return []

    rows = []
    for i, name in enumerate(FEATURE_NAMES):
        v = X[:, i].astype(float)
        if np.std(v) == 0:
            rows.append({"feature": name, "auc": None, "free_exclusion": None,
                         "verdict": "constant"})
            continue

        auc = float(roc_auc_score(y, v))
        fraud_v = v[y == 1]
        lo, hi = float(fraud_v.min()), float(fraud_v.max())
        below = float(np.sum((y == 0) & (v < lo)))
        above = float(np.sum((y == 0) & (v > hi)))
        free = max(below, above) / n_neg

        verdict = ("GENERATOR SWITCH" if free > SWITCH_THRESHOLD else
                   "very strong" if max(auc, 1 - auc) > 0.75 else
                   "useful" if max(auc, 1 - auc) > 0.55 else "weak")
        rows.append({"feature": name, "auc": round(auc, 4),
                     "free_exclusion": round(free, 4), "verdict": verdict})

    rows.sort(key=lambda r: -(r["free_exclusion"] or 0))

    print(f"\n=== {label} ===")
    print(f"    {n_pos:,} fraud · {n_neg:,} legitimate "
          f"· base rate {100 * n_pos / (n_pos + n_neg):.3f}%")
    for r in rows[:6]:
        fe = "n/a" if r["free_exclusion"] is None else f"{r['free_exclusion']:6.1%}"
        auc = "n/a" if r["auc"] is None else f"{r['auc']:.4f}"
        flag = "  <-- " + r["verdict"] if r["verdict"] == "GENERATOR SWITCH" else ""
        print(f"    {r['feature']:44s} auc {auc}  discards {fe} of legit{flag}")
    return rows


def main() -> None:
    if not CORPUS.exists():
        raise SystemExit(f"missing {CORPUS} — run the simulator corpus build first")

    d = np.load(CORPUS, allow_pickle=True)
    X, y = d["X"], d["y"].astype(int)
    typology = d["typology"]

    print(f"corpus: {len(y):,} rows, {X.shape[1]} features")

    report = {"overall": audit(X, y, "ALL FRAUD POOLED")}

    # The important part. A switch can hide in the pooled numbers.
    report["per_typology"] = {}
    for t in sorted({str(v) for v in typology if v and str(v) != "None"}):
        # Legitimate rows plus only this typology's fraud, which is how the
        # held-out evaluation actually poses the question.
        mask = (typology == t) | (y == 0)
        report["per_typology"][t] = audit(X[mask], y[mask], f"typology: {t}")

    switches = []
    for scope, rows in [("overall", report["overall"])] + list(report["per_typology"].items()):
        for r in rows:
            if r["verdict"] == "GENERATOR SWITCH":
                switches.append({"scope": scope, **r})

    print("\n" + "=" * 68)
    if switches:
        print("FAIL — generator switches found. These features are determined by")
        print("       the simulator rather than learned from behaviour:")
        for s in switches:
            print(f"       {s['scope']:20s} {s['feature']:44s} {s['free_exclusion']:.1%}")
        print("\n       The simulator must be changed so the behaviour is a tendency")
        print("       rather than a rule, and the corpus rebuilt.")
    else:
        print("PASS — no feature discards more than "
              f"{SWITCH_THRESHOLD:.0%} of legitimate traffic at full recall,")
        print("       overall or within any single typology. Every feature overlaps")
        print("       real behaviour, which is what a behavioural signal must do.")
    print("=" * 68)

    report["switches_found"] = switches
    report["passed"] = not switches
    out = REPO_ROOT / "ml" / "artifacts" / "own-corpus-audit.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")

    raise SystemExit(0 if not switches else 1)


if __name__ == "__main__":
    main()

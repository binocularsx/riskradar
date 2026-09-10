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

from riskradar.features.spec import FEATURE_NAMES, NEVER_SEEN_DAYS  # noqa: E402

NEVER_SEEN = NEVER_SEEN_DAYS

CORPUS = REPO_ROOT / "ml" / "data" / "corpus.features.npz"

# Two thresholds, because "how much overlap is enough" is a judgement and
# pretending otherwise would be its own dishonesty.
#
# FAIL is calibrated against the case we know is broken: in the Electric Sheep
# dataset, new_device_transaction discarded 18.6% of legitimate traffic at full
# recall *and* the model achieved nothing beyond that cut — its precision was
# exactly the base rate of whatever survived. That is a column doing the entire
# job, and it is what must never appear here.
#
# WARN is where a real distributional difference starts being worth explaining.
# A fan-out that never sends ₦200 is a fact about fan-outs, not a leak. Features
# in this band are reported in full and have to be justified in writing; they do
# not stop a build.
#
# Tightening FAIL until the simulator passes would be fitting the data to the
# metric, which is precisely the failure this file exists to catch.
WARN_THRESHOLD = 0.05
FAIL_THRESHOLD = 0.15


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

        # A correction to the first version of this audit.
        #
        # Card testing sets no beneficiary, because card payments do not have
        # one. Every beneficiary feature therefore sits at the "never seen"
        # sentinel for all of its fraud, while most legitimate traffic is
        # transfers and has a real value — so the feature appeared to discard
        # 58% of normal traffic at full recall. That measures the *instrument*,
        # not the generator, and calling it a switch was wrong.
        #
        # Where a feature is undefined for a whole typology it is reported as
        # structural and excluded from the pass/fail gate, rather than being
        # silently dropped.
        structural = bool(np.mean(fraud_v >= NEVER_SEEN) >= 0.95)

        lo, hi = float(fraud_v.min()), float(fraud_v.max())
        below = float(np.sum((y == 0) & (v < lo)))
        above = float(np.sum((y == 0) & (v > hi)))
        free = max(below, above) / n_neg

        verdict = ("not applicable" if structural else
                   "GENERATOR SWITCH" if free > FAIL_THRESHOLD else
                   "explain this" if free > WARN_THRESHOLD else
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
        flag = (("  <-- " + r["verdict"])
                if r["verdict"] in ("GENERATOR SWITCH", "not applicable", "explain this")
                else "")
        print(f"    {r['feature']:44s} auc {auc}  discards {fe} of legit{flag}")
    return rows


def load_instruments(expected_rows: int) -> np.ndarray:
    """Read the instrument column straight from the corpus, in row order.

    Kept out of the cached feature matrix on purpose: the instrument is not a
    feature and must never become one. It is read here only so the audit can
    compare a card fraud against card traffic.
    """
    import json

    path = REPO_ROOT / "ml" / "data" / "corpus.jsonl"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    values = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            values.append(json.loads(line)["instrument"])
    if len(values) != expected_rows:
        raise SystemExit(
            f"corpus has {len(values):,} rows but the feature cache has "
            f"{expected_rows:,} — rebuild the cache before auditing"
        )
    return np.array(values)


def main() -> None:
    if not CORPUS.exists():
        raise SystemExit(f"missing {CORPUS} — run the simulator corpus build first")

    d = np.load(CORPUS, allow_pickle=True)
    X, y = d["X"], d["y"].astype(int)
    typology = d["typology"]

    print(f"corpus: {len(y):,} rows, {X.shape[1]} features")

    report = {"overall": audit(X, y, "ALL FRAUD POOLED")}

    # The important part. A switch can hide in the pooled numbers.
    # Compare like with like.
    #
    # The first version of this audit compared each typology's fraud against
    # *all* legitimate traffic. Card testing is entirely card payments, and card
    # payments have no beneficiary account at all — so every beneficiary feature
    # appeared to discard half of normal traffic at full recall. That was
    # measuring the instrument, not the generator, and three of the eight
    # "switches" it originally reported were this mistake.
    #
    # Each typology's fraud is now compared against legitimate transactions of
    # the same instrument. A card fraud is judged against card traffic.
    instrument = load_instruments(len(y))

    report["per_typology"] = {}
    for t in sorted({str(v) for v in typology if v and str(v) != "None"}):
        fraud_mask = typology == t
        instruments = sorted({str(v) for v in instrument[fraud_mask]})
        comparable = np.isin(instrument, instruments)
        mask = fraud_mask | ((y == 0) & comparable)
        label = f"typology: {t}  (vs legitimate {', '.join(instruments).lower()})"
        report["per_typology"][t] = audit(X[mask], y[mask], label)

    switches, warnings = [], []
    for scope, rows in [("overall", report["overall"])] + list(report["per_typology"].items()):
        for r in rows:
            if r["verdict"] == "GENERATOR SWITCH":
                switches.append({"scope": scope, **r})
            elif r["verdict"] == "explain this":
                warnings.append({"scope": scope, **r})

    print("\n" + "=" * 68)
    if switches:
        print("FAIL — generator switches found. These features are determined by")
        print("       the simulator rather than learned from behaviour:")
        for s in switches:
            print(f"       {s['scope']:20s} {s['feature']:44s} {s['free_exclusion']:.1%}")
        print("\n       The simulator must be changed so the behaviour is a tendency")
        print("       rather than a rule, and the corpus rebuilt.")
    else:
        print(f"PASS — no feature discards more than {FAIL_THRESHOLD:.0%} of legitimate")
        print("       traffic at full recall, overall or within any typology.")
        print("       Every feature overlaps real behaviour, which is what a")
        print("       behavioural signal has to do.")

    if warnings:
        print()
        print(f"       {len(warnings)} feature(s) between {WARN_THRESHOLD:.0%} and "
              f"{FAIL_THRESHOLD:.0%} — real distributional")
        print("       differences that must be justified in writing, not ignored:")
        for w in warnings:
            print(f"         {w['scope']:20s} {w['feature']:44s} {w['free_exclusion']:.1%}")
    print("=" * 68)

    report["switches_found"] = switches
    report["warnings"] = warnings
    report["thresholds"] = {"warn": WARN_THRESHOLD, "fail": FAIL_THRESHOLD}
    report["passed"] = not switches
    out = REPO_ROOT / "ml" / "artifacts" / "own-corpus-audit.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")

    raise SystemExit(0 if not switches else 1)


if __name__ == "__main__":
    main()

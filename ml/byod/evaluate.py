"""Measure the system on somebody else's transactions.

Plain English
-------------
Every transaction in the file is measured by the same feature package the live
worker calls, checked by the same rules, and decided by the same policy, with
the desk held to an alert budget. Then the alerts are compared with the file's
fraud labels.

Several arms run on identical rows at an identical budget, so the report can
say *which layer* earned what:

* **rules only** — no model at all.
* **shipped model + rules** — the model trained on our simulated bank, as it
  ships, never shown this data. The honest answer to "what if we just switched
  it on?".
* **retrained model + rules** — the same recipe trained on the older part of
  this dataset and tested on the newer part. What a bank would run after a
  month of its own labelled outcomes.
* **retrained with the dataset's own columns** — adds the file's extra numeric
  columns. A ceiling, not something that ships: those columns exist in this
  file, not in a bank's feed.
* **anomaly score + rules** — an isolation forest that never sees a label. The
  only arm with any claim on fraud *nobody has labelled yet*.
* **amount alone** — the floor everything must clear.

When the file says which kind of fraud each row is, the **unseen-type test**
retrains without one type at a time and asks whether it is still caught: the
question "would this catch a fraud type we have not seen?" asked directly.

Split by time, always. A random split lets the model study next month's fraud
before being tested on it, which is the commonest way a fraud result is made
unrepeatable.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from riskradar.features import PandasHistorySource, TxView, compute_features, to_vector
from riskradar.features.spec import FEATURE_NAMES, FEATURE_SPEC_VERSION, MODEL_FEATURE_NAMES
from riskradar.policy.engine import Thresholds, apply as apply_policy
from riskradar.rules.engine import RuleContext, Signal, evaluate as evaluate_rules

from .canonical import to_rows, utc

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_COLUMNS = [FEATURE_NAMES.index(n) for n in MODEL_FEATURE_NAMES]

# The shipped operating point: 75 alerts a day (D76) over the demo bank's
# 13,986 transactions a day (D78f derivation) is 0.536% of traffic. A foreign
# dataset of a different size is held to the same *share* by default.
SHIPPED_ALERT_RATE = 75 / 13_986
MIN_TRAIN_FRAUD_ROWS = 30
MIN_TYPE_INCIDENTS = 10


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------------------
# Features and rules
# ---------------------------------------------------------------------------


def _tx(r: dict, row: pd.Series | dict, i: int) -> TxView:
    return TxView(
        transaction_ref=str(i),
        occurred_at=r["occurred_at"],
        amount_minor=int(r["amount_minor"]),
        currency="XXX",
        channel=r["channel"],
        instrument=r["instrument"],
        rail="CARD_SCHEME" if r["instrument"] == "CARD" else "NIP",
        subject_token=r["subject_token"],
        account_token=r["account_token"],
        beneficiary_token=r["beneficiary_token"],
        device_token=r["device_token"],
        ip_region=r["ip_region"],
        merchant_category=row.get("merchant_category"),
        auth_result=r["auth_result"],
        account_opened_at=utc(row.get("account_opened_at")),
        last_activity_at=utc(row.get("last_activity_at")),
        direction=r["direction"],
        remitter_token=r["remitter_token"],
    )


def features(df: pd.DataFrame, cache: Path | None = None) -> np.ndarray:
    """The live feature package over every row, with the whole file as history."""
    if cache and cache.exists():
        data = np.load(cache, allow_pickle=False)
        if str(data["spec"]) == FEATURE_SPEC_VERSION and data["X"].shape[0] == len(df):
            print(f"  features from cache {cache.name}")
            return data["X"]
    rows = to_rows(df)
    print(f"  indexing history for {len(rows):,} rows ...")
    source = PandasHistorySource(rows)
    extra = df[["merchant_category", "account_opened_at", "last_activity_at"]].to_dict("records")
    X = np.zeros((len(rows), len(FEATURE_NAMES)), dtype=float)
    print("  computing features with riskradar.features ...")
    for i, r in enumerate(rows):
        tx = _tx(r, extra[i], i)
        X[i] = to_vector(compute_features(tx, source.load(tx)))
        if i and i % 100_000 == 0:
            print(f"    {i:,} / {len(rows):,}")
    if cache:
        np.savez_compressed(cache, X=X, spec=np.array(FEATURE_SPEC_VERSION))
    return X


def signals_for(df: pd.DataFrame, X: np.ndarray, configs: dict) -> list[list[Signal]]:
    rows = to_rows(df)
    extra = df[["merchant_category", "account_opened_at", "last_activity_at"]].to_dict("records")
    out = []
    print("  evaluating rules ...")
    for i, r in enumerate(rows):
        feats = {name: float(X[i, j]) for j, name in enumerate(FEATURE_NAMES)}
        out.append(evaluate_rules(RuleContext(tx=_tx(r, extra[i], i), features=feats), configs))
    return out


# ---------------------------------------------------------------------------
# Deciding under a budget
# ---------------------------------------------------------------------------

NEVER = Thresholds(id=0, version=0, p_monitor=2.0, p_review=2.0, p_hold=2.0, alert_min_level="MEDIUM")


def rule_driven(signals: list[list[Signal]], model_applies: np.ndarray) -> np.ndarray:
    return np.array([apply_policy(0.0, s, NEVER, model_applies=bool(m)).actionable
                     for s, m in zip(signals, model_applies)])


def decide(p: np.ndarray | None, signals: list[list[Signal]], allowed: int,
           model_applies: np.ndarray, rules_on: bool = True) -> tuple[np.ndarray, dict]:
    """Rules spend the budget first; the score gets what is left (D61b).

    ``p`` None means no score (rules only). ``rules_on`` False means the score
    alone, no rules (a model-only arm).
    """
    n = len(signals)
    by_rules = rule_driven(signals, model_applies) if rules_on else np.zeros(n, dtype=bool)
    info = {"rule_alerts": int(by_rules.sum())}
    if p is None:
        return by_rules, info
    # Ties: a calibrated model gives many rows the same probability, and a
    # threshold on a tied value alerts on all of them, over the budget. Ranks
    # break ties by row order, so the budget is spent exactly.
    p = _ranks(p)
    remaining = allowed - int(by_rules.sum())
    info["budget_left_for_score"] = max(0, remaining)
    candidates = np.flatnonzero(~by_rules & model_applies)
    if remaining <= 0 or len(candidates) == 0:
        return by_rules, info
    k = min(remaining, len(candidates))
    cut = float(np.partition(p[candidates], -k)[-k])
    info["score_threshold"] = cut
    if not rules_on:
        flagged = (p >= cut) & model_applies
        return flagged, info
    th = Thresholds(id=0, version=0, p_monitor=cut,
                    p_review=max(cut, float(np.quantile(p, 0.999))),
                    p_hold=max(cut, float(np.quantile(p, 0.9999))), alert_min_level="MEDIUM")
    flagged = np.array([apply_policy(float(p[i]), signals[i], th, model_applies=bool(model_applies[i])).actionable
                        for i in range(n)])
    return flagged, info


def _ranks(p: np.ndarray) -> np.ndarray:
    order = np.argsort(p, kind="stable")
    r = np.empty(len(p), dtype=float)
    r[order] = np.arange(1, len(p) + 1) / len(p)
    return r


def score_arm(label: str, flagged: np.ndarray, test: pd.DataFrame, days: float, info: dict,
              p: np.ndarray | None = None) -> dict:
    y = test["is_fraud"].to_numpy()
    out: dict[str, Any] = {"arm": label, "alerts": int(flagged.sum()),
                           "alerts_per_day": round(float(flagged.sum()) / days, 1), **info}
    out.pop("score_threshold", None)
    if (y < 0).all():
        return out
    fraud = y == 1
    if p is not None and 0 < fraud.sum() < len(fraud):
        from sklearn.metrics import average_precision_score

        # Budget-free: how well the score ranks fraud above everything else,
        # against the base rate a random order would score.
        ap = float(average_precision_score(fraud, p))
        out["pr_auc"] = round(ap, 4)
        out["pr_auc_lift_over_random"] = round(ap / float(fraud.mean()), 2)
    tp = int((flagged & fraud).sum())
    out["precision"] = round(tp / max(1, int(flagged.sum())), 4)
    out["transaction_recall"] = round(tp / max(1, int(fraud.sum())), 4)
    inc = test["incident_id"].to_numpy()
    incidents = {i for i in inc[fraud] if i}
    caught = {i for i in inc[fraud & flagged] if i}
    lo, hi = wilson(len(caught), len(incidents))
    out.update({"incidents": len(incidents), "incidents_caught": len(caught),
                "incident_recall": round(len(caught) / len(incidents), 4) if incidents else None,
                "ci95": [round(lo, 3), round(hi, 3)]})
    false_alerts = int((flagged & ~fraud).sum())
    out["false_alerts"] = false_alerts
    out["false_alerts_per_incident_caught"] = round(false_alerts / len(caught), 2) if caught else None
    amount = test["amount_minor"].to_numpy()
    total = float(amount[fraud].sum())
    out["fraud_value_recall"] = round(float(amount[fraud & flagged].sum()) / total, 4) if total else None
    by_type = {}
    types = test["fraud_type"].to_numpy()
    for t in sorted({t for t in types[fraud] if t}):
        mask = fraud & (types == t)
        n_t = {i for i in inc[mask] if i}
        c_t = {i for i in inc[mask & flagged] if i}
        lo, hi = wilson(len(c_t), len(n_t))
        by_type[t] = {"incidents": len(n_t), "caught": len(c_t),
                      "recall": round(len(c_t) / len(n_t), 4) if n_t else None, "ci95": [round(lo, 3), round(hi, 3)]}
    out["by_fraud_type"] = by_type
    return out


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def shipped_model(explicit: Path | None = None):
    """The newest promoted-recipe model whose inputs match this feature spec."""
    import joblib

    if explicit:
        return joblib.load(explicit), explicit.name
    for meta in sorted((REPO_ROOT / "ml" / "artifacts").glob("evaluation-*-final.json"), reverse=True):
        try:
            names = json.loads(meta.read_text(encoding="utf-8")).get("feature_names")
        except (OSError, ValueError):
            continue
        version = meta.stem.removeprefix("evaluation-")
        artifact = meta.with_name(f"riskradar-gbm-{version}.joblib")
        if names == list(MODEL_FEATURE_NAMES) and artifact.exists():
            return joblib.load(artifact), artifact.name
    return None, "no shipped model matches feature spec " + FEATURE_SPEC_VERSION


def fit_gbm(X: np.ndarray, y: np.ndarray, times: np.ndarray):
    import sys

    sys.path.insert(0, str(REPO_ROOT / "ml"))
    from train import build_model

    model = build_model()
    model.fit(X, y, times=times)
    return model


def fit_anomaly(X: np.ndarray, seed: int = 20260917):
    from sklearn.ensemble import IsolationForest

    rng = np.random.default_rng(seed)
    sample = X[rng.choice(len(X), size=min(len(X), 200_000), replace=False)]
    return IsolationForest(n_estimators=200, random_state=seed, n_jobs=-1).fit(sample)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    alert_rate: float | None = SHIPPED_ALERT_RATE
    budget_per_day: float | None = None
    test_share: float = 0.3
    model_path: Path | None = None
    unseen_types: bool = True


def run(df: pd.DataFrame, native: pd.DataFrame | None, settings: Settings, cache: Path | None) -> dict:
    import sys

    sys.path.insert(0, str(REPO_ROOT / "ml"))
    from offline_rules import RULE_CONFIGS

    labelled = bool((df["is_fraud"] >= 0).all())
    X = features(df, cache)
    signals = signals_for(df, X, RULE_CONFIGS)
    model_applies = (df["direction"] == "OUTBOUND").to_numpy()
    times = df["occurred_at"].to_numpy()

    cut = int(len(df) * (1 - settings.test_share))
    train_idx, test_idx = np.arange(cut), np.arange(cut, len(df))
    test = df.iloc[test_idx].reset_index(drop=True)
    t0, t1 = pd.Timestamp(times[cut]), pd.Timestamp(times[-1])
    days = max((t1 - t0).total_seconds() / 86400.0, 1 / 24)
    if settings.budget_per_day:
        per_day = float(settings.budget_per_day)
    else:
        per_day = settings.alert_rate * len(test) / days
    allowed = max(1, int(round(per_day * days)))
    sig_test = [signals[i] for i in test_idx]
    ma_test = model_applies[test_idx]

    result: dict[str, Any] = {
        "feature_spec": FEATURE_SPEC_VERSION,
        "labelled": labelled,
        "split": {"rule": "by time", "train_rows": int(cut), "test_rows": int(len(test_idx)),
                  "test_from": t0.isoformat(), "test_to": t1.isoformat(), "test_days": round(days, 2)},
        "budget": {"alerts_per_day": round(per_day, 1), "alerts_in_test": allowed,
                   "share_of_traffic": round(allowed / len(test_idx), 5),
                   "basis": ("fixed alerts a day" if settings.budget_per_day else
                             f"the shipped operating point, {SHIPPED_ALERT_RATE:.3%} of traffic (75 a day, D76)")},
        "arms": [],
        "skipped_arms": {},
    }
    if labelled:
        y = df["is_fraud"].to_numpy()
        result["base_rate"] = {
            "fraud_rows": int(y.sum()), "fraud_row_share": round(float(y.mean()), 5),
            "incidents": int(df.loc[y == 1, "incident_id"].nunique()),
            "incidents_in_test": int(test.loc[test["is_fraud"] == 1, "incident_id"].nunique()),
            "fraud_rows_in_test_per_day": round(float((test["is_fraud"] == 1).sum()) / days, 1),
        }
        if result["base_rate"]["fraud_rows_in_test_per_day"] > per_day:
            result["warnings"] = [
                f"there are {result['base_rate']['fraud_rows_in_test_per_day']} fraud rows a day against a budget "
                f"of {per_day:.0f} alerts a day: row recall is capped well below 1 by the budget, not by detection. "
                "Incident recall is the fairer figure."]

    arms = result["arms"]
    flagged_by_arm: dict[str, np.ndarray] = {}
    scores_by_arm: dict[str, np.ndarray] = {}

    def add(label: str, p: np.ndarray | None, rules_on: bool = True):
        flagged, info = decide(p, sig_test, allowed, ma_test, rules_on)
        arms.append(score_arm(label, flagged, test, days, info, p))
        flagged_by_arm[label] = flagged
        if p is not None:
            scores_by_arm[label] = p

    add("rules only", None)
    rule_alerts = arms[-1]["rule_alerts"]
    if rule_alerts > allowed:
        fired: dict[str, int] = {}
        for sig in sig_test:
            for x in sig:
                if x.power == "ESCALATE":
                    fired[x.code] = fired.get(x.code, 0) + 1
        top = ", ".join(f"{k} {v / days:.0f}/day" for k, v in sorted(fired.items(), key=lambda kv: -kv[1])[:3])
        result.setdefault("warnings", []).append(
            f"the rules alone raise {rule_alerts / days:.0f} alerts a day, over the {per_day:.0f}-a-day budget "
            f"({top}). Rules spend the budget first, so every arm with rules is the rules; compare the model-alone "
            "arms and the ranking column instead, and retune those rules for this bank before relying on them.")

    Xm = X[:, MODEL_COLUMNS]
    model, model_name = shipped_model(settings.model_path)
    result["shipped_model"] = model_name
    if model is not None:
        p = model.predict_proba(Xm[test_idx])[:, 1]
        add("shipped model + rules", p)
        add("shipped model alone", p, rules_on=False)
    else:
        result["skipped_arms"]["shipped model"] = model_name

    anomaly = fit_anomaly(Xm[train_idx])
    add("anomaly score (no labels) + rules", -anomaly.score_samples(Xm[test_idx]))

    amount = X[test_idx, FEATURE_NAMES.index("amount_log10")]
    add("amount alone", amount, rules_on=False)

    if labelled:
        y = df["is_fraud"].to_numpy()
        train_fraud = int(y[train_idx].sum())
        if train_fraud >= MIN_TRAIN_FRAUD_ROWS:
            print("  retraining on the older part of the file ...")
            gbm = fit_gbm(Xm[train_idx], y[train_idx], times[train_idx])
            p = gbm.predict_proba(Xm[test_idx])[:, 1]
            add("retrained model + rules", p)
            add("retrained model alone", p, rules_on=False)
            if native is not None and len(native.columns):
                nat = native.apply(pd.to_numeric, errors="coerce").fillna(-999).to_numpy(dtype=float)
                Xn = np.hstack([Xm, nat])
                gbm_n = fit_gbm(Xn[train_idx], y[train_idx], times[train_idx])
                add("retrained with the dataset's own columns + rules", gbm_n.predict_proba(Xn[test_idx])[:, 1])
                result["native_columns"] = list(native.columns)
            if settings.unseen_types:
                result["unseen_type_test"] = unseen_type_test(df, Xm, signals, model_applies, per_day, cut)
        else:
            result["skipped_arms"]["retrained model"] = (
                f"only {train_fraud} fraud rows in the training part; at least {MIN_TRAIN_FRAUD_ROWS} needed")

    if labelled:
        lifts = [a["pr_auc_lift_over_random"] for a in arms if a.get("pr_auc_lift_over_random") is not None]
        if lifts and max(lifts) < 2.0:
            result.setdefault("warnings", []).append(
                f"no score ranks fraud even twice as well as a random order (best PR-AUC lift {max(lifts):.2f}), "
                "including models trained on this file with its own columns. The labels look unrelated to anything "
                "in the data, so no detector could be measured on them; treat every figure here as uninformative.")
    result["_flagged"] = flagged_by_arm
    result["_scores"] = scores_by_arm
    result["_X"] = X
    result["_signals_test"] = sig_test
    result["_test_idx"] = test_idx
    return result


def unseen_type_test(df: pd.DataFrame, Xm: np.ndarray, signals, model_applies, per_day: float, cut: int) -> dict:
    """Hide one fraud type from training at a time; is it still caught?

    Mirrors ``ml/evaluate_system.py``: legitimate rows split by time, every
    incident of the hidden type in the test, none of it in training.
    """
    y = df["is_fraud"].to_numpy()
    types = df["fraud_type"].to_numpy()
    inc = df["incident_id"].to_numpy()
    counts = {t: len({i for i in inc[(y == 1) & (types == t)] if i}) for t in {t for t in types[y == 1] if t}}
    eligible = sorted(t for t, n in counts.items() if n >= MIN_TYPE_INCIDENTS and t != "UNSPECIFIED")
    out: dict[str, Any] = {"types": counts, "tested": eligible, "results": {}}
    if len(eligible) < 2:
        out["skipped"] = f"needs at least two labelled fraud types with {MIN_TYPE_INCIDENTS}+ incidents each"
        return out
    times = df["occurred_at"].to_numpy()
    idx = np.arange(len(df))
    legit = y == 0
    anomaly = fit_anomaly(Xm[:cut])  # every training row: this arm never reads a label
    for t in eligible:
        print(f"  unseen-type test: hiding {t} ...")
        hidden = (y == 1) & (types == t)
        train = idx[(idx < cut) & (legit | ((y == 1) & ~hidden))]
        test = idx[((idx >= cut) & legit) | hidden]
        if y[train].sum() < MIN_TRAIN_FRAUD_ROWS:
            out["results"][t] = {"skipped": "too few other fraud rows to train on"}
            continue
        gbm = fit_gbm(Xm[train], y[train], times[train])
        tdf = df.iloc[test].reset_index(drop=True)
        tdf.loc[(tdf["is_fraud"] == 1) & (tdf["fraud_type"] != t), "is_fraud"] = 0
        span = max((pd.Timestamp(times[test].max()) - pd.Timestamp(times[test].min())).total_seconds() / 86400, 1 / 24)
        test_days = max((pd.Timestamp(times[-1]) - pd.Timestamp(times[cut])).total_seconds() / 86400, 1 / 24)
        allowed = max(1, int(round(per_day * test_days)))
        sig = [signals[i] for i in test]
        ma = model_applies[test]
        rows = {}
        for label, p, rules_on in (("rules only", None, True),
                                   ("retrained model + rules", gbm.predict_proba(Xm[test])[:, 1], True),
                                   ("retrained model alone", gbm.predict_proba(Xm[test])[:, 1], False),
                                   ("anomaly score (no labels) + rules", -anomaly.score_samples(Xm[test]), True)):
            flagged, _ = decide(p, sig, allowed, ma, rules_on)
            arm = score_arm(label, flagged, tdf, test_days, {})
            rows[label] = {k: arm.get(k) for k in ("incidents", "incidents_caught", "incident_recall", "ci95",
                                                   "false_alerts_per_incident_caught", "fraud_value_recall")}
        out["results"][t] = rows
        out.setdefault("note", "hidden-type incidents are tested across the whole file; legitimate rows only from "
                               f"the test period. Span of the test rows {span:.1f} days.")
    return out


def cache_key(path: Path, mapping_text: str, max_rows: int | None) -> str:
    h = hashlib.sha256()
    st = path.stat()
    h.update(f"{path.name}:{st.st_size}:{int(st.st_mtime)}:{max_rows}:{FEATURE_SPEC_VERSION}".encode())
    h.update(mapping_text.encode())
    return h.hexdigest()[:16]

"""Is the model still seeing the world it learned, and are the rules still firing as tuned? (D88)

Plain English
-------------
A fraud model does not fail loudly. Customers change how they pay, a new app
release changes a device field, a rule's threshold stops meaning what it did,
and the scores drift while every dashboard stays green. This compares a recent
window of scored traffic with the window before it and says, in numbers a
data scientist would accept, what moved:

* **Score drift.** The population stability index (PSI) of the model's
  probability, recent against baseline, over the baseline's deciles.
* **Feature drift.** The same index for each of the model's inputs. A feature
  that moved is where to look first when the score moved.
* **Rule health.** Each rule's firing rate per 10,000 payments, now and before.
  A rule that went silent may be broken; one that fires five times as often is
  either catching a campaign or about to flood the desk.

Conventional reading of PSI: under 0.10 stable, 0.10 to 0.25 worth watching,
over 0.25 a shift that needs a person (and usually a retrain or a re-derived
threshold). The windows are by when payments happened, so a replayed month
compares like with like.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .features.spec import MODEL_FEATURE_NAMES
from .rules.engine import ALL_RULE_CODES

STABLE, WATCH = 0.10, 0.25
EPS = 1e-4


def psi(baseline: np.ndarray, recent: np.ndarray, bins: int = 10) -> float | None:
    """Population stability index over the baseline's quantile bins."""
    baseline = baseline[np.isfinite(baseline)]
    recent = recent[np.isfinite(recent)]
    if len(baseline) < 50 or len(recent) < 50:
        return None
    edges = np.unique(np.quantile(baseline, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        # A constant feature: stable unless the recent window is not the same constant.
        return 0.0 if np.allclose(recent, edges[0]) else 1.0
    edges[0], edges[-1] = -np.inf, np.inf
    b = np.histogram(baseline, edges)[0] / len(baseline)
    r = np.histogram(recent, edges)[0] / len(recent)
    b, r = np.clip(b, EPS, None), np.clip(r, EPS, None)
    return float(np.sum((r - b) * np.log(r / b)))


def reading(value: float | None) -> str:
    if value is None:
        return "TOO_FEW"
    return "STABLE" if value < STABLE else "WATCH" if value < WATCH else "SHIFTED"


WINDOW_SQL = """
    SELECT d.p_fraud, d.features, d.signals, d.model_version_id
      FROM decisions d
      JOIN transactions t ON t.id = d.transaction_id
     WHERE t.direction = 'OUTBOUND'
       AND t.occurred_at >= %(start)s AND t.occurred_at < %(end)s
     ORDER BY t.occurred_at DESC
     LIMIT %(limit)s
"""


def _window(conn: Any, start: Any, end: Any, limit: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(WINDOW_SQL, {"start": start, "end": end, "limit": limit})
        return [dict(r) for r in cur.fetchall()]


def _matrix(rows: list[dict[str, Any]]) -> np.ndarray:
    out = np.full((len(rows), len(MODEL_FEATURE_NAMES)), np.nan)
    for i, r in enumerate(rows):
        f = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"] or "{}")
        for j, name in enumerate(MODEL_FEATURE_NAMES):
            v = f.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[i, j] = float(v)
            elif isinstance(v, bool):
                out[i, j] = float(v)
    return out


def _codes(r: dict[str, Any]) -> set[str]:
    sigs = r["signals"] if isinstance(r["signals"], list) else json.loads(r["signals"] or "[]")
    return {s["code"] for s in sigs}


def report(conn: Any, *, recent_days: float = 1.0, baseline_days: float = 7.0, limit: int = 20_000) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(occurred_at) AS end FROM transactions WHERE direction = 'OUTBOUND'")
        end = cur.fetchone()["end"]
        cur.execute("SELECT id FROM model_versions WHERE is_active")
        active = cur.fetchone()
    if end is None:
        return {"status": "NO_DATA"}
    from datetime import timedelta

    split = end - timedelta(days=recent_days)
    start = split - timedelta(days=baseline_days)
    recent = _window(conn, split, end + timedelta(seconds=1), limit)
    base = _window(conn, start, split, limit)
    active_id = active["id"] if active else None
    # Compare the model with itself: a baseline scored by an older model says nothing about drift.
    rs = [r for r in recent if r["model_version_id"] == active_id]
    bs = [r for r in base if r["model_version_id"] == active_id]

    score_psi = psi(np.array([float(r["p_fraud"]) for r in bs]), np.array([float(r["p_fraud"]) for r in rs]))
    xb, xr = _matrix(bs), _matrix(rs)
    features = []
    for j, name in enumerate(MODEL_FEATURE_NAMES):
        v = psi(xb[:, j], xr[:, j]) if len(bs) and len(rs) else None
        features.append({"feature": name, "psi": None if v is None else round(v, 4), "reading": reading(v),
                         "baseline_mean": _mean(xb[:, j]), "recent_mean": _mean(xr[:, j])})
    features.sort(key=lambda f: -(f["psi"] or 0))

    rules = []
    nb, nr = max(len(base), 1), max(len(recent), 1)
    fired_b = {c: 0 for c in ALL_RULE_CODES}
    fired_r = {c: 0 for c in ALL_RULE_CODES}
    for r in base:
        for c in _codes(r) & fired_b.keys():
            fired_b[c] += 1
    for r in recent:
        for c in _codes(r) & fired_r.keys():
            fired_r[c] += 1
    for code in ALL_RULE_CODES:
        per_b, per_r = 1e4 * fired_b[code] / nb, 1e4 * fired_r[code] / nr
        expected = fired_b[code] / nb * len(recent)
        if expected >= 3 and fired_r[code] == 0:
            state = "SILENT"
        elif fired_r[code] >= 5 and per_r > 3 * max(per_b, 1e4 / nb):
            state = "SPIKE"
        elif fired_r[code] == 0 and fired_b[code] == 0:
            state = "IDLE"
        else:
            state = "NORMAL"
        rules.append({"rule": code, "state": state, "recent_fired": fired_r[code], "baseline_fired": fired_b[code],
                      "recent_per_10k": round(per_r, 2), "baseline_per_10k": round(per_b, 2)})

    shifted = [f["feature"] for f in features if f["reading"] == "SHIFTED"]
    attention = [r["rule"] for r in rules if r["state"] in ("SILENT", "SPIKE")]
    overall = ("SHIFTED" if reading(score_psi) == "SHIFTED" or shifted
               else "WATCH" if reading(score_psi) == "WATCH" or attention else "STABLE")
    if score_psi is None:
        overall = "TOO_FEW"
    return {
        "status": overall,
        "windows": {"baseline": [start, split], "recent": [split, end],
                    "baseline_payments": len(base), "recent_payments": len(recent),
                    "scored_by_active_model": {"baseline": len(bs), "recent": len(rs)}},
        "score": {"psi": None if score_psi is None else round(score_psi, 4), "reading": reading(score_psi),
                  "baseline_mean_p": _mean(np.array([float(r["p_fraud"]) for r in bs])),
                  "recent_mean_p": _mean(np.array([float(r["p_fraud"]) for r in rs]))},
        "features": features,
        "rules": rules,
        "needs_attention": {"shifted_features": shifted, "rules": attention},
        "thresholds": {"stable_below": STABLE, "shifted_above": WATCH},
    }


def _mean(x: np.ndarray) -> float | None:
    x = x[np.isfinite(x)]
    return None if len(x) == 0 else round(float(x.mean()), 5)

"""Thresholds from the alert budget, and a check that they still fit (D11d, D86).

Plain English
-------------
Thresholds are solved backwards from the number of alerts the desk can read.
``derive`` does the solving: rules first (their alerts are spent before the
model gets any), then the probability at which the model's share of the budget
runs out. ``implied`` does the checking: given the thresholds in force, how
many alerts a day would the traffic just seen have produced?

The two used to live only in ``scripts/derive_thresholds.py``. They are here so
the API can answer "are the thresholds still right?" on any day, and an
administrator can publish a re-derived set from the console, without the
arithmetic existing twice.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..rules.engine import Signal
from .engine import Thresholds, apply as apply_policy

NEVER = Thresholds(id=0, version=0, p_monitor=2.0, p_review=2.0, p_hold=2.0, alert_min_level="MEDIUM")

SAMPLE_SQL = """
    SELECT d.p_fraud, t.occurred_at, d.signals
      FROM decisions d
      JOIN transactions t ON t.id = d.transaction_id
     WHERE d.rule_only_mode = false
       AND (%(last_days)s::float IS NULL
            OR t.occurred_at >= (SELECT max(occurred_at) FROM transactions)
                                - make_interval(secs => %(last_days)s::float * 86400))
     ORDER BY d.decided_at DESC
     LIMIT %(limit)s
"""


class CalibrationRefused(Exception):
    """The sample is too small, or the rules alone overspend the budget."""


@dataclass
class Sample:
    p: np.ndarray
    signals: list[list[Signal]]
    span_days: float
    rules: np.ndarray | None = None

    @property
    def daily_volume(self) -> float:
        return len(self.p) / self.span_days


def _signals(raw: Any) -> list[Signal]:
    if not raw or raw == "[]":
        return []
    items = raw if isinstance(raw, list) else json.loads(raw)
    return [Signal(code=x["code"], power=x["power"], severity=x["severity"], evidence=x.get("evidence") or {})
            for x in items]


def load_sample(conn: Any, *, last_days: float | None = None, limit: int = 200_000) -> Sample:
    with conn.cursor() as cur:
        cur.execute(SAMPLE_SQL, {"last_days": last_days, "limit": limit})
        rows = cur.fetchall()
    if not rows:
        return Sample(np.zeros(0), [], 1.0)
    times = [r["occurred_at"] for r in rows]
    span = max((max(times) - min(times)).total_seconds() / 86400.0, 0.5)
    return Sample(np.array([float(r["p_fraud"]) for r in rows]), [_signals(r["signals"]) for r in rows], span)


def rule_driven(sample: Sample) -> np.ndarray:
    """Payments the rules would alert on even if the model said zero."""
    if sample.rules is None:
        # Most payments fire no rule, and those cannot be rule-driven: skip the policy walk.
        sample.rules = np.array([bool(s) and apply_policy(0.0, s, NEVER).actionable for s in sample.signals],
                                dtype=bool)
    return sample.rules


def implied(sample: Sample, thresholds: Thresholds) -> dict[str, Any]:
    """What these thresholds would have raised on this traffic, per day, before the budget guard."""
    if len(sample.p) == 0:
        return {"sample": 0}
    levels = Counter()
    actionable = 0
    from .engine import LEVELS, base_level

    alert_from = LEVELS.index(thresholds.alert_min_level)
    for p, sigs in zip(sample.p, sample.signals):
        if sigs:
            r = apply_policy(float(p), sigs, thresholds)
            level, actionable_here = r.risk_level, r.actionable
        else:
            # No rule fired: the level is the model's band, nothing else moves it.
            level = base_level(float(p), thresholds)
            actionable_here = LEVELS.index(level) >= alert_from
        if actionable_here:
            actionable += 1
            levels[level] += 1
    per_day = lambda n: round(n / sample.span_days, 1)  # noqa: E731
    rules = rule_driven(sample)
    return {
        "sample": int(len(sample.p)),
        "span_days": round(sample.span_days, 2),
        "daily_volume": round(sample.daily_volume, 0),
        "alerts_per_day": per_day(actionable),
        "of_which_rules_alone_per_day": per_day(int(rules.sum())),
        "by_level_per_day": {k: per_day(v) for k, v in sorted(levels.items())},
    }


def derive(sample: Sample, budget: int, *, min_sample: int = 2000, rule_share: float = 0.6) -> dict[str, Any]:
    """Thresholds that spend the model's envelope of ``budget`` alerts a day (D92).

    Until D92 the rules were spent first and the model got the remainder, which
    measured (D91) as the rules taking 45 of 75 alerts at precision 0.605 while
    the model ranked better. Now each layer has a share: the model's threshold
    is solved for ``budget × (1 - rule_share)``, and rule alerts over their own
    share are paced by the budget guard rather than crowding the model out.
    """
    if len(sample.p) < min_sample:
        raise CalibrationRefused(
            f"only {len(sample.p)} scored decisions; need at least {min_sample}. Let the worker drain first."
        )
    p = sample.p
    rules = rule_driven(sample)
    rule_per_day = float(rules.sum()) / sample.span_days
    rule_quota = budget * rule_share
    remaining = budget - min(rule_per_day, rule_quota)
    by_rule: Counter = Counter()
    for sigs, flagged in zip(sample.signals, rules):
        if flagged:
            for s in sigs:
                if s.power in ("ESCALATE", "OVERRIDE"):
                    by_rule[s.code] += 1
    rules_per_day = {code: round(n / sample.span_days, 1) for code, n in by_rule.most_common()}
    if remaining <= 0:
        raise CalibrationRefused(
            f"the rules' share alone ({rule_quota:.0f} a day) is the whole {budget} a day budget; "
            f"leave the model a share: {rules_per_day}"
        )
    note = None
    if rule_per_day > rule_quota:
        # Not a refusal any more (D92): the guard ranks rule alerts and paces
        # the ones over the share, so the model's envelope is safe either way.
        note = (f"the rules would raise {rule_per_day:.0f} a day against their share of {rule_quota:.0f}; "
                f"the guard will pace the rest, most serious first. Retune if that is not what you want: "
                f"{rules_per_day}")

    p_free = p[~rules]
    alert_rate = min(1.0, remaining / max(sample.daily_volume * (1 - rules.mean()), 1e-9))

    def within(share: float) -> float:
        """The lowest threshold whose alert share does not exceed ``share`` (D85: step past ties)."""
        t = float(np.quantile(p_free, 1.0 - share))
        if float(np.mean(p_free >= t)) <= share:
            return t
        above = np.unique(p_free[p_free > t])
        return float(above[0]) if len(above) else float(np.nextafter(t, 1.0))

    p_monitor = within(alert_rate)
    p_review = max(within(alert_rate / 3.0), p_monitor)
    p_hold = max(within(alert_rate / 10.0), p_review)
    system = rules | (p >= p_monitor)
    return {
        "budget": budget,
        "rule_share": rule_share,
        "rule_quota_per_day": round(rule_quota, 1),
        "model_quota_per_day": round(budget - rule_quota, 1),
        "note": note,
        "p_monitor": p_monitor,
        "p_review": p_review,
        "p_hold": p_hold,
        "sample": int(len(p)),
        "span_days": round(sample.span_days, 2),
        "daily_volume": round(sample.daily_volume, 0),
        "alert_share": alert_rate,
        "rules_alone_per_day": round(rule_per_day, 1),
        "rules_per_day": rules_per_day,
        "implied": {
            "whole_system_alerts_per_day": round(float(system.sum()) / sample.span_days, 1),
            "of_which_rules_alone_per_day": round(rule_per_day, 1),
            "monitor_and_above_per_day": round(float(np.mean(p >= p_monitor)) * sample.daily_volume, 1),
            "review_and_above_per_day": round(float(np.mean(p >= p_review)) * sample.daily_volume, 1),
            "hold_per_day": round(float(np.mean(p >= p_hold)) * sample.daily_volume, 1),
        },
    }

"""The alert budget, enforced as alerts are raised (D86).

Plain English
-------------
The desk can read 75 alerts a day (D76). The thresholds are worked out
backwards from that number (D11d), but only from last month's traffic. When
today's traffic looks different, thresholds alone send the desk 300 alerts
or 20, and nobody finds out until the analysts do.

So every alert now passes one more check before it reaches a person. It counts
against the bank's local day. Once the day's budget is spent, or the current
hour has used its share, a discretionary alert is **deferred**, not dropped.
Deferred alerts wait in a queue, most serious first, and are raised as soon as
there is room: at the top of the next hour, or first thing tomorrow. One still
waiting after a day expires. The expiry is recorded and shown, because it is a
suspicious payment nobody read.

Three kinds of alert are never held back, and still count:

* **mandatory**: a veto rule fired (sanctions, a known mule). A person must own
  it today, whatever the budget says.
* **machine action** (D80): the system acts on a card-testing run or a takeover
  in progress. Delaying the block defeats it, and no analyst time is spent.
* anything raised while enforcement is switched off, for a measurement that
  must see every alert the thresholds imply.

If mandatory and machine alerts alone overrun the day, the guard cannot help.
That raises an alarm: the rules need retuning, not the desk more patience.

The budget counts against the bank's clock (UTC+1, the same local time the
features use), not the transaction's own time: capacity is analysts' hours.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..features.spec import LOCAL_UTC_OFFSET_HOURS

LOCAL = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))

RAISE = "RAISE"
DEFER = "DEFER"

DAILY_CAP = "DAILY_CAP"
HOURLY_PACE = "HOURLY_PACE"
RULE_QUOTA = "RULE_QUOTA"
MODEL_QUOTA = "MODEL_QUOTA"
MANDATORY = "MANDATORY"
MACHINE = "MACHINE"
NOT_ENFORCED = "NOT_ENFORCED"
WITHIN_BUDGET = "WITHIN_BUDGET"


@dataclass(frozen=True)
class BudgetConfig:
    per_day: int = 75
    hourly_burst: float = 3.0
    enforced: bool = True
    deferral_hours: int = 24
    # D92: the share of the day reserved for discretionary rule alerts. The
    # model gets the rest, and neither layer can spend the other's envelope.
    rule_share: float = 0.6

    @property
    def hour_ceiling(self) -> int:
        """The most alerts any one hour may raise: ``burst`` times an even share."""
        return max(1, math.ceil(self.per_day * self.hourly_burst / 24.0))

    @property
    def rule_quota(self) -> int:
        return int(round(self.per_day * self.rule_share))

    @property
    def model_quota(self) -> int:
        return self.per_day - self.rule_quota


@dataclass(frozen=True)
class Admission:
    verdict: str          # RAISE or DEFER
    reason: str           # why
    day: date
    hour: int
    raised_today: int     # after this admission
    hour_count: int       # after this admission
    config: BudgetConfig
    rule_driven: bool = False   # D92: which envelope it was judged against

    @property
    def raised(self) -> bool:
        return self.verdict == RAISE

    def as_trace(self) -> dict[str, Any]:
        return {
            "step": "budget",
            "verdict": self.verdict,
            "reason": self.reason,
            "local_day": self.day.isoformat(),
            "local_hour": self.hour,
            "raised_today": self.raised_today,
            "per_day": self.config.per_day,
            "hour_count": self.hour_count,
            "hour_ceiling": self.config.hour_ceiling,
            "envelope": "rules" if self.rule_driven else "model",
            "rule_quota": self.config.rule_quota,
            "model_quota": self.config.model_quota,
        }


def local_day_hour(now: datetime) -> tuple[date, int]:
    local = now.astimezone(LOCAL)
    return local.date(), local.hour


def judge(*, raised_today: int, hour_count: int, config: BudgetConfig, mandatory: bool, machine: bool,
          rule_driven: bool = False, rule_raised: int = 0, model_raised: int = 0) -> tuple[str, str]:
    """Pure: raise or defer one alert, given what the day, the hour and each envelope have spent."""
    if mandatory:
        return RAISE, MANDATORY
    if machine:
        return RAISE, MACHINE
    if not config.enforced:
        return RAISE, NOT_ENFORCED
    if raised_today >= config.per_day:
        return DEFER, DAILY_CAP
    if hour_count >= config.hour_ceiling:
        return DEFER, HOURLY_PACE
    # D92: each layer spends its own envelope. A rule alert held here is not
    # lost: it waits, ranked against the other rule alerts, and is raised when
    # the rules have room again — which makes the share a share, not a cut.
    if rule_driven and rule_raised >= config.rule_quota:
        return DEFER, RULE_QUOTA
    if not rule_driven and model_raised >= config.model_quota:
        return DEFER, MODEL_QUOTA
    return RAISE, WITHIN_BUDGET


def _config_value(rows: dict[str, Any], key: str, default: Any) -> Any:
    value = rows.get(key)
    return default if value is None else value


def load_config(conn: Any) -> BudgetConfig:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM app_config WHERE key IN "
            "('alert_budget_per_day', 'alert_budget_hourly_burst', 'alert_budget_enforced', "
            "'alert_deferral_hours', 'alert_budget_rule_share')"
        )
        rows = {r["key"]: r["value"] for r in (dict(x) for x in cur.fetchall())}
    return BudgetConfig(
        per_day=int(_config_value(rows, "alert_budget_per_day", 75)),
        hourly_burst=float(_config_value(rows, "alert_budget_hourly_burst", 3.0)),
        enforced=bool(_config_value(rows, "alert_budget_enforced", True)),
        deferral_hours=int(_config_value(rows, "alert_deferral_hours", 24)),
        rule_share=float(_config_value(rows, "alert_budget_rule_share", 0.35)),
    )


def lock_day(conn: Any, day: date, config: BudgetConfig) -> dict[str, Any]:
    """The day's ledger row, created if new, locked until the caller commits.

    The lock is what makes the count exact under several workers: two alerts
    cannot both see 74 and both become the 75th. It is taken before the case
    correlation and audit locks on every path, so the order never inverts.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alert_budget_days (day, budget) VALUES (%s, %s) ON CONFLICT (day) DO NOTHING",
            (day, config.per_day),
        )
        cur.execute("SELECT * FROM alert_budget_days WHERE day = %s FOR UPDATE", (day,))
        row = dict(cur.fetchone())
        if row["budget"] != config.per_day:
            # The budget was changed today: the new figure applies from now on.
            cur.execute("UPDATE alert_budget_days SET budget = %s WHERE day = %s", (config.per_day, day))
            row["budget"] = config.per_day
    return row


def admit(conn: Any, *, mandatory: bool, machine: bool, rule_driven: bool = False,
          now: datetime | None = None, config: BudgetConfig | None = None) -> Admission:
    """Decide one alert and write it to the ledger. Runs in the caller's transaction."""
    now = now or datetime.now(timezone.utc)
    config = config or load_config(conn)
    day, hour = local_day_hour(now)
    row = lock_day(conn, day, config)
    raised_today, hour_count = int(row["raised"]), int(row["hourly"][hour])
    verdict, reason = judge(raised_today=raised_today, hour_count=hour_count, config=config,
                            mandatory=mandatory, machine=machine, rule_driven=rule_driven,
                            rule_raised=int(row["rule_raised"]), model_raised=int(row["model_raised"]))
    with conn.cursor() as cur:
        if verdict == RAISE:
            cur.execute(
                """
                UPDATE alert_budget_days
                   SET raised = raised + 1,
                       mandatory = mandatory + %(m)s,
                       machine = machine + %(x)s,
                       rule_raised = rule_raised + %(r)s,
                       model_raised = model_raised + %(o)s,
                       hourly[%(h)s] = hourly[%(h)s] + 1,
                       updated_at = now()
                 WHERE day = %(d)s
                """,
                # A veto or a machine action is outside both envelopes (D92).
                {"m": int(mandatory), "x": int(machine and not mandatory), "h": hour + 1, "d": day,
                 "r": int(rule_driven and not (mandatory or machine)),
                 "o": int(not rule_driven and not (mandatory or machine))},
            )
            raised_today, hour_count = raised_today + 1, hour_count + 1
        else:
            cur.execute("UPDATE alert_budget_days SET deferred = deferred + 1, updated_at = now() WHERE day = %s",
                        (day,))
    return Admission(verdict, reason, day, hour, raised_today, hour_count, config, rule_driven=rule_driven)


def overrun_needs_alarm(conn: Any, admission: Admission) -> bool:
    """True once per day, when alerts the guard may not hold back overrun the budget."""
    if admission.verdict != RAISE or admission.reason not in (MANDATORY, MACHINE):
        return False
    if admission.raised_today <= admission.config.per_day:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_budget_days SET overrun_alarmed_at = now() "
            "WHERE day = %s AND overrun_alarmed_at IS NULL RETURNING day",
            (admission.day,),
        )
        return cur.fetchone() is not None


def record_deferral(conn: Any, admission: Admission, *, decision_id: int, transaction_id: int,
                    subject_token: str, risk_level: str, p_fraud: float, score: int,
                    signal_codes: list[str], now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alert_deferrals
                (decision_id, transaction_id, subject_token, risk_level, p_fraud, score_0_100,
                 signals, reason, deferred_at, expires_at, rule_driven)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (decision_id, transaction_id, subject_token, risk_level, p_fraud, score, signal_codes,
             admission.reason, now, now + timedelta(hours=admission.config.deferral_hours),
             admission.rule_driven),
        )
        return int(dict(cur.fetchone())["id"])


def headroom(row: dict[str, Any], hour: int, config: BudgetConfig) -> dict[str, int]:
    """How many deferred alerts may be raised right now, per envelope (D92)."""
    if not config.enforced:
        return {"total": 1_000_000, "rules": 1_000_000, "model": 1_000_000}
    total = max(0, min(config.per_day - int(row["raised"]), config.hour_ceiling - int(row["hourly"][hour])))
    return {
        "total": total,
        "rules": max(0, min(total, config.rule_quota - int(row.get("rule_raised") or 0))),
        "model": max(0, min(total, config.model_quota - int(row.get("model_raised") or 0))),
    }


def count_release(conn: Any, day: date, hour: int, *, rule_driven: bool = False) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE alert_budget_days
               SET raised = raised + 1, released = released + 1,
                   rule_raised = rule_raised + %(r)s, model_raised = model_raised + %(o)s,
                   hourly[%(h)s] = hourly[%(h)s] + 1, updated_at = now()
             WHERE day = %(d)s
            """,
            {"h": hour + 1, "d": day, "r": int(rule_driven), "o": int(not rule_driven)},
        )


def expire_waiting(conn: Any, day: date, now: datetime) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE alert_deferrals SET state = 'EXPIRED', resolved_at = %s
             WHERE state = 'WAITING' AND expires_at <= %s
            RETURNING id, decision_id, transaction_id, risk_level, p_fraud
            """,
            (now, now),
        )
        expired = [dict(r) for r in cur.fetchall()]
        if expired:
            cur.execute("UPDATE alert_budget_days SET expired = expired + %s, updated_at = now() WHERE day = %s",
                        (len(expired), day))
    return expired


def pace(row: dict[str, Any] | None, config: BudgetConfig, now: datetime) -> dict[str, Any]:
    """Where the day stands against its budget, for the screen and the alarms."""
    local = now.astimezone(LOCAL)
    elapsed = (local.hour * 3600 + local.minute * 60 + local.second) / 86400.0
    raised = int(row["raised"]) if row else 0
    hour_count = int(row["hourly"][local.hour]) if row else 0
    expected = config.per_day * elapsed
    projected = raised / elapsed if elapsed > 1 / 24 else None
    if raised >= config.per_day:
        state = "SPENT"
    elif config.enforced and hour_count >= config.hour_ceiling:
        state = "PACED"
    elif raised > expected * 1.25 + 3:
        state = "AHEAD"
    elif raised < expected * 0.5 - 3:
        state = "UNDER"
    else:
        state = "ON_PACE"
    return {
        "state": state,
        "rule_alerts_today": int(row.get("rule_raised") or 0) if row else 0,
        "model_alerts_today": int(row.get("model_raised") or 0) if row else 0,
        "rule_quota": config.rule_quota,
        "model_quota": config.model_quota,
        "day_elapsed": round(elapsed, 4),
        "expected_by_now": round(expected, 1),
        "projected_end_of_day": round(projected, 1) if projected is not None else None,
        "remaining_today": max(0, config.per_day - raised),
        "remaining_this_hour": max(0, config.hour_ceiling - hour_count) if config.enforced else None,
    }

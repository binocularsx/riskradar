-- D86: the alert budget is enforced where alerts are raised, not only where
-- thresholds are derived.
--
-- Until now the budget (75 a day, D76) was honoured by arithmetic done once,
-- offline: thresholds solved backwards from recent traffic. Nothing stopped the
-- desk receiving 300 alerts on a day the traffic changed shape, and on IEEE-CIS
-- that is exactly what a rounding fault produced (D85). The guard below counts
-- every alert as it is raised, in the bank's local day, and holds back the
-- discretionary ones once the day's budget or the hour's share of it is spent.
--
-- Held back is not thrown away. A deferred alert waits, ordered by risk, and is
-- raised as soon as the budget has room; one still waiting after a day expires,
-- and the expiry is recorded, because an alert that never reached a person is
-- the most important number an operations lead can be shown.

-- One row per local day: what was spent, and on what.
CREATE TABLE alert_budget_days (
    day                 date        PRIMARY KEY,
    budget              int         NOT NULL CHECK (budget > 0),
    raised              int         NOT NULL DEFAULT 0,  -- every alert that reached the desk or the machine
    mandatory           int         NOT NULL DEFAULT 0,  -- of which a veto rule demanded (never deferred)
    machine             int         NOT NULL DEFAULT 0,  -- of which the system acted on (D80, never deferred)
    deferred            int         NOT NULL DEFAULT 0,  -- held back when they arrived
    released            int         NOT NULL DEFAULT 0,  -- raised later from the deferred queue
    expired             int         NOT NULL DEFAULT 0,  -- waited a full day and never reached a person
    hourly              int[]       NOT NULL DEFAULT array_fill(0, ARRAY[24]),
    overrun_alarmed_at  timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_deferrals (
    id                  bigserial   PRIMARY KEY,
    decision_id         bigint      NOT NULL UNIQUE REFERENCES decisions(id) ON DELETE CASCADE,
    transaction_id      bigint      NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    subject_token       text        NOT NULL,
    risk_level          risk_level  NOT NULL,
    p_fraud             numeric(14,12) NOT NULL,
    score_0_100         int         NOT NULL,
    signals             text[]      NOT NULL DEFAULT '{}',
    reason              text        NOT NULL CHECK (reason IN ('DAILY_CAP', 'HOURLY_PACE')),
    deferred_at         timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    state               text        NOT NULL DEFAULT 'WAITING' CHECK (state IN ('WAITING', 'RELEASED', 'EXPIRED')),
    resolved_at         timestamptz,
    released_by         bigint REFERENCES users(id),
    alert_id            bigint REFERENCES alerts(id)
);
-- The release query: waiting, most serious first.
CREATE INDEX alert_deferrals_waiting_idx ON alert_deferrals (state, risk_level DESC, p_fraud DESC)
    WHERE state = 'WAITING';

-- Live traffic is scored before replayed history. A month of backfill queued
-- ahead of a payment happening now would make the live desk wait for it.
ALTER TABLE scoring_queue ADD COLUMN priority smallint NOT NULL DEFAULT 0;
CREATE INDEX scoring_queue_priority_idx ON scoring_queue (priority, available_at, transaction_id);

-- The live desk and system status count what arrived in the last minutes by
-- arrival time. Without this every refresh scanned the whole table.
CREATE INDEX IF NOT EXISTS tx_ingested_idx ON transactions (ingested_at DESC);

-- Workers say they are alive, so readiness can tell "idle" from "dead".
CREATE TABLE worker_heartbeats (
    worker_id           text        PRIMARY KEY,
    host                text        NOT NULL,
    pid                 int         NOT NULL,
    started_at          timestamptz NOT NULL,
    seen_at             timestamptz NOT NULL DEFAULT now(),
    processed           bigint      NOT NULL DEFAULT 0,
    failed              bigint      NOT NULL DEFAULT 0
);

GRANT SELECT, INSERT, UPDATE ON alert_budget_days TO riskradar_app;
GRANT SELECT, INSERT, UPDATE ON alert_deferrals TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE alert_deferrals_id_seq TO riskradar_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO riskradar_app;

INSERT INTO app_config (key, value) VALUES
    -- Any one hour may use this multiple of an even share of the day's budget.
    -- 3.0 at 75 a day: at most 10 alerts in an hour, so a bad hour cannot spend
    -- the day and a flood reaches the desk as a trickle.
    ('alert_budget_hourly_burst', '3.0'),
    -- Off only for a measurement that must see every alert the thresholds imply.
    ('alert_budget_enforced', 'true'),
    -- How long a deferred alert waits before it is recorded as never reviewed.
    ('alert_deferral_hours', '24')
ON CONFLICT (key) DO NOTHING;

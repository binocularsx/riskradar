-- Risk Radar — schema v1
-- Owned by the migration role. The application role receives narrow grants in 0002.
--
-- Design references: PRD §6 (domain model), §10 (data & privacy), §13 (security & audit).
-- Decisions: D9 (dimensionality), D9a (integer kobo), D9b (dual timestamps),
--            D13 (decision->alert->case), D19 (subject vs account), D21 (auth_result),
--            D22 (account context stamped, not joined), D12c (audit integrity).

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

-- D9: three orthogonal dimensions, never one conflated enum.
CREATE TYPE channel        AS ENUM ('MOBILE_APP','WEB','USSD','POS','ATM','AGENT','BRANCH','API');
CREATE TYPE instrument     AS ENUM ('CARD','ACCOUNT_TRANSFER','CASH','WALLET');
CREATE TYPE rail           AS ENUM ('NIP','NEFT','RTGS','CARD_SCHEME','INTRABANK','ATM_NETWORK');

-- D21: an event is an attempt, not a success.
CREATE TYPE auth_result    AS ENUM ('APPROVED','DECLINED','FAILED','REVERSED');

-- D22: stamped account context.
CREATE TYPE product_type   AS ENUM ('SAVINGS','CURRENT','DOMICILIARY','WALLET');

-- D7: the typed advisory decision. Risk Radar produces this; it never enforces it.
CREATE TYPE decision_kind  AS ENUM ('ALLOW','MONITOR','REVIEW','HOLD');
CREATE TYPE risk_level     AS ENUM ('LOW','MEDIUM','HIGH','CRITICAL');

-- D13b: case state machine.
CREATE TYPE case_state     AS ENUM ('OPEN','UNDER_REVIEW','ESCALATED','CLOSED');
CREATE TYPE case_outcome   AS ENUM ('CONFIRMED_FRAUD','FALSE_POSITIVE','INCONCLUSIVE');

-- D12b: four roles plus a non-login system principal so audit actor is never null.
CREATE TYPE user_role      AS ENUM ('ANALYST','FRAUD_OPS_LEAD','INFOSEC_ANALYST','ADMIN','SYSTEM');

-- D11a: rules escalate, override or suppress. Severity is ordinal, never points.
CREATE TYPE rule_power     AS ENUM ('ESCALATE','OVERRIDE','SUPPRESS');
CREATE TYPE signal_severity AS ENUM ('LOW','MEDIUM','HIGH','CRITICAL');

CREATE TYPE escalation_target AS ENUM ('INFOSEC','FRAUD_OPS');

-- ---------------------------------------------------------------------------
-- Identity, sessions, API keys
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id                  bigserial PRIMARY KEY,
    email               text        NOT NULL UNIQUE,
    display_name        text        NOT NULL,
    -- Argon2id. NULL only for the SYSTEM principal, which can never log in.
    password_hash       text,
    role                user_role   NOT NULL,
    -- D12a: TOTP. Mandatory for FRAUD_OPS_LEAD and ADMIN, default-on for ANALYST.
    totp_secret         text,
    totp_enabled        boolean     NOT NULL DEFAULT false,
    -- D12: the OIDC seam. The socket, not the integration.
    external_idp_subject text UNIQUE,
    is_system           boolean     NOT NULL DEFAULT false,
    active              boolean     NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT system_principal_cannot_log_in
        CHECK (NOT is_system OR (password_hash IS NULL AND totp_secret IS NULL))
);

-- D12: server-side sessions in Postgres. Revocation is a DELETE.
-- Never a JWT in localStorage: XSS-readable and unrevocable before expiry.
CREATE TABLE sessions (
    id                  text        PRIMARY KEY,          -- SHA-256 of the cookie value
    user_id             bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    -- D27: 12h idle, 24h absolute.
    idle_expires_at     timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    mfa_satisfied       boolean     NOT NULL DEFAULT false,
    user_agent          text,
    ip_region           text
);
CREATE INDEX sessions_user_idx ON sessions(user_id);

-- FR-001: ingestion is authenticated by API key. Hashed, never stored raw.
CREATE TABLE api_keys (
    id                  bigserial PRIMARY KEY,
    name                text        NOT NULL,
    key_hash            text        NOT NULL UNIQUE,      -- SHA-256 of the presented key
    active              boolean     NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_used_at        timestamptz
);

-- ---------------------------------------------------------------------------
-- Account dimension (D22)
-- ---------------------------------------------------------------------------
-- Mutable. Exists for the analyst UI baseline panel (FR-024) and cross-subject
-- lookup. riskradar.features MUST NEVER read this table: joining it would make a
-- six-week-old decision reproduce against today's dormancy (defeating G4) and
-- would leak post-fraud account state backwards into training.

CREATE TABLE accounts (
    account_token       text        PRIMARY KEY,
    subject_token       text        NOT NULL,
    product_type        product_type NOT NULL,
    origin_sol_id       text        NOT NULL,
    account_opened_at   timestamptz NOT NULL,
    last_activity_at    timestamptz,
    display_name        text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX accounts_subject_idx ON accounts(subject_token);

-- ---------------------------------------------------------------------------
-- Transactions (PRD §6.1)
-- ---------------------------------------------------------------------------

CREATE TABLE transactions (
    id                  bigserial PRIMARY KEY,
    -- FR-002: caller-supplied idempotency key.
    transaction_ref     text        NOT NULL UNIQUE,

    -- D9b: two timestamps. All behavioural features use occurred_at.
    -- Scoring late arrivals on ingested_at manufactures a fake velocity spike.
    occurred_at         timestamptz NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now(),

    -- D9a: kobo. Integer. Never floating point, including in aggregates.
    amount_minor        bigint      NOT NULL CHECK (amount_minor >= 0),
    currency            char(3)     NOT NULL,

    channel             channel     NOT NULL,
    instrument          instrument  NOT NULL,
    rail                rail        NOT NULL,

    -- D19: two identity levels. Both one-way HMAC-SHA256 with the pepper.
    subject_token       text        NOT NULL,   -- the customer  -> case correlation
    account_token       text        NOT NULL,   -- the account   -> behavioural baseline
    beneficiary_token   text,
    beneficiary_subject_token text,             -- D25: ships, never populated in v1

    device_token        text,                   -- channel layer (§9.1)
    ip_region           text,                   -- channel layer, coarse. Never a raw IP
    merchant_category   text,                   -- switch layer, POS/card only

    -- D21: without this, card testing is unrepresentable and reversals count as money moved.
    auth_result         auth_result NOT NULL,
    decline_reason      text,

    display_name        text,                   -- obviously-synthetic, analyst UI only

    -- D22: account context stamped at the boundary, immutable thereafter.
    -- Facts (timestamps), not derived values: age and dormancy are derived at
    -- feature time from occurred_at, which is point-in-time correct by construction.
    account_opened_at   timestamptz,
    last_activity_at    timestamptz,
    product_type        product_type,
    origin_sol_id       text,

    -- D8d: replayed transactions do not raise alerts unless explicitly requested.
    is_replay           boolean     NOT NULL DEFAULT false,
    raise_alerts        boolean     NOT NULL DEFAULT true,

    CONSTRAINT decline_reason_only_when_not_approved
        CHECK (decline_reason IS NULL OR auth_result <> 'APPROVED')
);

-- Behavioural feature queries: always (identity, occurred_at DESC).
CREATE INDEX tx_account_time_idx     ON transactions(account_token, occurred_at DESC);
CREATE INDEX tx_subject_time_idx     ON transactions(subject_token, occurred_at DESC);
CREATE INDEX tx_beneficiary_time_idx ON transactions(beneficiary_token, occurred_at DESC)
    WHERE beneficiary_token IS NOT NULL;
CREATE INDEX tx_device_time_idx      ON transactions(device_token, occurred_at DESC)
    WHERE device_token IS NOT NULL;
CREATE INDEX tx_occurred_idx         ON transactions(occurred_at DESC);

-- FR-003: reject, never coerce. Raw body preserved with the validation error.
CREATE TABLE dead_letter (
    id                  bigserial PRIMARY KEY,
    received_at         timestamptz NOT NULL DEFAULT now(),
    api_key_id          bigint REFERENCES api_keys(id),
    raw_body            text        NOT NULL,
    validation_error    text        NOT NULL
);
CREATE INDEX dead_letter_time_idx ON dead_letter(received_at DESC);

-- ---------------------------------------------------------------------------
-- Work queue (D8a) — Postgres IS the queue. No Kafka, no Redis, no Celery.
-- ---------------------------------------------------------------------------

CREATE TABLE scoring_queue (
    transaction_id      bigint PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
    enqueued_at         timestamptz NOT NULL DEFAULT now(),
    available_at        timestamptz NOT NULL DEFAULT now(),
    attempts            int         NOT NULL DEFAULT 0,
    last_error          text
);
-- The claim query orders by available_at; SKIP LOCKED does the rest.
CREATE INDEX scoring_queue_available_idx ON scoring_queue(available_at, transaction_id);

-- ---------------------------------------------------------------------------
-- Versioned configuration: models, rulesets, thresholds (D11c, D15a, FR-040/041/042)
-- ---------------------------------------------------------------------------

CREATE TABLE model_versions (
    id                  bigserial PRIMARY KEY,
    name                text        NOT NULL,
    version             text        NOT NULL,
    artifact_hash       text        NOT NULL,
    artifact_path       text,
    feature_spec_version text       NOT NULL,
    training_snapshot_id text,
    metrics             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    calibration         text,                    -- 'isotonic' | 'platt' | 'none'
    trained_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    is_active           boolean     NOT NULL DEFAULT false,
    promoted_at         timestamptz,
    promoted_by         bigint REFERENCES users(id),
    UNIQUE (name, version)
);
-- Exactly one active model at a time. Promotion is repointing this, not a redeploy.
CREATE UNIQUE INDEX model_versions_single_active ON model_versions((is_active)) WHERE is_active;

CREATE TABLE rulesets (
    id                  bigserial PRIMARY KEY,
    version             int         NOT NULL UNIQUE,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          bigint REFERENCES users(id),
    is_active           boolean     NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX rulesets_single_active ON rulesets((is_active)) WHERE is_active;

-- FR-040: enable/disable and retune without a code change.
CREATE TABLE rule_configs (
    id                  bigserial PRIMARY KEY,
    ruleset_id          bigint      NOT NULL REFERENCES rulesets(id) ON DELETE CASCADE,
    code                text        NOT NULL,
    power               rule_power  NOT NULL,
    severity            signal_severity NOT NULL,
    enabled             boolean     NOT NULL DEFAULT true,
    params              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ruleset_id, code)
);

-- D11d: thresholds are derived backwards from the alert budget, never intuition.
CREATE TABLE threshold_sets (
    id                  bigserial PRIMARY KEY,
    version             int         NOT NULL UNIQUE,
    p_monitor           numeric(8,6) NOT NULL,
    p_review            numeric(8,6) NOT NULL,
    p_hold              numeric(8,6) NOT NULL,
    alert_min_level     risk_level  NOT NULL DEFAULT 'MEDIUM',
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          bigint REFERENCES users(id),
    is_active           boolean     NOT NULL DEFAULT false,
    CONSTRAINT thresholds_ordered CHECK (p_monitor <= p_review AND p_review <= p_hold)
);
CREATE UNIQUE INDEX threshold_sets_single_active ON threshold_sets((is_active)) WHERE is_active;

-- Runtime configuration that is not a threshold (correlation window, alert budget...).
CREATE TABLE app_config (
    key                 text PRIMARY KEY,
    value               jsonb       NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          bigint REFERENCES users(id)
);

-- ---------------------------------------------------------------------------
-- Decision -> Alert -> Case (D13)
-- ---------------------------------------------------------------------------

-- D7a: written before any alert is raised. Immutable. This is what makes a
-- decision reproducible months later (G4).
CREATE TABLE decisions (
    id                  bigserial PRIMARY KEY,
    transaction_id      bigint      NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    decided_at          timestamptz NOT NULL DEFAULT now(),

    p_fraud             numeric(8,6) NOT NULL CHECK (p_fraud >= 0 AND p_fraud <= 1),
    -- D11b: presentation artefact for sorting. NOT a decision input.
    score_0_100         int         NOT NULL CHECK (score_0_100 BETWEEN 0 AND 100),
    decision            decision_kind NOT NULL,
    risk_level          risk_level  NOT NULL,

    model_version_id    bigint REFERENCES model_versions(id),
    ruleset_id          bigint      NOT NULL REFERENCES rulesets(id),
    threshold_set_id    bigint      NOT NULL REFERENCES threshold_sets(id),
    feature_spec_version text       NOT NULL,

    features            jsonb       NOT NULL,   -- the full feature snapshot
    signals             jsonb       NOT NULL,   -- [{code, severity, power, evidence{}}]
    attributions        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    policy_trace        jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- FR-017: rule-only mode is never a silent degradation.
    rule_only_mode      boolean     NOT NULL DEFAULT false,
    latency_ms          int
);
CREATE INDEX decisions_time_idx ON decisions(decided_at DESC);
CREATE INDEX decisions_level_idx ON decisions(risk_level, decided_at DESC);

-- D13a: the investigative container. Mutable. Holds many alerts.
CREATE TABLE cases (
    id                  bigserial PRIMARY KEY,
    subject_token       text        NOT NULL,        -- D19: the customer, not the account
    state               case_state  NOT NULL DEFAULT 'OPEN',
    outcome             case_outcome,
    risk_level          risk_level  NOT NULL,        -- max over correlated alerts
    opened_at           timestamptz NOT NULL DEFAULT now(),
    last_alert_at       timestamptz NOT NULL DEFAULT now(),
    correlation_expires_at timestamptz NOT NULL,     -- opened_at + window (config)
    assignee_id         bigint REFERENCES users(id),
    escalated_to        escalation_target,
    closed_at           timestamptz,
    closed_by           bigint REFERENCES users(id),
    alert_count         int         NOT NULL DEFAULT 0,
    CONSTRAINT outcome_only_when_resolved
        CHECK (outcome IS NULL OR state IN ('UNDER_REVIEW','ESCALATED','CLOSED')),
    CONSTRAINT closed_requires_outcome
        CHECK (state <> 'CLOSED' OR outcome IS NOT NULL)
);
CREATE INDEX cases_state_idx   ON cases(state, last_alert_at DESC);
CREATE INDEX cases_subject_idx ON cases(subject_token);
-- The correlation lookup: one open case per subject inside the window.
CREATE UNIQUE INDEX cases_open_subject_uniq ON cases(subject_token)
    WHERE state IN ('OPEN','UNDER_REVIEW','ESCALATED');

-- D13: an observation. Observations do not change their minds.
CREATE TABLE alerts (
    id                  bigserial PRIMARY KEY,
    decision_id         bigint      NOT NULL UNIQUE REFERENCES decisions(id) ON DELETE CASCADE,
    transaction_id      bigint      NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    case_id             bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    subject_token       text        NOT NULL,
    risk_level          risk_level  NOT NULL,
    score_0_100         int         NOT NULL,
    raised_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX alerts_case_idx ON alerts(case_id, raised_at DESC);
CREATE INDEX alerts_time_idx ON alerts(raised_at DESC);

CREATE TABLE case_notes (
    id                  bigserial PRIMARY KEY,
    case_id             bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    author_id           bigint      NOT NULL REFERENCES users(id),
    body                text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX case_notes_case_idx ON case_notes(case_id, created_at);

-- ---------------------------------------------------------------------------
-- SSE durable event log (FR-030/031)
-- ---------------------------------------------------------------------------
-- D14a: LISTEN/NOTIFY carries IDs only — 8000-byte cap, non-durable. The table is
-- the record; the notification is only a doorbell. Last-Event-ID replays from here.

CREATE TABLE stream_events (
    id                  bigserial PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    event_type          text        NOT NULL,
    payload             jsonb       NOT NULL
);
CREATE INDEX stream_events_time_idx ON stream_events(created_at DESC);

-- ---------------------------------------------------------------------------
-- Audit log (D12c) — append-only, hash-chained
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    id                  bigserial PRIMARY KEY,
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    -- "Analyst cleared this case" means nothing if anyone can assert they are any
    -- analyst. Actor is NEVER null; system actions use the SYSTEM principal.
    actor_user_id       bigint      NOT NULL REFERENCES users(id),
    action              text        NOT NULL,
    object_type         text        NOT NULL,
    object_id           text,
    from_state          text,
    to_state            text,
    payload             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash           text        NOT NULL,
    hash                text        NOT NULL
);
CREATE INDEX audit_object_idx ON audit_log(object_type, object_id, occurred_at);
CREATE INDEX audit_time_idx   ON audit_log(occurred_at DESC);

-- Belt and braces alongside the role grants in 0002. The grants are the real
-- control; this trigger makes the intent explicit and survives a mis-grant.
CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();

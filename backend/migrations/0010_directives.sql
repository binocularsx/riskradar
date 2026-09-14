-- WP-07 / D74: a decision a bank can act on (base PRD FR-301 seam, FR-305).
--
-- Risk Radar still never blocks, holds or declines anything (D7, D69a). What
-- changes is the shape of what it hands the bank. An advisory band on a
-- dashboard cannot be wired into a payment switch; a directive can:
--
--   * idempotent: one per decision, with a reference the bank can quote back;
--   * time-limited: past expires_at it must not be acted on, and the written
--     fail-open policy says what the bank does instead;
--   * mode-stamped: SHADOW (record what you would have done, change nothing)
--     or LIVE — and LIVE is refused by the schema without a named sponsor's
--     signature on the policy, which is D69a in a constraint;
--   * provable: the moment the bank first fetched it and what the bank says it
--     did with it are recorded, because the APP framework puts the loss on the
--     institution that failed to flag or freeze in time.

CREATE TYPE enforcement_mode  AS ENUM ('SHADOW', 'LIVE');
CREATE TYPE directive_action  AS ENUM ('APPROVE', 'APPROVE_AND_MONITOR', 'HOLD_FOR_REVIEW', 'DECLINE');
CREATE TYPE directive_ack     AS ENUM ('APPLIED', 'NOT_APPLIED', 'SHADOW_RECORDED');

CREATE TABLE enforcement_policies (
    id                  bigserial   PRIMARY KEY,
    version             int         NOT NULL UNIQUE,
    mode                enforcement_mode NOT NULL,
    -- How long a directive may be acted on after it is issued.
    ttl_seconds         int         NOT NULL CHECK (ttl_seconds BETWEEN 5 AND 3600),
    -- What the bank does when a directive is late, expired or missing. Fail
    -- open: a fraud engine that is down must not stop a country's payments.
    fail_open_action    directive_action NOT NULL DEFAULT 'APPROVE',
    policy_text         text        NOT NULL,
    signed_by           text,
    signed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          bigint REFERENCES users(id),
    is_active           boolean     NOT NULL DEFAULT false,
    CONSTRAINT live_needs_a_signed_policy
        CHECK (mode = 'SHADOW' OR (signed_by IS NOT NULL AND signed_at IS NOT NULL)),
    CONSTRAINT fail_open_means_open
        CHECK (fail_open_action IN ('APPROVE', 'APPROVE_AND_MONITOR'))
);
CREATE UNIQUE INDEX enforcement_policies_single_active ON enforcement_policies((is_active)) WHERE is_active;

CREATE TABLE directives (
    id                  bigserial   PRIMARY KEY,
    directive_ref       uuid        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    decision_id         bigint      NOT NULL UNIQUE REFERENCES decisions(id) ON DELETE CASCADE,
    transaction_id      bigint      NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    action              directive_action NOT NULL,
    mode                enforcement_mode NOT NULL,
    policy_version      int         NOT NULL REFERENCES enforcement_policies(version),
    fail_open_action    directive_action NOT NULL,
    issued_at           timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    -- The record of the bank being told.
    first_delivered_at  timestamptz,
    delivery_count      int         NOT NULL DEFAULT 0,
    acknowledged_at     timestamptz,
    ack_action          directive_ack,
    ack_taken_at        timestamptz,
    ack_reason          text,
    CONSTRAINT expires_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT ack_is_complete CHECK ((acknowledged_at IS NULL) = (ack_action IS NULL))
);
CREATE INDEX directives_issued_idx ON directives(issued_at DESC);

-- A directive is an instruction once given: only its delivery and its
-- acknowledgement may be filled in, each once.
CREATE OR REPLACE FUNCTION directive_moves_forward_only() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.directive_ref, NEW.decision_id, NEW.transaction_id, NEW.action, NEW.mode,
           NEW.policy_version, NEW.fail_open_action, NEW.issued_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.directive_ref, OLD.decision_id, OLD.transaction_id, OLD.action, OLD.mode,
           OLD.policy_version, OLD.fail_open_action, OLD.issued_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'directives: an issued directive cannot be rewritten';
    END IF;
    IF OLD.first_delivered_at IS NOT NULL AND NEW.first_delivered_at IS DISTINCT FROM OLD.first_delivered_at THEN
        RAISE EXCEPTION 'directives: first delivery is recorded once';
    END IF;
    IF OLD.acknowledged_at IS NOT NULL
       AND ROW(NEW.acknowledged_at, NEW.ack_action, NEW.ack_taken_at, NEW.ack_reason)
           IS DISTINCT FROM ROW(OLD.acknowledged_at, OLD.ack_action, OLD.ack_taken_at, OLD.ack_reason) THEN
        RAISE EXCEPTION 'directives: an acknowledgement is recorded once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER directives_forward_only BEFORE UPDATE ON directives
    FOR EACH ROW EXECUTE FUNCTION directive_moves_forward_only();

GRANT SELECT, INSERT, UPDATE ON enforcement_policies, directives TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE enforcement_policies_id_seq, directives_id_seq TO riskradar_app;

-- Version 1: shadow mode, as the base PRD and D69a both say a bank pilot begins.
INSERT INTO enforcement_policies (version, mode, ttl_seconds, fail_open_action, is_active, policy_text) VALUES (
    1, 'SHADOW', 120, 'APPROVE', true,
    'Shadow mode. Risk Radar issues a directive for every live decision; the bank records what it would have done '
    'and changes nothing. If a directive is not received, is received after it expires, or Risk Radar is '
    'unreachable, the payment proceeds (fail open). No directive may be enforced until a bank sponsor signs a '
    'LIVE policy version (D69a).'
);

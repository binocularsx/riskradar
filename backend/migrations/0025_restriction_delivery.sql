-- D97: the restriction delivery pipeline (implementation plan, next phase of D93).
--
-- D93 let a lead approve a fraud finding that *asks the bank* to restrict a
-- customer's account — stop debits, block a channel, freeze a card, block a
-- destination — and recorded the request on the approval. It went nowhere:
-- "recorded on the approval and not yet delivered anywhere". This builds the
-- three stages the plan named — outbox, execution, reconciliation — while
-- keeping D7 exactly: Risk Radar never restricts anything. It records the
-- recommendation, dispatches it to the bank, and records what the bank did.
--
-- The shape reuses two patterns already in the system:
--
--   * a transactional outbox, like the industry watch-list (D75): an order and
--     its outbox row are written in the *same* transaction as the approval, so
--     an approved restriction can never exist without its message, nor a message
--     without a rolled-back approval. A sweep hands due messages to the bank's
--     restriction connector, which is unconnected by default and waits visibly.
--
--   * delivery-and-acknowledgement recorded once, like a directive (D74): the
--     order records when the bank was first told, and the bank's reconciliation
--     — APPLIED / NOT_APPLIED / REJECTED — exactly once. An order is a
--     recommendation once issued; a trigger lets only delivery and the ack fill
--     in, and forbids rewriting the recommendation itself.

CREATE TYPE restriction_action AS ENUM (
    'DEBIT_RESTRICTION', 'CHANNEL_RESTRICTION', 'CARD_FREEZE', 'BENEFICIARY_RESTRICTION'
);

-- What the bank did about the recommendation. The whole of reconciliation.
CREATE TYPE restriction_ack AS ENUM ('APPLIED', 'NOT_APPLIED', 'REJECTED');

CREATE TABLE restriction_orders (
    id                 bigserial   PRIMARY KEY,
    -- The bank-facing handle; a uuid so it carries no internal id.
    restriction_ref    uuid        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    -- Cascade with the case and its submission: cases and their children are
    -- deletable for retention (D27, D32); only audit_log is never removed. An
    -- order without its submission or case would be an orphan the retention
    -- policy could not clear.
    submission_id      bigint      NOT NULL REFERENCES fraud_submissions(id) ON DELETE CASCADE,
    case_id            bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    action             restriction_action NOT NULL,
    -- Tokens only (D9c, D9d): the bank maps a token to the account, card or
    -- destination on its own side, where it holds the pepper. Risk Radar never
    -- resolves one back.
    account_token      text,
    beneficiary_token  text,
    channel            text,        -- only for CHANNEL_RESTRICTION
    reason             text,
    -- The lead whose approval created it — the human authority behind the ask.
    approved_by        bigint      NOT NULL REFERENCES users(id),
    issued_at          timestamptz NOT NULL DEFAULT now(),
    -- The record of the bank being told (set by dispatch, or by the poll feed).
    first_delivered_at timestamptz,
    delivery_count     int         NOT NULL DEFAULT 0,
    -- Reconciliation: what the bank did, recorded once.
    acknowledged_at    timestamptz,
    ack_outcome        restriction_ack,
    ack_reason         text,
    ack_taken_at       timestamptz,
    CONSTRAINT restriction_names_a_target
        CHECK (account_token IS NOT NULL OR beneficiary_token IS NOT NULL),
    CONSTRAINT restriction_ack_complete
        CHECK ((acknowledged_at IS NULL) = (ack_outcome IS NULL))
);
CREATE INDEX restriction_orders_case_idx ON restriction_orders (case_id, issued_at);
CREATE INDEX restriction_orders_feed_idx ON restriction_orders (id);

-- A recommendation once issued: only delivery and the acknowledgement may fill
-- in, each once. The ask itself cannot be rewritten (the same posture as the
-- directive trigger in 0010, and the append-only rule everywhere else).
CREATE OR REPLACE FUNCTION restriction_moves_forward_only() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.restriction_ref, NEW.submission_id, NEW.case_id, NEW.action,
           NEW.account_token, NEW.beneficiary_token, NEW.channel, NEW.approved_by, NEW.issued_at)
       IS DISTINCT FROM
       ROW(OLD.restriction_ref, OLD.submission_id, OLD.case_id, OLD.action,
           OLD.account_token, OLD.beneficiary_token, OLD.channel, OLD.approved_by, OLD.issued_at) THEN
        RAISE EXCEPTION 'restriction_orders: an issued recommendation cannot be rewritten';
    END IF;
    IF OLD.acknowledged_at IS NOT NULL
       AND ROW(NEW.acknowledged_at, NEW.ack_outcome, NEW.ack_reason, NEW.ack_taken_at)
           IS DISTINCT FROM ROW(OLD.acknowledged_at, OLD.ack_outcome, OLD.ack_reason, OLD.ack_taken_at) THEN
        RAISE EXCEPTION 'restriction_orders: an acknowledgement is recorded once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER restriction_orders_forward_only
    BEFORE UPDATE ON restriction_orders
    FOR EACH ROW EXECUTE FUNCTION restriction_moves_forward_only();

-- The transactional outbox for the push to the bank's connector.
CREATE TABLE restriction_outbox (
    id              bigserial   PRIMARY KEY,
    order_id        bigint      NOT NULL REFERENCES restriction_orders(id) ON DELETE CASCADE,
    payload         jsonb       NOT NULL,
    status          text        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    attempts        int         NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    external_ref    text,
    UNIQUE (order_id)
);
CREATE INDEX restriction_outbox_due_idx ON restriction_outbox (next_attempt_at) WHERE status = 'PENDING';

GRANT SELECT, INSERT, UPDATE ON restriction_orders TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE restriction_orders_id_seq TO riskradar_app;
GRANT SELECT, INSERT, UPDATE ON restriction_outbox TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE restriction_outbox_id_seq TO riskradar_app;

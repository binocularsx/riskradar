-- D106: confirmed fraud tells the customer's account manager, through the same
-- durable outbox the bank's restrictions ride on (D75, D97).
--
-- Until now an approved fraud finding did three things: it set the outcome, it
-- promoted the destinations to KNOWN_MULE, and it dispatched whatever
-- restrictions the finding happened to ask for. Nobody who owns the customer
-- relationship was told anything. The desk knew; the person who would have to
-- call that customer did not, and found out when the customer called them.
--
-- The account manager is treated as an external party, exactly like the bank:
-- no login, no role inside Risk Radar, a message delivered to their system and
-- retried until it lands. That keeps D12b intact — no new human role is added
-- to a separation-of-duties model that four roles were carefully balanced in.
--
-- One report per approved submission, enforced by the unique constraint rather
-- than by remembering to check: an approval replayed after a crash must not
-- tell the account manager twice.

CREATE TABLE account_manager_reports (
    id              bigserial   PRIMARY KEY,
    -- The recipient-facing handle; a uuid so it carries no internal id.
    report_ref      uuid        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    -- Cascade with the case and its submission, for the same retention reason
    -- as restriction_orders (D27, D32): audit_log is the only permanent record.
    case_id         bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    submission_id   bigint      NOT NULL REFERENCES fraud_submissions(id) ON DELETE CASCADE,
    subject_token   text        NOT NULL,
    payload         jsonb       NOT NULL,
    status          text        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    attempts        int         NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    external_ref    text,
    -- One report per approved finding. An approval replayed after a crash finds
    -- this row already present and writes nothing.
    UNIQUE (submission_id)
);

CREATE INDEX account_manager_reports_due_idx
    ON account_manager_reports (next_attempt_at) WHERE status = 'PENDING';
CREATE INDEX account_manager_reports_case_idx ON account_manager_reports (case_id);

GRANT SELECT, INSERT, UPDATE ON account_manager_reports TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE account_manager_reports_id_seq TO riskradar_app;

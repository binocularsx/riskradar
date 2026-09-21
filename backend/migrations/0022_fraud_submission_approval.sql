-- D93: an analyst proposes fraud; a different authorised lead decides it.
--
-- Until now `POST /v1/cases/{id}/disposition` let an analyst write
-- CONFIRMED_FRAUD straight onto the case, and that outcome is both the bank's
-- fraud record and the model's training label (D13c). One person could
-- therefore create a customer-impacting fraud finding, and the same person's
-- judgement became ground truth. The implementation plan retires that: the
-- analyst's verdict becomes a *submission*, and a lead who is not the
-- submitter approves, rejects or returns it.
--
-- Three separations the plan asks for, made structural rather than procedural:
--
--   * proposal (fraud_submissions) is not adjudication (its decision columns)
--     and neither is the later real-world outcome (cases.outcome stays, but is
--     written only by an approval);
--   * the submission names exactly what it asks for — the accounts and the
--     restriction scope — so a lead approves a specific request, not a mood;
--   * the submission pins the case version and the evidence it was read
--     against, so an approval cannot silently apply to a case that has moved.

CREATE TYPE submission_state AS ENUM ('PENDING', 'APPROVED', 'REJECTED', 'RETURNED', 'SUPERSEDED');

CREATE TABLE fraud_submissions (
    id                  bigserial   PRIMARY KEY,
    case_id             bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    -- What is proposed. INCONCLUSIVE and FALSE_POSITIVE travel the same path:
    -- a lead signs off every ending, so the desk has one route, not two.
    proposed_outcome    case_outcome NOT NULL,
    rationale           text        NOT NULL CHECK (length(rationale) >= 20),
    -- The evidence the analyst read, by id and by the decision that produced it,
    -- so the lead reviews a known snapshot (plan §6.3).
    evidence            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- What the analyst asks the bank to do, if anything. Empty for no action.
    restrictions        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    case_version        int         NOT NULL,
    state               submission_state NOT NULL DEFAULT 'PENDING',
    submitted_by        bigint      NOT NULL REFERENCES users(id),
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    decided_by          bigint      REFERENCES users(id),
    decided_at          timestamptz,
    decision_reason     text,
    -- The separation that matters, in the schema rather than in a code review.
    CONSTRAINT decider_is_not_submitter CHECK (decided_by IS NULL OR decided_by <> submitted_by),
    CONSTRAINT decided_together CHECK ((decided_by IS NULL) = (decided_at IS NULL))
);

-- One pending submission per case: a second proposal supersedes the first
-- rather than giving a lead two things to approve.
CREATE UNIQUE INDEX fraud_submissions_one_pending ON fraud_submissions (case_id) WHERE state = 'PENDING';
CREATE INDEX fraud_submissions_queue_idx ON fraud_submissions (state, submitted_at);
CREATE INDEX fraud_submissions_case_idx ON fraud_submissions (case_id, submitted_at DESC);

-- Optimistic concurrency (plan §2.3, §4.1): every material change to a case
-- bumps this, and a submission or approval quotes the version it read.
ALTER TABLE cases ADD COLUMN version int NOT NULL DEFAULT 1;
-- How the case reached its owner, for the assignment service the plan asks for.
ALTER TABLE cases ADD COLUMN assigned_at timestamptz;
ALTER TABLE cases ADD COLUMN assignment_reason text;

GRANT SELECT, INSERT, UPDATE ON fraud_submissions TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE fraud_submissions_id_seq TO riskradar_app;

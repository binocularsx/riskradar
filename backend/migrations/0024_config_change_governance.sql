-- D96: a second approver on the changes that tune detection (plan §3, FR-308).
--
-- D69e recorded the gap plainly: a rule or threshold change carried
-- `approved_by`, but that was *the person who made the change*. One
-- administrator could loosen a threshold or disable a rule alone, and the field
-- meant to record oversight recorded only themselves. D12b's whole argument is
-- that the account which tunes detection is set apart from the account which
-- clears what detection misses; this closes the remaining half of it — even
-- among administrators, tuning detection now takes two people.
--
-- The same maker-checker shape as fraud approval (D93), for the same reason:
-- the separation is a database CHECK, not a code review. An administrator
-- proposes a change; a *different* administrator holding the same permission
-- approves it; only then is a new ruleset or threshold version published, with
-- the approver recorded as the second signature the old `approved_by` pretended
-- to be.
--
-- Scope is the detection-tuning surface D69e names: rule updates and threshold
-- publications. Model promotion, list entries and user administration are the
-- obvious next surfaces and are recorded as such (D96d) rather than smuggled in.

CREATE TYPE config_change_type AS ENUM ('RULE_UPDATE', 'THRESHOLD_PUBLISH');

-- Reuses submission_state from 0022 (PENDING / APPROVED / REJECTED / RETURNED /
-- SUPERSEDED): the lifecycle is identical to a fraud submission's, and one
-- vocabulary for "a proposal a second person decides" is one vocabulary to learn.
CREATE TABLE config_change_requests (
    id              bigserial   PRIMARY KEY,
    change_type     config_change_type NOT NULL,
    -- What the change is against: a rule code, or the single 'thresholds' target.
    target          text        NOT NULL,
    -- A one-line human summary, so the approver's queue reads without unpacking
    -- the payload.
    summary         text        NOT NULL,
    -- The proposed change exactly as the API would have applied it directly
    -- (a RuleUpdateIn or a ThresholdsIn), so approval applies a known request.
    payload         jsonb       NOT NULL,
    -- What the proposer read it against — the active version and its values —
    -- so a reviewer sees the before, and a later reader sees what moved.
    before_snapshot jsonb       NOT NULL DEFAULT '{}'::jsonb,
    rationale       text        NOT NULL CHECK (length(rationale) >= 20),
    state           submission_state NOT NULL DEFAULT 'PENDING',
    proposed_by     bigint      NOT NULL REFERENCES users(id),
    proposed_at     timestamptz NOT NULL DEFAULT now(),
    decided_by      bigint      REFERENCES users(id),
    decided_at      timestamptz,
    decision_reason text,
    -- The ruleset or threshold version an approval produced, for the trail.
    applied_version int,
    -- The separation, in the schema rather than in a promise.
    CONSTRAINT config_decider_is_not_proposer
        CHECK (decided_by IS NULL OR decided_by <> proposed_by),
    CONSTRAINT config_decided_together
        CHECK ((decided_by IS NULL) = (decided_at IS NULL))
);

-- One pending proposal per target: a second proposal supersedes the first,
-- rather than leaving two approvers to race on the same rule.
CREATE UNIQUE INDEX config_change_one_pending
    ON config_change_requests (change_type, target) WHERE state = 'PENDING';
CREATE INDEX config_change_queue_idx ON config_change_requests (state, proposed_at);

GRANT SELECT, INSERT, UPDATE ON config_change_requests TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE config_change_requests_id_seq TO riskradar_app;

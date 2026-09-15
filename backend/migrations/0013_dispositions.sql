-- WP-08 / D80: spend the budget by tier (base PRD FR-303, O5).
--
-- Every alert used to go to an analyst. Some need no investigation to know what
-- to do: declined card probes call for the card to be blocked, and a takeover
-- sequence is what CBN's 24-hour flag and customer call exist for. The policy
-- layer now gives every actionable decision a disposition:
--
--   MACHINE_ACTION  named high-precision signals; the system takes the named
--                   action (hold directive, 24-hour flag) and a person
--                   confirms by contacting the customer
--   HUMAN_REVIEW    an analyst investigates
--   AUTO_CLOSE      recorded, no case; off in version 1 (measured: it would
--                   cost fraud value, D80)
--
-- Measured at 75 alerts a day (ml/artifacts/tiers.json): machine actions 24.1 a
-- day at precision 0.992, analyst reviews 75 -> 50.9 a day, detection and value
-- detected unchanged, because the freed capacity is kept, not refilled.

CREATE TABLE disposition_policies (
    id                          bigserial   PRIMARY KEY,
    version                     int         NOT NULL UNIQUE,
    machine_action_signals      text[]      NOT NULL,
    auto_close_enabled          boolean     NOT NULL DEFAULT false,
    -- A model-only alert at or below this probability would close unread.
    auto_close_max_probability  numeric(8,6),
    review_capacity_per_day     int         NOT NULL CHECK (review_capacity_per_day > 0),
    notes                       text        NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    created_by                  bigint REFERENCES users(id),
    is_active                   boolean     NOT NULL DEFAULT false,
    CONSTRAINT auto_close_needs_a_cut
        CHECK (NOT auto_close_enabled OR auto_close_max_probability IS NOT NULL)
);
CREATE UNIQUE INDEX disposition_policies_single_active ON disposition_policies((is_active)) WHERE is_active;

-- Written with the decision, like everything else about it (D7a).
ALTER TABLE decisions ADD COLUMN disposition text
    CHECK (disposition IN ('NONE', 'HUMAN_REVIEW', 'MACHINE_ACTION', 'AUTO_CLOSE'));
ALTER TABLE decisions ADD COLUMN disposition_policy_version int REFERENCES disposition_policies(version);

-- Who works a case. A machine-action case becomes HUMAN the moment an alert
-- needing review joins it; it never goes back.
ALTER TABLE cases ADD COLUMN handling text NOT NULL DEFAULT 'HUMAN' CHECK (handling IN ('HUMAN', 'MACHINE'));
CREATE INDEX cases_handling_idx ON cases(handling, state);

GRANT SELECT, INSERT, UPDATE ON disposition_policies TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE disposition_policies_id_seq TO riskradar_app;

INSERT INTO disposition_policies
    (version, is_active, machine_action_signals, auto_close_enabled, review_capacity_per_day, notes)
VALUES (1, true, ARRAY['CARD_TESTING_PROBES', 'ACCOUNT_TAKEOVER_SEQUENCE'], false, 51,
        'Machine actions: card-testing probes (hold directive) and takeover sequences (hold directive and '
        'the 24-hour flag). Analysts review the rest: 51 a day of the 75 at the D80 measurement. Auto-close '
        'off: closing the lowest fifth of model alerts lost no incident but cut fraud value detected from '
        '90.4% to 87.8%.');

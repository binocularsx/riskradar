-- D83: a case can be followed from alert to resolution, and every recommended
-- step can be done and recorded from the dashboard.
--
-- Three gaps this closes:
--
-- * An escalation said where a case went, not who sent it, when, or why, so a
--   team receiving it could not hand it back and nobody could measure how long
--   escalations wait.
-- * The recommended steps ("block the card", "notify the receiving bank") had
--   nowhere to be recorded. Risk Radar does not do them itself (D7); the analyst
--   does them in the bank's systems and records here that they were done, with
--   the result, so the case shows what has happened and what is left.
-- * A returned escalation needs to go back to the person who raised it.

ALTER TABLE cases
    ADD COLUMN escalated_by      bigint REFERENCES users(id),
    ADD COLUMN escalated_at      timestamptz,
    ADD COLUMN escalation_reason text;

CREATE TABLE case_actions (
    id          bigserial PRIMARY KEY,
    case_id     bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    action_code text        NOT NULL,
    result      text        NOT NULL,
    detail      text,
    actor_id    bigint      NOT NULL REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX case_actions_case_idx ON case_actions(case_id, created_at);

GRANT SELECT, INSERT ON case_actions TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE case_actions_id_seq TO riskradar_app;

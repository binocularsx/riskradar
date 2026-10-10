-- D92: which envelope a deferred alert belongs to, so the release respects
-- them too. Split from 0019, which was already applied and is recorded by its
-- hash; a migration that has run is never edited.

ALTER TABLE alert_deferrals ADD COLUMN rule_driven boolean NOT NULL DEFAULT false;
CREATE INDEX alert_deferrals_envelope_idx ON alert_deferrals (rule_driven, risk_level DESC, p_fraud DESC)
    WHERE state = 'WAITING';

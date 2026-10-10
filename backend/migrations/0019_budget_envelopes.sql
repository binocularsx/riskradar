-- D92: the day's budget is split, not spent rules-first.
--
-- Rule alerts used to be raised whatever they cost, and the model competed for
-- what was left (D61b). Measured (D91): at 75 a day the rules take 45 alerts at
-- precision 0.605 and the desk catches fewer incidents than the model alone
-- would at the same volume; at 60 a day the rules take 45 of 53 and recall
-- falls by a fifth. The rules still earn their place on fraud the model has
-- never seen, so the answer is a share, not a removal: each layer gets an
-- envelope, and neither can starve the other.
--
-- A veto (sanctions, a known mule) and a machine action are outside both
-- envelopes: they are raised whatever the day has spent, and counted.

ALTER TABLE alert_budget_days ADD COLUMN rule_raised  int NOT NULL DEFAULT 0;
ALTER TABLE alert_budget_days ADD COLUMN model_raised int NOT NULL DEFAULT 0;

-- Reasons a deferral can carry: the two envelopes join the day and the hour.
ALTER TABLE alert_deferrals DROP CONSTRAINT alert_deferrals_reason_check;
ALTER TABLE alert_deferrals ADD CONSTRAINT alert_deferrals_reason_check
    CHECK (reason IN ('DAILY_CAP', 'HOURLY_PACE', 'RULE_QUOTA', 'MODEL_QUOTA'));

INSERT INTO app_config (key, value) VALUES
    -- The share of the day's budget reserved for discretionary rule alerts.
    -- Set from ml/allocation_sweep.py (D92); 1.0 is the old rules-first rule.
    ('alert_budget_rule_share', '0.35')
ON CONFLICT (key) DO NOTHING;

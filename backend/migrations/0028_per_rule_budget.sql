-- D92 (the refinement it named unbuilt): per-rule budget shares.
--
-- D92 split the alert budget into two envelopes — 0.6 of the day to the rules,
-- the rest to the model — and noted the obvious next step: "one number for eight
-- rules of very different quality." Inside the rules envelope every rule draws
-- from one pool, so a low-precision rule that spikes (the velocity burst at
-- precision 0.607, firing on a busy trading day) can spend the envelope and
-- defer a high-precision rule behind it (the card-testing probes at precision
-- 1.000). A per-rule daily cap stops that: each rule may raise up to its own
-- measured share before it defers, and the alerts it defers wait behind the
-- other rules of its own kind, not in front of a better rule.
--
-- The caps are *budget* configuration, not detection configuration: they live in
-- app_config beside the budget's per-day and rule-share (D86), not on the
-- versioned ruleset, because changing a cap changes nobody's score — it changes
-- only how the day's alert budget is shared. Derivation is measured
-- (ml/rule_allocation.py); the seed publishes the result.

-- Per-rule tallies for the day, keyed by the rule that drove each rule alert.
-- A map so a new rule needs no migration — it simply appears as a new key.
ALTER TABLE alert_budget_days ADD COLUMN rule_counts jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Which rule drove a deferred rule alert, so release can honour that rule's own
-- cap rather than only the rules envelope. NULL for a model-driven deferral.
ALTER TABLE alert_deferrals ADD COLUMN budget_rule text;

-- The caps themselves: one app_config row holding a JSON map {rule_code: cap}.
-- A rule absent from the map is uncapped and draws only against the rules
-- envelope, exactly as before this change — so the default (an empty map) is the
-- old behaviour, and a cap is added only where a measurement supports one.
INSERT INTO app_config (key, value)
VALUES ('alert_budget_rule_caps', '{}'::jsonb)
ON CONFLICT (key) DO NOTHING;

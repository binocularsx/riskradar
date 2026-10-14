-- D69e: a rules inventory (enterprise base PRD FR-504, FR-507).
--
-- Every active rule carries an owner, the reason it exists, when it was last
-- approved and when it must next be reviewed. A rule with no owner is
-- "orphaned"; a rule past its review date is "overdue". Both are flagged by
-- GET /v1/admin/rules, alongside rules that have not fired in 30 days.
--
-- The columns live on rule_configs, so they travel with each ruleset version
-- exactly as enabled/severity/params do, and an old decision's ruleset still
-- says who owned the rule that fired on it.
--
-- Stated limit: approved_by records who made the change. There is no second
-- approver yet (maker-checker, base PRD FR-308, is not built).

ALTER TABLE rule_configs
    ADD COLUMN owner          text,
    ADD COLUMN rationale      text,
    ADD COLUMN approved_by    text,
    ADD COLUMN approved_at    timestamptz,
    ADD COLUMN next_review_at timestamptz;

-- Backfill the six v1 rules on an existing database. A fresh database gets the
-- same values from scripts/seed.py.
UPDATE rule_configs r SET
    owner          = v.owner,
    rationale      = v.rationale,
    approved_by    = 'team decision log',
    approved_at    = v.approved_at::timestamptz,
    next_review_at = v.approved_at::timestamptz + interval '90 days'
FROM (VALUES
    ('VELOCITY_BURST_1H',          'Chidera', 'D67: a burst of 5+ payments in an hour to a destination new to the bank; 10+ with no destination. Speed alone was mostly traders.', '2026-09-11'),
    ('CARD_TESTING_PROBES',        'Chidera', 'D21, D62b: refused small card attempts before a large one. Right 99.8% of the time.', '2026-09-09'),
    ('SANCTIONED_BENEFICIARY',     'Chidera', 'D11a: a sanctioned destination is not a matter of probability.', '2026-09-09'),
    ('KNOWN_MULE_BENEFICIARY',     'Chidera', 'D11a: a destination an analyst confirmed as fraudulent.', '2026-09-09'),
    ('PRE_REGISTERED_BENEFICIARY', 'Chidera', 'D11a: the customer set this payee up on purpose; the main false-alarm control.', '2026-09-09'),
    ('ESTABLISHED_PAYEE_NORMAL',   'Chidera', 'D64: a long-standing payee receiving a normal amount; never fires without a payee.', '2026-09-11')
) AS v(code, owner, rationale, approved_at)
WHERE r.code = v.code AND r.owner IS NULL;

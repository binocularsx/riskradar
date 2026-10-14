-- D82: four more fraud types.
--
-- A database seeded before D82 has an active ruleset without the four rules, so
-- nothing would ever evaluate them. This adds them to the active ruleset,
-- enabled, with the parameters and governance scripts/seed.py gives a fresh
-- database. On a fresh database there is no active ruleset yet when migrations
-- run, so this inserts nothing and the seed adds all rules as usual.
--
-- Changing a live ruleset in a migration skips the admin audit trail (FR-041),
-- so the change is stated here and in docs/decisions.md D82, and a rule an
-- administrator has already added by the same code is left untouched.

INSERT INTO rule_configs (ruleset_id, code, power, severity, enabled, params,
                          owner, rationale, approved_by, approved_at, next_review_at)
SELECT r.id, v.code, 'ESCALATE', 'HIGH', true, v.params::jsonb,
       'Chidera', v.rationale, 'team decision log', '2026-09-17'::timestamptz,
       '2026-09-17'::timestamptz + interval '90 days'
  FROM rulesets r
 CROSS JOIN (VALUES
    ('SCAM_BENEFICIARY_FANIN', '{"min_other_senders": 1, "max_beneficiary_age_days": 3, "min_amount_ratio": 1.5}',
     'D82: a new destination several other customers paid today; the collection account of a social-engineering scam.'),
    ('SIM_SWAP_TRANSFER', '{"within_hours": 12, "channels": ["USSD", "MOBILE_APP"], "min_amount_log10": 4.7}',
     'D82: a new destination within hours of the SIM changing; the USSD drain after a SIM swap.'),
    ('DORMANT_ACCOUNT_REACTIVATION', '{"min_dormant_days": 60, "min_amount_log10": 5.0}',
     'D82: a long-dormant account sending a large sum somewhere new.'),
    ('CARD_PRESENT_NEW_REGION_CASHOUT', '{"min_count_1h": 3}',
     'D82: card-present attempts in a run, in a region the customer has not used; a cloned card cashed out.')
 ) AS v(code, params, rationale)
 WHERE r.is_active
   AND NOT EXISTS (SELECT 1 FROM rule_configs c WHERE c.ruleset_id = r.id AND c.code = v.code);

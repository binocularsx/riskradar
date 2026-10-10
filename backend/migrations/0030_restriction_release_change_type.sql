-- D103: releasing a restriction joins the maker-checker surfaces (D96, D98, D99).
--
-- The mechanism is the one already built for detection tuning: propose, a
-- different person with the same authority decides, only an approval applies.
-- What differs is who holds that authority. A restriction belongs to a case, so
-- the second signature is the Fraud Ops lead's (cases:approve_fraud), not an
-- administrator's — an administrator cannot see a case at all (D12b).

ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'RESTRICTION_RELEASE';

-- D104: the rest of an account's life joins the maker-checker surfaces.
--
-- D99 made *creating* a user a two-signature act, because creating an ADMIN is
-- privilege escalation. Everything afterwards was left unbuilt, so an account's
-- role, its access and its second factor could not be changed at all — the only
-- way to remove somebody was to edit the table by hand, which leaves no audit
-- trail and no second pair of eyes.
--
-- The three changes here are the same act by other means. Granting a role hands
-- over a permission set (D12b). Disabling an account is how one administrator
-- would take sole control of the system. Re-issuing a second factor is what an
-- attacker holding one admin account would do to reach another. Each therefore
-- takes the signature of an administrator who did not propose it.

ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_ROLE_CHANGE';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_SET_ACTIVE';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_MFA_RESET';

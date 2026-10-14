-- D105: an administrator can edit an account, not merely its role and its life.
--
-- D104 added role, active and an authenticator re-issue. What an administrator
-- still could not do was change whether a person is asked for a code at all,
-- reset a forgotten password, or correct the email and the name on an account --
-- so a mistyped email meant the account was unreachable and unusable, with no
-- remedy inside the product.
--
-- All three are access changes, not profile trivia. The email *is* the login
-- identifier. A password reset lets whoever sets it sign in as that person. And
-- turning the second factor off removes a control. Each therefore takes the same
-- second signature as every other privileged change (D96, D98, D99, D104).

ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_SET_MFA';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_PASSWORD_RESET';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_PROFILE_UPDATE';

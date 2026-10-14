-- D99: the last maker-checker surface — creating a user (closes D98b, the third
-- surface named in D96d).
--
-- Creating a user is access control, not detection tuning, and it carries a
-- credential, which is why it waited for its own decision. Creating an ADMIN is
-- privilege escalation; one administrator should not be able to grant another
-- the keys alone. So user creation joins the maker-checker: one administrator
-- proposes the user, a different one approves, and only then does the account
-- exist.
--
-- The credential handling is the reason this is separate (see D99 for the full
-- argument): the password is hashed at proposal time so no plaintext is ever
-- stored, and the TOTP secret is generated at *approval* time so no authenticator
-- secret sits in a change-request payload waiting to be read.
--
-- Only the enum grows; the mechanism is unchanged.

ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'USER_CREATE';

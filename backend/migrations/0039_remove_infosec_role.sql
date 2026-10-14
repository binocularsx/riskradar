-- D111: the InfoSec role is removed from the system entirely.
--
-- D108 retired InfoSec as an escalation *target*: `cases.py` refused it with a
-- message pointing at the Fraud Ops lead, and `cases/workflow.py` listed it as
-- "InfoSec (retired)". The role, its account, its permissions and its
-- visibility rule all stayed, so the desk still carried a fourth role that
-- nobody could escalate to and no one on this desk fills. At the owner's
-- direction it now goes, including the two enum values, so that the role
-- cannot be assigned to a new account by mistake.
--
-- Removing an enum value means recreating the type: Postgres has no
-- DROP VALUE. Both types are safe to recreate here — `user_role` is used only
-- by `users.role` and `escalation_target` only by `cases.escalated_to`,
-- neither has a default, and no view depends on either.
--
-- The existing account is **deactivated, not deleted**. `audit_log` holds 77
-- entries naming it as the actor (logins, case views, four CASE_RETURNED) and
-- stores only `actor_user_id` — no denormalised name or role — so deleting the
-- row would leave recorded actions with no one attached to them. The audit log
-- is append-only by grant and is not rewritten to tidy up a role change. The
-- account keeps its name and its history, cannot log in (`auth.login` refuses
-- an inactive user), and its current role becomes ANALYST because that enum
-- value has to be free before the type can be recreated.

-- 1. Free the value: no row may hold it when the type is recreated.
UPDATE users SET role = 'ANALYST', active = false WHERE role = 'INFOSEC_ANALYST';

-- Any session it still holds goes with the account.
DELETE FROM sessions WHERE user_id IN (
    SELECT id FROM users WHERE active = false AND display_name ILIKE '%infosec%'
);

-- 2. Recreate user_role without INFOSEC_ANALYST.
ALTER TYPE user_role RENAME TO user_role_old;
CREATE TYPE user_role AS ENUM ('ANALYST', 'FRAUD_OPS_LEAD', 'ADMIN', 'SYSTEM');
ALTER TABLE users ALTER COLUMN role TYPE user_role USING role::text::user_role;
DROP TYPE user_role_old;

-- 3. Recreate escalation_target without INFOSEC. No case has ever been
--    escalated to it (verified: 0 rows), so nothing needs remapping first.
ALTER TYPE escalation_target RENAME TO escalation_target_old;
CREATE TYPE escalation_target AS ENUM ('FRAUD_OPS');
ALTER TABLE cases ALTER COLUMN escalated_to TYPE escalation_target
    USING escalated_to::text::escalation_target;
DROP TYPE escalation_target_old;

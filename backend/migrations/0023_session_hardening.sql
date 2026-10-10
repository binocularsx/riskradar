-- D95: session and MFA hardening (implementation plan §3).
--
-- Three gaps the plan names, all on the human-authentication path (D12):
--
--   1. MFA for *every* case user, not only leads and admins. That is a code
--      change (rbac.MFA_REQUIRED_ROLES), recorded here only because it is part
--      of the same decision; nothing in the schema changes for it.
--
--   2. Session-token rotation. A cookie captured at rest — from a proxy log, a
--      backup, a shared machine — stays valid for the whole 24-hour absolute
--      life of the session (D27). Rotating the identifier periodically shrinks
--      that window to the rotation interval without ending the session or
--      asking the user to sign in again. The primary key is the *hash* of the
--      cookie value (D12), so rotation mints a new random cookie value, stores
--      its hash, and keeps the previous hash valid for a short grace window so
--      the several requests a dashboard fires at once do not race each other
--      into a spurious logout (the same concurrency care as D41 and D94a).
--
--   3. CSRF. SameSite=Lax already blocks the common cross-site POST, but it is
--      one browser default, not a control we enforce, and it does not cover
--      same-site subdomains or every method. A double-submit token closes it:
--      a value the server pins to the session, mirrored in a readable cookie,
--      and required back in an X-CSRF-Token header on every unsafe request. An
--      attacker's page can neither read our cookie (different origin) nor set a
--      custom header cross-origin, so it cannot forge the pair.

-- The double-submit token, pinned to the session. Not a secret at rest: it is
-- mirrored to a script-readable cookie by design, and its security is that a
-- cross-origin page cannot read it or set the header. NULL only for the brief
-- life of any session that predates this migration; resolve() backfills one.
ALTER TABLE sessions ADD COLUMN csrf_token text;

-- When the identifier was last rotated. Drives the rotation interval, and the
-- grace window during which the previous identifier is still accepted.
ALTER TABLE sessions ADD COLUMN rotated_at timestamptz NOT NULL DEFAULT now();

-- The identifier this session had immediately before its last rotation. Kept
-- valid only for the grace window (see resolve()), so a request still carrying
-- the just-superseded cookie resolves instead of being logged out, while a
-- token older than one rotation cycle is dead.
ALTER TABLE sessions ADD COLUMN previous_id text;
CREATE INDEX sessions_previous_id_idx ON sessions (previous_id) WHERE previous_id IS NOT NULL;

-- No new grants: the app role already holds SELECT/INSERT/UPDATE/DELETE on
-- sessions (migration 0002), and added columns inherit the table's grants.

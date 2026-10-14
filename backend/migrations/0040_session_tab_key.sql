-- D112: one browser can hold several sessions at once, one per tab.
--
-- The session cookie carried exactly one identifier, so a browser could hold
-- exactly one session per host. Three consequences the desk actually hit:
-- signing in as a second role replaced the first; signing out of one tab signed
-- every tab out, because they all read the same cookie; and opening the console
-- in a new tab landed straight in whatever session was already there.
--
-- The cookie now carries up to four session identifiers and each tab says which
-- one is its own, in an `X-Session-Key` header (or `?tab=` on the SSE stream,
-- which is an EventSource and cannot send headers). The key is a selector, not
-- a credential: it names a session but cannot authenticate one, because the
-- session identifier itself is still the httpOnly cookie value that no script
-- can read (D12 holds).
--
-- `tab_key` is stable for the life of the session. `sessions.id` could not be
-- used for this: it is the *hash of the cookie value* and is replaced every 15
-- minutes by D95's rotation, so a tab holding it would lose its own session.
--
-- Existing sessions get a key so that a browser signed in across this migration
-- is not logged out by it; they have no way to learn it, so their next request
-- re-authenticates. That is one login, once, at deploy time.

ALTER TABLE sessions ADD COLUMN tab_key text;

UPDATE sessions SET tab_key = replace(gen_random_uuid()::text, '-', '') WHERE tab_key IS NULL;

ALTER TABLE sessions ALTER COLUMN tab_key SET NOT NULL;

-- Looked up on every authenticated request: the tab names its key, and the
-- cookie's identifiers decide whether it may have it.
CREATE UNIQUE INDEX sessions_tab_key_idx ON sessions (tab_key);

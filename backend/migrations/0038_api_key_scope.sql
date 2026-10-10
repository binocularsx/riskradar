-- D109g: the support team gets its own API key, limited to its own endpoints.
--
-- Until now any active key could call every machine endpoint, so a key issued
-- to the support team could also post transactions or flag BVNs on the
-- industry list, and the bank's switch key could forward customer reports.
-- A key now carries a scope:
--
--   BANK     the bank's switch and core: ingestion, events, directives, the
--            industry watch-list. Every existing key is one, so nothing that
--            works today stops working.
--   SUPPORT  the support team's system: forwarding customer reports, recording
--            watch-flag contacts, and the action feed it acknowledges. Nothing
--            else.

ALTER TABLE api_keys
    ADD COLUMN scope text NOT NULL DEFAULT 'BANK' CHECK (scope IN ('BANK', 'SUPPORT'));

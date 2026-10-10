-- D98: extend the privileged-access maker-checker (D96) to the remaining
-- detection-affecting admin surfaces named in D96d.
--
-- D96 covered rule and threshold changes. Promoting a model changes the live
-- scoring engine; adding or removing a sanctions / known-mule / allowlist entry
-- changes what the deterministic veto rules do (D11a) — a single administrator
-- could allowlist a mule to hide it, or drop a sanctions entry to let it
-- through. Each is exactly the single-hand tuning D69e closed for rules, so each
-- now takes a second administrator too. The mechanism is unchanged; only the
-- catalogue of change types grows, so this migration only widens the enum.
--
-- User administration (D96d's third surface) is access control rather than
-- detection tuning and carries a credential, so it is left for its own decision
-- (D98d) rather than folded in here.

ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'MODEL_PROMOTE';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'LIST_ADD';
ALTER TYPE config_change_type ADD VALUE IF NOT EXISTS 'LIST_REMOVE';

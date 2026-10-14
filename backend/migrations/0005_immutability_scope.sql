-- Narrow the immutability triggers to what "immutable" actually means.
--
-- 0002 added statement-level BEFORE UPDATE *and* BEFORE DELETE triggers to
-- decisions, alerts and transactions. Two problems surfaced the moment the
-- system was exercised:
--
-- 1. A statement-level trigger fires even when the statement matches **zero
--    rows**, so `DELETE FROM transactions WHERE id = <unscored>` failed on a
--    cascade that would have deleted nothing.
-- 2. Blocking DELETE on `decisions` makes `transactions` undeletable too (the
--    FK cascades), which forecloses the data-retention policy in D27 — a
--    retention job could never remove anything.
--
-- Immutability here means **"never silently altered"**, not "never removable".
-- Rewriting a decision in place would corrupt the reproducibility claim in G4
-- and would be invisible; deleting a transaction and its decision together
-- under a retention policy is a different, wholesale, visible operation.
--
-- So: UPDATE stays forbidden, at row level. DELETE is allowed.
--
-- This does not weaken the control that carries the tamper-evidence claim.
-- `audit_log` is protected by **grants** (0002) rather than triggers — the
-- application role holds INSERT and SELECT only — and a grant cannot be
-- sidestepped by anything the application does.

DROP TRIGGER IF EXISTS decisions_no_delete   ON decisions;
DROP TRIGGER IF EXISTS alerts_no_delete      ON alerts;

DROP TRIGGER IF EXISTS decisions_no_update   ON decisions;
DROP TRIGGER IF EXISTS alerts_no_update      ON alerts;
DROP TRIGGER IF EXISTS transactions_no_update ON transactions;

-- FOR EACH ROW: only fires when a row is genuinely being changed.
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER alerts_no_update BEFORE UPDATE ON alerts
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER transactions_no_update BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- audit_log keeps both, also at row level, as belt and braces behind the grant.
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();

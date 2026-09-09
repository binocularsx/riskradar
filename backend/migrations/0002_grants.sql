-- Risk Radar — privilege separation for the application role.
--
-- D12c: "App role has INSERT only on audit_log — no UPDATE/DELETE grant at the
-- database level." This file is that control. If it is wrong, the tamper-evidence
-- claim in PRD §13 is decoration.
--
-- Runs as riskradar_migrate, which owns every object created in 0001.

-- Baseline: the app can use the schema and read sequences it inserts into.
GRANT USAGE ON SCHEMA public TO riskradar_app;

-- Ordinary operational tables: full DML.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    transactions,
    scoring_queue,
    dead_letter,
    accounts,
    decisions,
    alerts,
    cases,
    case_notes,
    sessions,
    stream_events
TO riskradar_app;

-- Immutability of decisions and alerts (D13) is enforced by the application and
-- by these triggers, not by withholding UPDATE — the worker needs UPDATE on
-- neither, but cases and alerts share sequences and a blanket revoke complicates
-- the correlation upsert. Belt and braces:
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable (attempted %)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER alerts_no_update BEFORE UPDATE ON alerts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER alerts_no_delete BEFORE DELETE ON alerts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Transactions are immutable once ingested; only the queue moves.
CREATE TRIGGER transactions_no_update BEFORE UPDATE ON transactions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Configuration: the app reads it, ADMIN writes it through the API (which runs
-- as the app role), so INSERT and UPDATE are needed. Deletion is not.
GRANT SELECT, INSERT, UPDATE ON
    users,
    api_keys,
    model_versions,
    rulesets,
    rule_configs,
    threshold_sets,
    app_config
TO riskradar_app;

-- ===========================================================================
-- The control that matters.
-- ===========================================================================
-- INSERT and SELECT only. No UPDATE. No DELETE. No TRUNCATE.
-- An audit trail the application can rewrite is not an audit trail.
GRANT SELECT, INSERT ON audit_log TO riskradar_app;

-- Sequences the app must advance.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO riskradar_app;

-- Nothing created later should silently grant more.
ALTER DEFAULT PRIVILEGES FOR ROLE riskradar_migrate IN SCHEMA public
    REVOKE ALL ON TABLES FROM riskradar_app;

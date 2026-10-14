-- Risk Radar — cluster bootstrap. Run once, as a superuser, against the maintenance DB.
--
-- D12c: two roles. The application role must never be able to UPDATE or DELETE
-- audit_log, and that has to be true at the database level rather than by
-- convention in application code. A separate migration role owns the schema.
--
-- This file is deliberately not part of the numbered migration sequence: it
-- creates the roles and the database the sequence then runs inside.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'riskradar_migrate') THEN
        CREATE ROLE riskradar_migrate LOGIN PASSWORD 'riskradar_migrate_dev';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'riskradar_app') THEN
        CREATE ROLE riskradar_app LOGIN PASSWORD 'riskradar_app_dev';
    END IF;
END
$$;

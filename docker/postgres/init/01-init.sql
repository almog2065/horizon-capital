-- Idempotent bootstrap for the horizon role/db.
-- Runs once on first container start (postgres-data volume creation).

\connect horizon

-- Read-only role for analytics / Grafana dashboards.
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'horizon_ro') THEN
       CREATE ROLE horizon_ro LOGIN PASSWORD 'horizon_ro' NOSUPERUSER NOCREATEDB NOCREATEROLE;
   END IF;
END
$$;
GRANT CONNECT ON DATABASE horizon TO horizon_ro;
GRANT USAGE   ON SCHEMA public  TO horizon_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO horizon_ro;

-- Tracing extension if available (Postgres 15+ ships pg_trgm, useful for
-- LIKE searches on agent traces).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

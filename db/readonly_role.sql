-- A login role that can only read. Meridian connects as this role, so even a query that slipped
-- past the SQL guard could not change data.
--   psql "$OWNER_DATABASE_URL" -v pw="'choose-a-strong-password'" -f db/readonly_role.sql
-- (On Neon you can instead create the role in the console and run only the GRANT/ALTER lines.)

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'meridian_reader') THEN
    CREATE ROLE meridian_reader LOGIN;
  END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM meridian_reader;
GRANT USAGE ON SCHEMA public TO meridian_reader;
GRANT SELECT ON regions, customers, subscriptions, transactions TO meridian_reader;
ALTER ROLE meridian_reader SET default_transaction_read_only = on;
ALTER ROLE meridian_reader SET statement_timeout = '10s';

-- Upgrade volumes created before partner_report.reporting_timezone.
-- Idempotent. Does not drop or recreate tables. Does not touch extract_state.
--
-- Legacy NULL values are filled with America/Los_Angeles, the mock account
-- timezone. That assumption applies only to existing rows. DROP DEFAULT then
-- forces new inserts to provide reporting_timezone explicitly.
--
-- The extractor image does not mount this directory. Apply at setup with:
--   python -c 'from storage import apply_migrations; ...'
-- or from the host:
--   psql "$PG_DSN" -f task1_api/warehouse/migrations/001_add_reporting_timezone.sql

ALTER TABLE partner_report
    ADD COLUMN IF NOT EXISTS reporting_timezone text;

UPDATE partner_report
    SET reporting_timezone = 'America/Los_Angeles'
    WHERE reporting_timezone IS NULL;

ALTER TABLE partner_report
    ALTER COLUMN reporting_timezone SET NOT NULL;

ALTER TABLE partner_report
    ALTER COLUMN reporting_timezone DROP DEFAULT;

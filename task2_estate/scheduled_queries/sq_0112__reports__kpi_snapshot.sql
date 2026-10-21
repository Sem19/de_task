-- Exec KPI snapshot (accumulating)
-- owner: olena.kravets@acme.co
-- created: 2022-06-01
-- keeps history; the source table only holds 120 days
WITH existing AS (
  SELECT * FROM `acme-analytics-prod.reports.kpi_snapshot`
  WHERE snapshot_date < CURRENT_DATE()
),
today AS (
  SELECT
      CURRENT_DATE() AS snapshot_date,
      metric_name,
      metric_value
  FROM `acme-analytics-prod.reports.exec_kpi_daily`
)
SELECT * FROM existing
UNION ALL
SELECT * FROM today

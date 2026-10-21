-- [2026] funnel unified
-- owner: maria.voloshyna@acme.co
-- created: 2026-02-06
SELECT
    event_date AS report_date,
    region,
    * EXCEPT (event_date, region)
FROM `acme-analytics-prod.reports.funnel_daily_core`
WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)

-- [2026] exec kpi v2
-- owner: maria.voloshyna@acme.co
-- created: 2026-05-09
SELECT
    payment_date AS report_date,
    region,
    * EXCEPT (payment_date, region)
FROM `acme-analytics-prod.reports.exec_kpi_daily`
WHERE payment_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 120 DAY)

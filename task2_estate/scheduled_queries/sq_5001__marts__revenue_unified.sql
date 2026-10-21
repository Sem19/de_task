-- [2026] revenue unified
-- owner: maria.voloshyna@acme.co
-- created: 2026-01-05
SELECT
    payment_date AS report_date,
    region,
    * EXCEPT (payment_date, region)
FROM `acme-analytics-prod.reports.revenue_daily`
WHERE payment_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY)

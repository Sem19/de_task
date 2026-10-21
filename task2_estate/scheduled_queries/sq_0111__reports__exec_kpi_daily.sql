-- Exec KPI daily
-- owner: olena.kravets@acme.co
-- created: 2022-06-01
SELECT
    payment_date AS report_date,
    region,
    * EXCEPT (payment_date, region)
FROM `acme-analytics-prod.reports.revenue_by_region`
WHERE payment_date >= DATE_SUB(CURRENT_DATE("Europe/Kyiv"), INTERVAL 120 DAY)

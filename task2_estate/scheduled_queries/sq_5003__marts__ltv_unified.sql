-- [2026] ltv unified
-- owner: maria.voloshyna@acme.co
-- created: 2026-03-07
SELECT
    cohort_date AS report_date,
    region,
    * EXCEPT (cohort_date, region)
FROM `acme-analytics-prod.reports.ltv_by_cohort`
WHERE cohort_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 540 DAY)

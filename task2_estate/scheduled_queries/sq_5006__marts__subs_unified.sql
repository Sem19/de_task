-- [2026] subs unified
-- owner: maria.voloshyna@acme.co
-- created: 2026-06-10
SELECT
    snapshot_date AS report_date,
    region,
    * EXCEPT (snapshot_date, region)
FROM `acme-analytics-prod.product.subscriber_snapshot`
WHERE snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)

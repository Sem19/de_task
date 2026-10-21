-- Funnel union → funnel_by_device
-- owner: maria.voloshyna@acme.co
-- created: 2026-01-08
WITH all_shards AS (
SELECT * FROM `acme-analytics-prod.reports.funnel_2024`
UNION ALL
SELECT * FROM `acme-analytics-prod.reports.funnel_2025`
UNION ALL
SELECT * FROM `acme-analytics-prod.reports.funnel_current`
)
SELECT
    event_date,
    region,
    device_type,
    SUM(visits) AS visits,
    SUM(registrations) AS registrations,
    SUM(checkouts) AS checkouts,
    SUM(payments) AS payments
FROM all_shards
GROUP BY 1, 2, 3

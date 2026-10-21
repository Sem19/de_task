-- [2026] dst gap daily recon
-- owner: maria.voloshyna@acme.co
-- created: 2026-02-22
SELECT
    TIMESTAMP_TRUNC(created_at, HOUR) AS attempt_hour,
    processor,
    is_rebill,
    COUNT(*) AS attempts,
    COUNT(DISTINCT user_id) AS users,
    SUM(amount_minor) / 100.0 AS amount_local,
    SAFE_DIVIDE(COUNTIF(status = 'settled'), COUNT(*)) AS settle_rate
FROM `acme-analytics-prod.raw.payments`
WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
GROUP BY 1, 2, 3

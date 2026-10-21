-- [2024] settle rate hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-04-14
SELECT
    TIMESTAMP_TRUNC(created_at, HOUR) AS attempt_hour,
    processor,
    attempt_no,
    is_rebill,
    COUNT(*) AS attempts,
    COUNT(DISTINCT user_id) AS users,
    SUM(amount_minor) / 100.0 AS amount_local,
    SAFE_DIVIDE(COUNTIF(status = 'settled'), COUNT(*)) AS settle_rate
FROM `acme-analytics-prod.raw.payments`
WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
GROUP BY 1, 2, 3, 4

-- [2024] payment attempts unpart
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-02-12
SELECT
    TIMESTAMP_TRUNC(created_at, HOUR) AS attempt_hour,
    processor,
    status,
    currency,
    COUNT(*) AS attempts,
    COUNT(DISTINCT user_id) AS users,
    SUM(amount_minor) / 100.0 AS amount_local,
    SAFE_DIVIDE(COUNTIF(status = 'settled'), COUNT(*)) AS settle_rate
FROM `acme-analytics-prod.raw.payments`
WHERE DATE(created_at) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
GROUP BY 1, 2, 3, 4

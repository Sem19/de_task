-- [2025] abtest revenue join 90d
-- owner: ihor.pavliuk@acme.co
-- created: 2025-10-13
SELECT
    DATE(p.settled_at) AS settle_date,
    a.variant,
    COUNT(DISTINCT p.user_id) AS payers,
    SUM(p.amount_minor) / 100.0 AS revenue
FROM `acme-analytics-prod.raw.payments` p
LEFT JOIN `acme-analytics-prod.ml.legacy_user_map` a
    ON CAST(a.legacy_user_id AS STRING) = p.user_id
WHERE DATE(p.settled_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  AND p.status = 'settled'
GROUP BY 1, 2

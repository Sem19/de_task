-- [2025] abtest paywall position
-- owner: ihor.pavliuk@acme.co
-- created: 2025-02-05
WITH assign AS (
  SELECT test_name, user_id, variant, MIN(assigned_at) AS assigned_at
  FROM `acme-analytics-prod.raw.abtest_assignments`
  WHERE test_name = 'paywall_position'
  GROUP BY 1, 2, 3
),
pay AS (
  SELECT user_id, SUM(amount_minor) / 100.0 AS revenue, COUNT(*) AS payments
  FROM `acme-analytics-prod.raw.payments`
  WHERE status = 'settled'
  GROUP BY 1
)
SELECT
    a.test_name,
    a.variant,
    COUNT(DISTINCT a.user_id) AS users,
    COUNT(DISTINCT p.user_id) AS payers,
    SUM(p.revenue) AS revenue,
    SAFE_DIVIDE(SUM(p.revenue), COUNT(DISTINCT a.user_id)) AS arpu
FROM assign a
LEFT JOIN pay p ON p.user_id = a.user_id
WHERE p.user_id IS NOT NULL
GROUP BY 1, 2

-- [2024] revenue intraday usd
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-05-25
WITH
pay AS (
  SELECT *
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY updated_at DESC, ingested_at DESC) AS _rn
    FROM `acme-analytics-prod.raw.payments`
  )
  WHERE _rn = 1
),
subs AS (
  SELECT subscription_id, user_id, plan_id, tier, billing_period_days, country_code
  FROM `acme-analytics-prod.raw.subscriptions`
  WHERE NOT COALESCE(is_test, FALSE)
)
SELECT
    DATE(p.created_at) AS payment_date,
    s.country_code,
    CASE
      WHEN s.country_code IN ('US','CA') THEN 'NA'
      WHEN s.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN s.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN s.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN s.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    s.tier,
    s.billing_period_days,
    COUNT(DISTINCT p.payment_id) AS payments,
    COUNT(DISTINCT p.user_id) AS payers,
    SUM(p.amount_minor / 100.0 * 0.92  -- EUR->USD as of 2024-11, revisit later) AS revenue_usd
FROM pay p
JOIN subs s
    ON s.subscription_id = p.subscription_id

WHERE DATE(p.created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND p.status = 'settled'
GROUP BY 1, 2, 3, 4, 5

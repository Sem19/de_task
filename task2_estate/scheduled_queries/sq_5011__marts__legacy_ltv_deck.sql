-- [2026] legacy ltv deck
-- owner: maria.voloshyna@acme.co
-- created: 2026-03-15
WITH cohort AS (
  SELECT
      DATE(s.activated_at) AS cohort_date,
      s.subscription_id,
      s.user_id,
      s.tier,
      s.billing_period_days,
      s.country_code,
      CASE
      WHEN s.country_code IN ('US','CA') THEN 'NA'
      WHEN s.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN s.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN s.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN s.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region
  FROM `acme-analytics-prod.raw.subscriptions` s
  WHERE s.status = 'active'
    AND s.activated_at IS NOT NULL
    AND DATE(s.activated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 540 DAY)
),
rev AS (
  SELECT
      p.subscription_id,
      SUM(p.amount_minor / 100.0 * 0.92  -- EUR->USD as of 2024-11, revisit later) AS revenue_usd,
      COUNT(*) AS charges
  FROM `acme-analytics-prod.raw.payments` p
  
  WHERE p.status = 'settled'
  GROUP BY 1
)
SELECT
    c.cohort_date,
    c.region,
    c.tier,
    c.billing_period_days,
    COUNT(DISTINCT c.subscription_id) AS subscriptions,
    SUM(r.revenue_usd) AS revenue_usd,
    SAFE_DIVIDE(SUM(r.revenue_usd), COUNT(DISTINCT c.subscription_id)) AS ltv_usd,
    SAFE_DIVIDE(SUM(r.charges), COUNT(DISTINCT c.subscription_id)) AS charges_per_sub
FROM cohort c
LEFT JOIN rev r USING (subscription_id)
GROUP BY 1, 2, 3, 4

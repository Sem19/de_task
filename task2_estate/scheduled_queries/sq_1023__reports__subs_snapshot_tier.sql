-- [2022] subs snapshot tier
-- owner: olena.kravets@acme.co
-- created: 2022-01-25
SELECT
    CURRENT_DATE() AS snapshot_date,
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
    s.plan_id,
    s.currency AS extra_dim,
    COUNT(*) AS subscriptions,
    COUNT(DISTINCT s.user_id) AS subscribers,
    SUM(s.price_minor) / 100.0 AS mrr_local
FROM `acme-analytics-prod.raw.subscriptions` s
WHERE s.status = 'active'
  AND NOT COALESCE(s.is_test, FALSE)
GROUP BY 1, 2, 3, 4, 5, 6

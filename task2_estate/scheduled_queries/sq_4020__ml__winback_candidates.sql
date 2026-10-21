-- [2025] winback candidates
-- owner: ihor.pavliuk@acme.co
-- created: 2025-09-23
SELECT
    u.user_id,
    u.registered_at,
    u.country_code,
    CASE
      WHEN u.country_code IN ('US','CA') THEN 'NA'
      WHEN u.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN u.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN u.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN u.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    u.acquisition_source
FROM `acme-analytics-prod.raw.users` u
WHERE u.user_id NOT IN (
    SELECT user_id
    FROM `acme-analytics-prod.raw.subscriptions`
    WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
)
  AND DATE(u.registered_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)

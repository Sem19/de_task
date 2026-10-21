-- [2022] surface overlap monthly
-- owner: olena.kravets@acme.co
-- created: 2022-08-21
WITH web AS (
  SELECT DATE(event_ts) AS event_date, user_id, 'web' AS surface, country_code
  FROM `acme-analytics-prod.raw.events`
  WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
    AND device_type IN ('desktop','mobile_web')
),
app AS (
  SELECT DATE(event_ts) AS event_date, user_id, 'app' AS surface, country_code
  FROM `acme-analytics-prod.raw.events`
  WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
    AND device_type = 'app'
),
combined AS (
  SELECT * FROM web
  UNION DISTINCT
  SELECT * FROM app
)
SELECT
    event_date,
    surface,
    CASE
      WHEN country_code IN ('US','CA') THEN 'NA'
      WHEN country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    COUNT(*) AS user_days,
    COUNT(DISTINCT user_id) AS users
FROM combined
GROUP BY 1, 2, 3

-- [2024] events hourly device
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-04-04
SELECT
    TIMESTAMP_TRUNC(event_ts, HOUR) AS event_hour,
    country_code,
    CASE
      WHEN country_code IN ('US','CA') THEN 'NA'
      WHEN country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    event_name,
    platform_lang,
    COUNT(*) AS events,
    COUNT(DISTINCT user_id) AS users,
    COUNT(DISTINCT session_id) AS sessions
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
  AND event_ts < TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), HOUR)
GROUP BY 1, 2, 3, 4, 5

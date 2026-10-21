-- [2024] events hourly utm
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-06-06
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
    utm_campaign,
    COUNT(*) AS events,
    COUNT(DISTINCT user_id) AS users,
    COUNT(DISTINCT session_id) AS sessions
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) = CURRENT_DATE()
  AND event_ts < TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), HOUR)
GROUP BY 1, 2, 3, 4, 5

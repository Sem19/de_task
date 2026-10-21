-- [2024] funnel hourly utm
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-07-17
SELECT
    DATE(event_ts) AS event_date,
    country_code,
    CASE
      WHEN country_code IN ('US','CA') THEN 'NA'
      WHEN country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    utm_source,
    COUNTIF(event_name = 'session_started') AS session_started_events,
    COUNT(DISTINCT IF(event_name = 'session_started', user_id, NULL)) AS session_started_users,
    COUNTIF(event_name = 'checkout_started') AS checkout_started_events,
    COUNT(DISTINCT IF(event_name = 'checkout_started', user_id, NULL)) AS checkout_started_users
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) = @run_date
  AND event_name IN ('session_started', 'checkout_started')
GROUP BY 1, 2, 3, 4

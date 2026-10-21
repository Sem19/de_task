-- [2022] funnel daily core
-- owner: olena.kravets@acme.co
-- created: 2022-01-03
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
    device_type,
    COUNTIF(event_name = 'session_started') AS session_started_events,
    COUNT(DISTINCT IF(event_name = 'session_started', user_id, NULL)) AS session_started_users,
    COUNTIF(event_name = 'registration_completed') AS registration_completed_events,
    COUNT(DISTINCT IF(event_name = 'registration_completed', user_id, NULL)) AS registration_completed_users,
    COUNTIF(event_name = 'checkout_started') AS checkout_started_events,
    COUNT(DISTINCT IF(event_name = 'checkout_started', user_id, NULL)) AS checkout_started_users,
    COUNTIF(event_name = 'payment_settled') AS payment_settled_events,
    COUNT(DISTINCT IF(event_name = 'payment_settled', user_id, NULL)) AS payment_settled_users
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
  AND event_name IN ('session_started', 'registration_completed', 'checkout_started', 'payment_settled')
GROUP BY 1, 2, 3, 4

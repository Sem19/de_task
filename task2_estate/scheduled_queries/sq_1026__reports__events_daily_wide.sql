-- [2022] events daily wide
-- owner: olena.kravets@acme.co
-- created: 2022-04-03
SELECT
    DATE(e.event_ts) AS event_date,
    e.country_code,
    CASE
      WHEN e.country_code IN ('US','CA') THEN 'NA'
      WHEN e.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN e.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN e.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN e.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    e.platform_lang,
    e.device_type,
    e.utm_source,
    e.utm_medium,
    COUNT(*) AS events,
    COUNT(DISTINCT e.user_id) AS users,
    COUNT(DISTINCT e.session_id) AS sessions,
    COUNTIF(e.event_name = 'registration_completed') AS registrations,
    COUNTIF(e.event_name = 'checkout_started') AS checkouts,
    COUNTIF(e.event_name = 'payment_settled') AS payments
FROM `acme-analytics-prod.raw.events` e
WHERE DATE(event_ts) >= '2022-01-01'
GROUP BY 1, 2, 3, 4, 5, 6, 7

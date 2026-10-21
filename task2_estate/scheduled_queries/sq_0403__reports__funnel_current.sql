-- Funnel shard funnel_current
-- owner: olena.kravets@acme.co
-- created: 2026-01-05
SELECT
    DATE(event_ts) AS event_date,
    CASE
      WHEN country_code IN ('US','CA') THEN 'NA'
      WHEN country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    device_type,
    COUNTIF(event_name = 'session_started') AS visits,
    COUNTIF(event_name = 'registration_completed') AS registrations,
    COUNTIF(event_name = 'checkout_started') AS checkouts,
    COUNTIF(event_name = 'payment_settled') AS payments
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) BETWEEN '2026-01-01' AND '2026-12-31'
GROUP BY 1, 2, 3

-- [2022] events payment raw
-- owner: olena.kravets@acme.co
-- created: 2022-11-24
SELECT
    *
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
  AND event_name = 'payment_settled'

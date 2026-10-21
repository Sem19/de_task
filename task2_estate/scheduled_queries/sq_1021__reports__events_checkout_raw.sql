-- [2022] events checkout raw
-- owner: olena.kravets@acme.co
-- created: 2022-10-23
SELECT
    *
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
  AND event_name = 'checkout_started'

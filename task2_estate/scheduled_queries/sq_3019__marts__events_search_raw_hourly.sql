-- [2024] events search raw hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-10-20
SELECT
    *
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND event_name = 'search_submitted'

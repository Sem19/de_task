-- [2024] events app raw hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-09-19
SELECT
    *
FROM `acme-analytics-prod.raw.events`
WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND event_name = 'app_open'

-- [2024] stg events hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-06-26
SELECT
    event_id, user_id, session_id, event_name, event_ts, page_url, referrer, utm_source, utm_medium, utm_campaign, country_code, device_type, platform_lang, app_version, properties, experiment, ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY event_id
            ORDER BY ingested_at DESC
        ) AS _rn
    FROM `acme-analytics-prod.raw.events`
    WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
WHERE _rn = 1

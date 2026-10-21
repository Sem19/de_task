-- [2026] session quality v1
-- owner: maria.voloshyna@acme.co
-- created: 2026-07-19
SELECT
    DATE(e.event_ts) AS event_date,
    v.version_label,
    v.release_channel,
    COUNT(DISTINCT e.user_id) AS users,
    COUNT(*) AS events
FROM `acme-analytics-prod.raw.events` e
JOIN `acme-analytics-prod.staging.stg_sessions_v1` v
    ON v.app_version = e.app_version
WHERE DATE(e.event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY 1, 2, 3

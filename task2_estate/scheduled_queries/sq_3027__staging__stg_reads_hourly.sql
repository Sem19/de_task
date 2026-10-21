-- [2024] stg reads hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-08-02
SELECT
    read_id, user_id, title_id, episode_no, is_paid_episode, read_ts, minutes, device_type, ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY read_id
            
        ) AS _rn
    FROM `acme-analytics-prod.raw.reads`
    WHERE DATE(read_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
WHERE _rn = 1

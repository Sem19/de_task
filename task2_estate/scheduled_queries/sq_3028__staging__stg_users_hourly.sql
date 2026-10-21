-- [2024] stg users hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-09-03
SELECT
    user_id, registered_at, country_code, platform_lang, acquisition_source, email_hash, is_test, updated_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY user_id
            ORDER BY updated_at DESC, ingested_at DESC
        ) AS _rn
    FROM `acme-analytics-prod.raw.users`
    WHERE DATE(updated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
WHERE _rn = 1

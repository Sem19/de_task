-- [2024] stg payments hourly
-- owner: dmytro.shevchuk@acme.co
-- created: 2024-07-27
SELECT
    payment_id, order_id, subscription_id, user_id, amount_minor, currency, status, processor, attempt_no, is_rebill, created_at, settled_at, refunded_at, updated_at, ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY payment_id
            ORDER BY updated_at ASC
        ) AS _rn
    FROM `acme-analytics-prod.raw.payments`
    WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
WHERE _rn = 1

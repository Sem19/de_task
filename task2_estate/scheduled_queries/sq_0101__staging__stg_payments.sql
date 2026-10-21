-- stg payments dedupe
-- owner: olena.kravets@acme.co
-- created: 2023-02-06
-- feeds every revenue report
SELECT
    payment_id, order_id, subscription_id, user_id, amount_minor, currency, status, processor, attempt_no, is_rebill, created_at, settled_at, refunded_at, updated_at, ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY payment_id
            ORDER BY updated_at DESC, ingested_at DESC
        ) AS _rn
    FROM `acme-analytics-prod.raw.payments`
    WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
)
WHERE _rn = 1

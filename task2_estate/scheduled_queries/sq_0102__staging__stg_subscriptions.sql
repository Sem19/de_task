-- stg subscriptions dedupe
-- owner: olena.kravets@acme.co
-- created: 2023-02-06
SELECT
    subscription_id, original_subscription_id, user_id, plan_id, status, tier, price_minor, currency, billing_period_days, created_at, activated_at, cancelled_at, expires_at, upgraded_at, country_code, payment_processor, is_test, updated_at, ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY subscription_id
            
        ) AS _rn
    FROM `acme-analytics-prod.raw.subscriptions`
    WHERE DATE(updated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
)
WHERE _rn = 1

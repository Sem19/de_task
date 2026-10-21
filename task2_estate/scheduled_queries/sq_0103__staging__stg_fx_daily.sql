-- stg fx daily fill-forward
-- owner: olena.kravets@acme.co
-- created: 2023-02-20
WITH spine AS (
  SELECT rate_date
  FROM UNNEST(GENERATE_DATE_ARRAY('2022-01-01', CURRENT_DATE())) AS rate_date
),
currencies AS (
  SELECT DISTINCT currency FROM `acme-analytics-prod.raw.fx_rates`
)
SELECT
    spine.rate_date,
    currencies.currency,
    fx.rate_to_usd,
    LAST_VALUE(fx.rate_to_usd IGNORE NULLS) OVER (
        PARTITION BY currencies.currency
        ORDER BY spine.rate_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS last_known_rate
FROM spine
CROSS JOIN currencies
LEFT JOIN `acme-analytics-prod.raw.fx_rates` fx
    ON fx.rate_date = spine.rate_date
   AND fx.currency = currencies.currency

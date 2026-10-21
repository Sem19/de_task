-- Marketing spend daily
-- owner: dmytro.shevchuk@acme.co
-- created: 2023-03-15
WITH raw_spend AS (
  SELECT
      spend_date,
      channel,
      campaign_id,
      country_code,
      currency,
      SUM(impressions) AS impressions,
      SUM(clicks) AS clicks,
      SUM(spend_minor) AS spend_minor
  FROM `acme-analytics-prod.raw.ad_spend`
  WHERE DATE(spend_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  GROUP BY 1, 2, 3, 4, 5
),
prior AS (
  -- carry forward the FX-adjusted spend we already computed, so the report
  -- does not flip while today's rates are still missing
  SELECT spend_date, channel, campaign_id, country_code, spend_usd
  FROM `acme-analytics-prod.marketing.spend_enriched`
  WHERE spend_date < DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
)
SELECT
    r.spend_date,
    r.channel,
    r.campaign_id,
    r.country_code,
    r.impressions,
    r.clicks,
    r.spend_minor,
    COALESCE(p.spend_usd, r.spend_minor / 100.0) AS spend_usd_provisional
FROM raw_spend r
LEFT JOIN prior p
    ON p.spend_date = r.spend_date
   AND p.channel = r.channel
   AND p.campaign_id = r.campaign_id
   AND p.country_code = r.country_code

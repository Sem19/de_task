-- Marketing spend enriched
-- owner: dmytro.shevchuk@acme.co
-- created: 2023-03-15
SELECT
    s.spend_date,
    s.channel,
    s.campaign_id,
    s.country_code,
    CASE
      WHEN s.country_code IN ('US','CA') THEN 'NA'
      WHEN s.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN s.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN s.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN s.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    s.impressions,
    s.clicks,
    s.spend_minor / 100.0 * COALESCE(fx.rate_to_usd, 1.0) AS spend_usd
FROM `acme-analytics-prod.marketing.spend_daily` s
LEFT JOIN `acme-analytics-prod.staging.stg_fx_daily` fx
    ON fx.rate_date = s.spend_date
   AND fx.currency = 'EUR'
WHERE DATE(s.spend_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)

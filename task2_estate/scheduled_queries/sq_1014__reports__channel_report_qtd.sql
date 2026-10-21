-- [2022] channel report qtd
-- owner: olena.kravets@acme.co
-- created: 2022-03-16
WITH spend AS (
  SELECT
      spend_date,
      channel,
      campaign_id,
      country_code,
      SUM(impressions) AS impressions,
      SUM(clicks) AS clicks,
      SUM(spend_minor) / 100.0 AS spend
  FROM `acme-analytics-prod.raw.ad_spend`
  WHERE DATE(spend_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL 120 DAY)
  GROUP BY 1, 2, 3, 4
),
conv AS (
  SELECT
      DATE(u.registered_at) AS reg_date,
      u.acquisition_source AS channel,
      u.country_code,
      COUNT(DISTINCT u.user_id) AS registrations,
      COUNT(DISTINCT p.user_id) AS payers
  FROM `acme-analytics-prod.raw.users` u
  LEFT JOIN `acme-analytics-prod.raw.payments` p
      ON p.user_id = u.user_id
     AND p.status = 'settled'
  WHERE DATE(u.registered_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 120 DAY)
  GROUP BY 1, 2, 3
)
SELECT
    s.spend_date,
    s.channel,
    CASE
      WHEN s.country_code IN ('US','CA') THEN 'NA'
      WHEN s.country_code IN ('GB','DE','FR','IT','ES','NL','PL','PT','SE','IE') THEN 'EU'
      WHEN s.country_code IN ('BR','MX','AR','CL','CO') THEN 'LATAM'
      WHEN s.country_code IN ('JP','KR','TW','SG','TH','PH','ID') THEN 'APAC'
      WHEN s.country_code IN ('TR','AE','SA','IL','ZA') THEN 'EMEA'
      ELSE 'Other'
    END AS region,
    SUM(s.spend) AS spend,
    SUM(s.clicks) AS clicks,
    SUM(c.registrations) AS registrations,
    SUM(c.payers) AS payers,
    SAFE_DIVIDE(SUM(s.spend), SUM(c.payers)) AS cpa
FROM spend s
LEFT JOIN conv c
    ON c.reg_date = s.spend_date
   AND c.channel = s.channel
   AND c.country_code = s.country_code
GROUP BY 1, 2, 3

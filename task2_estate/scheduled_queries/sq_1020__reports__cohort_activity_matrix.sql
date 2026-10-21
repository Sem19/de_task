-- [2022] cohort activity matrix
-- owner: olena.kravets@acme.co
-- created: 2022-09-22
WITH spine AS (
  SELECT d AS activity_date
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY), CURRENT_DATE())) AS d
),
cohorts AS (
  SELECT DISTINCT DATE(activated_at) AS cohort_date, tier AS tier, country_code
  FROM `acme-analytics-prod.raw.subscriptions`
  WHERE activated_at IS NOT NULL
),
activity AS (
  SELECT DATE(read_ts) AS activity_date, user_id, COUNT(*) AS reads
  FROM `acme-analytics-prod.raw.reads`
  GROUP BY 1, 2
)
SELECT
    c.cohort_date,
    s.activity_date,
    c.tier,
    c.country_code,
    SUM(a.reads) AS reads
FROM cohorts c
CROSS JOIN spine s
LEFT JOIN activity a ON a.activity_date = s.activity_date
GROUP BY 1, 2, 3, 4

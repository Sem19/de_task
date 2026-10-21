-- [2022] cohort retention reads
-- owner: olena.kravets@acme.co
-- created: 2022-04-17
WITH cohort AS (
  SELECT user_id, DATE(MIN(activated_at)) AS cohort_date
  FROM `acme-analytics-prod.raw.subscriptions`
  WHERE activated_at IS NOT NULL
  GROUP BY 1
),
act AS (
  SELECT user_id, DATE(read_ts) AS activity_date
  FROM `acme-analytics-prod.raw.reads`
  WHERE DATE(read_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  GROUP BY 1, 2
)
SELECT
    c.cohort_date,
    DATE_DIFF(a.activity_date, c.cohort_date, DAY) AS day_index,
    COUNT(DISTINCT c.user_id) AS cohort_users,
    COUNT(DISTINCT a.user_id) AS active_users
FROM cohort c
LEFT JOIN act a ON a.user_id = c.user_id
GROUP BY 1, 2

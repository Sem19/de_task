-- [2022] reads daily device
-- owner: olena.kravets@acme.co
-- created: 2022-08-10
WITH
r AS (
  SELECT read_id, user_id, title_id, episode_no, is_paid_episode, read_ts, minutes, device_type
  FROM (
    SELECT read_id, user_id, title_id, episode_no, is_paid_episode, read_ts, minutes, device_type, ROW_NUMBER() OVER (PARTITION BY read_id ORDER BY ingested_at DESC) AS _rn
    FROM `acme-analytics-prod.raw.reads`
  )
  WHERE _rn = 1
)
SELECT
    DATE(r.read_ts) AS read_date,
    t.genre,
    r.device_type,
    t.is_active AS extra_dim,
    COUNT(*) AS reads,
    COUNT(DISTINCT r.user_id) AS readers,
    COUNTIF(r.is_paid_episode) AS paid_reads,
    SUM(r.minutes) AS minutes_read,
    COUNT(DISTINCT IF(r.is_paid_episode, r.title_id, NULL)) AS paid_titles
FROM r
JOIN `acme-analytics-prod.raw.titles` t USING (title_id)
WHERE DATE(r.read_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY 1, 2, 3, 4

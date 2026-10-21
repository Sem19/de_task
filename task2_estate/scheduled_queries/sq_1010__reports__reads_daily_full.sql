-- [2022] reads daily full
-- owner: olena.kravets@acme.co
-- created: 2022-10-12
WITH
r AS (
  SELECT read_id, user_id, title_id, episode_no, is_paid_episode, read_ts, minutes, device_type
  FROM (
    SELECT read_id, user_id, title_id, episode_no, is_paid_episode, read_ts, minutes, device_type, ROW_NUMBER() OVER (PARTITION BY read_id ORDER BY updated_at ASC) AS _rn
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
WHERE DATE(r.read_ts) >= '2022-01-01'
GROUP BY 1, 2, 3, 4

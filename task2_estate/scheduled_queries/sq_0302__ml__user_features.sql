-- User feature table
-- owner: ihor.pavliuk@acme.co
-- created: 2025-01-20
WITH base AS (
  SELECT
      user_id,
      MAX(activity_date) AS last_active_date,
      SUM(reads) AS reads_lifetime,
      SUM(minutes_read) AS minutes_lifetime,
      SUM(paid_reads) AS paid_reads_lifetime
  FROM `acme-analytics-prod.ml.user_daily`
  GROUP BY 1
),
seg AS (
  SELECT user_id, segment, segment_scored_at
  FROM `acme-analytics-prod.ml.user_segments`
)
SELECT
    b.user_id,
    b.last_active_date,
    b.reads_lifetime,
    b.minutes_lifetime,
    b.paid_reads_lifetime,
    DATE_DIFF(CURRENT_DATE(), b.last_active_date, DAY) AS days_since_active,
    seg.segment AS previous_segment
FROM base b
LEFT JOIN seg ON seg.user_id = b.user_id

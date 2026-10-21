-- User daily activity
-- owner: ihor.pavliuk@acme.co
-- created: 2025-01-20
SELECT
    DATE(r.read_ts) AS activity_date,
    r.user_id,
    COUNT(*) AS reads,
    SUM(r.minutes) AS minutes_read,
    COUNTIF(r.is_paid_episode) AS paid_reads,
    ANY_VALUE(seg.segment) AS segment_at_activity
FROM `acme-analytics-prod.raw.reads` r
LEFT JOIN `acme-analytics-prod.ml.user_segments` seg
    ON seg.user_id = r.user_id
WHERE DATE(r.read_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
GROUP BY 1, 2

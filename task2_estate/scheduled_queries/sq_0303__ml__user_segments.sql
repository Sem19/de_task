-- User segments
-- owner: ihor.pavliuk@acme.co
-- created: 2025-01-27
SELECT
    f.user_id,
    CASE
      WHEN f.days_since_active <= 3 AND f.paid_reads_lifetime >= 20 THEN 'core'
      WHEN f.days_since_active <= 7 THEN 'active'
      WHEN f.days_since_active <= 30 THEN 'lapsing'
      ELSE 'dormant'
    END AS segment,
    CURRENT_TIMESTAMP() AS segment_scored_at
FROM `acme-analytics-prod.ml.user_features` f

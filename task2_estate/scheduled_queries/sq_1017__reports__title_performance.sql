-- [2022] title performance
-- owner: olena.kravets@acme.co
-- created: 2022-06-19
SELECT
    t.title_id,
    t.title_slug,
    t.genre,
    t.episodes_total,
    t.episodes_free,
    t.`end` AS ending_type,
    COUNT(DISTINCT r.user_id) AS readers,
    COUNTIF(r.is_paid_episode) AS paid_reads
FROM `acme-analytics-prod.raw.titles` t
LEFT JOIN `acme-analytics-prod.raw.reads` r USING (title_id)
WHERE DATE(r.read_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY 1, 2, 3, 4, 5, 6

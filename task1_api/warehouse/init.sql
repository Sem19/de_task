-- Landing zone for the partner report. This is the target of task 1.
--
-- The grain and the keys are YOUR decision — this DDL is a starting point, not a
-- specification. If you change it, say why.

CREATE TABLE IF NOT EXISTS partner_report (
    report_date     date        NOT NULL,
    country         text        NOT NULL,
    channel         text        NOT NULL,

    impressions     bigint      NOT NULL,
    clicks          bigint      NOT NULL,
    installs        bigint      NOT NULL,
    signups         bigint      NOT NULL,
    payers          bigint      NOT NULL,
    spend_minor     bigint      NOT NULL,
    revenue_minor   bigint      NOT NULL,
    currency        text        NOT NULL,

    -- ingestion metadata
    extracted_at    timestamptz NOT NULL,
    data_version    bigint,

    PRIMARY KEY (report_date, country, channel)
);

-- Where the extractor remembers what it has already done. Shape is up to you.
CREATE TABLE IF NOT EXISTS extract_state (
    key         text        PRIMARY KEY,
    value       jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

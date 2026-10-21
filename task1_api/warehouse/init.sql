-- Authoritative DDL for a fresh warehouse volume.
-- Postgres runs this once from /docker-entrypoint-initdb.d on first init.
-- Existing volumes are not recreated; see warehouse/migrations/.
--
-- reporting_timezone has no column default. New inserts must supply it.
-- The published partner spec says UTC; the mock account uses America/Los_Angeles.

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
    reporting_timezone text     NOT NULL,

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

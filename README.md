# Offline lab. How to run it

Everything runs in Docker. No cloud access or runtime internet is
required. The first image build needs network to pull base images.

```bash
make build          # build images; first time takes a few minutes
make help           # all targets with short descriptions
```

API discrepancies versus openapi_public.yaml are in FINDINGS.txt
(required Task 1 deliverable).


## Task 1. Partner report extractor

One-shot and scheduled ingestion of the mock partner API into PostgreSQL
`partner_report`. The extractor never talks to Task 2.

### Architecture

```
mock-api :8080          warehouse :5432
        \                    /
         extractor            # make extract → python main.py → exit
         extractor-scheduler  # opt-in Compose profile `schedule`
```

`task1_api/extractor/main.py` is the only extraction entry point. It
loads Settings from the environment, runs `pipeline.run_extraction()`,
and exits 0 on success or lock-busy skip, 1 on failure.

`pipeline.py` takes a session advisory lock, plans date windows
(`window.py`), submits at most one report per 7-day chunk, validates
the snapshot (`validation/`), then `apply_snapshot()` writes
`partner_report` and `extract_state` on the same connection.
`pipeline._commit_snapshot()` commits that pair, or rolls both back.

`extractor-scheduler` is a second Compose service on the same image.
It does not embed a loop in ETL code. It runs supercronic against
`task1_api/extractor/crontab` (`*/15 * * * * /app/schedule-run.sh`),
which `cd`s to `/app` and execs `python main.py`.

Why a separate supercronic service, not cron inside the default
extractor container and not a Python `while True`:

- `make extract` keeps `CMD ["python", "main.py"]` and still exits.
- The `schedule` profile means `make api-up` and unit tests do not
  start a recurring job.
- supercronic is a container crontab: logs go to stdout, a failed job
  does not kill the scheduler, Compose stop/down stops future ticks.
- Debian cron would need mail/syslog and would fight the one-shot CMD.

Overlap: `schedule-run.sh` uses `flock -n` so a tick longer than 15
minutes skips the next one. PostgreSQL advisory lock
(`storage/lock.py`, class 7421 id 1) still serializes `make extract`
against a scheduled run. Stopping the scheduler stops future runs;
`restart: unless-stopped` does not bring back a container stopped with
`make schedule-down`.

### Environment and credentials

Compose injects these into `extractor` and `extractor-scheduler`
(`docker-compose.yml`). Names only; values stay in Compose:

- `API_BASE` — mock origin inside the network (`http://mock-api:8080`)
- `API_CLIENT_ID` / `API_CLIENT_SECRET` — OAuth client_credentials
- `PG_DSN` — warehouse DSN (password is never logged)
- `PYTHONUNBUFFERED=1` — so extractor logs reach `docker compose logs`

Host-side tests use `http://127.0.0.1:8080` and
`postgresql://…@localhost:5432/acme` via
`tests/test_storage_connection.py` (`PG_DSN` override accepted).

`EXTRACT_START_DATE` is optional and is **not** set on the scheduler.
See “Backfill” below. `LOG_LEVEL` defaults to INFO.

crontab contains no secrets. FINDINGS.txt F1 is a mismatch between
the assignment text and observed Compose/mock configuration, not an
API defect: Compose credentials match `mock_api/app.py` defaults, and
a live token request succeeded.

### Build and start the sandbox

```bash
make build
make api-up
curl -s localhost:8080/healthz
```

`make api-up` starts `mock-api` and `warehouse` only. It does not start
the scheduler. `make api-down` is `docker compose stop mock-api warehouse`
and keeps those containers, so existing Postgres files stay. In
`docker-compose.yml`, `warehouse` has no named data volume; it only
bind-mounts `task1_api/warehouse/init.sql` into
`docker-entrypoint-initdb.d` (read-only). `make clean` is
`docker compose down --remove-orphans -v` and removes containers and
any volumes Compose attached to the project — do not use it to
“reset tests”.

### One-shot extract

```bash
make extract
```

That is `docker compose run --rm extractor`. It waits until mock-api and
warehouse are healthy, runs `python main.py` once, prints the window /
row / change logs to that container’s stdout, and removes the container.
Idempotent: a second run replaces the same date slice in one transaction
and does not duplicate the primary key `(report_date, country, channel)`.

### Automatic 15-minute scheduling (opt-in)

```bash
make schedule-up      # mock-api + warehouse + extractor-scheduler
make schedule-logs    # follow supercronic + python stdout/stderr
make schedule-down    # stop only extractor-scheduler
```

Equivalent Compose:

```bash
docker compose --profile schedule up -d --build mock-api warehouse extractor-scheduler
docker compose --profile schedule logs -f extractor-scheduler
docker compose --profile schedule stop extractor-scheduler
```

Do not set `EXTRACT_START_DATE` here. First incremental run uses
`extract_state`; a persistent start date would re-request history every
tick.

### Inspect PostgreSQL

```bash
make psql
```

Useful statements:

```sql
SELECT count(*) FROM partner_report;

SELECT min(report_date), max(report_date), count(*) FROM partner_report;
SELECT report_date, country, channel, spend_minor, revenue_minor, currency,
       reporting_timezone, data_version
  FROM partner_report
 ORDER BY report_date, country, channel
 LIMIT 20;
SELECT key, value, updated_at FROM extract_state;
```

`extract_state` key `partner_report` holds watermarks (`loaded_from`,
`loaded_through`, `settled_through`), last `report_id` / `data_version`,
and frozen currency / timezone. Stage 6.1 left `public.partner_report`
at 420 rows; isolated tests use schema `e2e_reconcile_test` and drop it.

### Tests

Warehouse and mock-api must be up for integration / live tests
(`make api-up`). Tests create and drop their own schemas; they do not
truncate `public.partner_report` and they do not take the production
advisory lock (test lock ids 91043 / 91044 / 61001).

```bash
cd task1_api/extractor
python3 -m unittest discover -s tests
python3 -m unittest tests.test_end_to_end -v
```

202 tests passed in Stage 6.1 with both services available (0 skipped).
PostgreSQL-backed classes use `@unittest.skipUnless(postgres_available())`.
The live API reconciliation class also skips unless
`http://127.0.0.1:8080/healthz` answers. That is the coverage that was
run, not a claim that every live failure mode was waited out for 15
minutes.

### Backfill and EXTRACT_START_DATE

`window.plan_extract_windows()`:

- First run, env unset: last 7 inclusive account-calendar days
  (UTC−7 `account_today`, not IANA Los Angeles).
- First run, `EXTRACT_START_DATE=YYYY-MM-DD`: that date through today,
  split into chunks of at most 7 inclusive days, oldest first.
- Later runs: start at `settled_through + 1`. If nothing has settled,
  start at `loaded_from` (contiguous prefix), not the last snapshot’s
  `last_date_from`.
- If `EXTRACT_START_DATE` is earlier than that cursor, extra backfill
  chunks run first, then incremental. A later start date cannot skip
  an unprocessed gap.

One-shot example (do not put this on the scheduler):

```bash
docker compose run --rm -e EXTRACT_START_DATE=2026-01-01 extractor
```

### Missing dates, mutable history, crashes

- Downtime: the next successful run plans from `settled_through + 1`
  through account today, in 7-day chunks, so missed days are requested
  in order with no holes.
- Mutable tail: dates `>= restated_through` (account attribution window,
  3 days on the mock) are rewritten every run. Settlement does not
  advance over them.
- Never-settled multi-chunk: `loaded_from` keeps the earliest prefix
  date so a later 7-day chunk cannot forget the earlier mutable days.
- Crash / exception during `apply_snapshot`: the load connection rolls
  back; `partner_report` and `extract_state` stay at the last commit.
- Failure after chunk N: chunks 1..N remain committed; later chunks
  are not started as success. The next run resumes from watermarks.
- Concurrent processes: `pg_try_advisory_lock` on a dedicated
  autocommit connection; the loser exits 0 (`skipped`) without writes.

### Why data and checkpoints commit together

`replace_date_slice()` and the `extract_state` upsert run on one
non-autocommit connection and do not commit (`storage/loader.py`,
`storage/state.py`). Only `pipeline._commit_snapshot()` calls
`conn.commit()`. A mid-write exception triggers `conn.rollback()`.
Advancing the watermark before the rows were durable would skip dates;
committing rows without a watermark would reload forever. Logging says
“Pending … (uncommitted)” until that commit.

### How correctness is checked

Stage 6.1 `tests/test_end_to_end.py` fetches one real mock report,
loads it into schema `e2e_reconcile_test`, and compares every grain
`(report_date, country, channel)` and every metric including integer
`spend_minor` / `revenue_minor`, plus currency, timezone, and
`data_version`, to **that** `report_id`. Independently generated
mutable reports are not required to match across `version_epoch`.

Observed live snapshot (same epoch as the 6.1 write-up): 420 source
rows, 420 stored rows, 0 missing / extra / duplicate / mismatched.
`public.partner_report` was left at 420 rows.

### Known limitations

- `POST /v2/reports` is not retried after the request may have reached
  the partner (`AmbiguousRequestError`). A lost job after mock-api
  restart is retried at most once on GET 404.
- Legacy `extract_state` JSON without `loaded_from` falls back to
  `last_date_from`, which is wrong for never-settled multi-chunk
  recovery; current writes always persist `loaded_from`.
- Default first run is 7 days, not full partner history.
- Mock source keeps jobs in process memory (`_jobs`). A restart of
  `mock-api` is expected to drop `report_id`s; that restart was not
  run against the live sandbox.
- Account calendar is fixed UTC−7, not IANA `America/Los_Angeles`.
- `docker-compose.yml` declares no named volume for Postgres data.
  `make api-down` (`docker compose stop`) keeps the warehouse
  container. `make clean` (`down -v`) removes containers and any
  volumes Compose attached to the project.
- Live reconciliation covered one 7-day window, not a live 15-minute
  wait or a live version-epoch roll.
- Scheduler must be started with the absolute supercronic path
  (`/usr/local/bin/supercronic`); relative argv[0] fails PID-1 re-exec.


## Task 2. Query estate

```bash
make estate-stats   # checks that the estate files are readable
make shell          # python 3 + sqlglot + networkx + pandas + duckdb + jq
make compile        # compiles task2_estate/dataform; must exit 0
```

**There is no data warehouse for Task 2.** `raw.*` does not exist
anywhere you can query. None of the 129 queries will run, and
`dataform run` will not work. Only `dataform compile` works. Output
table columns and types are described only in `schemas.json`.

This is a constraint of the exercise, not an omission. What to do
about it is in the assignment, in the section on `PLAN.md` and
Equivalence.

## Lab image tools

| Tool | Why |
|---|---|
| `dataform` CLI 3.x | the only way to check your actions without a warehouse |
| `sqlglot` | parse BigQuery SQL; needed for the dependency graph |
| `networkx` | graph algorithms, if you want them |
| `pandas`, `duckdb` | query CSV files as tables |
| `jq` | for `transfer_configs.json` |

None of this is mandatory; use your own toolkit if you prefer. Only
two things are fixed: `make compile` must exit 0, and your tooling
must live in the repository.

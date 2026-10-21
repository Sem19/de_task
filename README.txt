How to run this repository

Docker is required. The first make build needs network access for base
images. After that, Task 1 and make compile do not need a warehouse or a
cloud project. The longer write-up of the same commands is README.md.

Layout
  task1_api/extractor/     partner-report extractor (entry point main.py)
  task1_api/mock_api/      mock partner API
  task2_estate/            129 scheduled queries and their metadata
  task2_estate/dataform/   implemented revenue_daily slice
  tools/                   estate loader, SQL parser, graph, audit, cost
  FINDINGS.txt             Task 1 API discrepancies
  AUDIT.txt                Task 2 problem register, generated
  COST.txt                 Task 2 cost estimates, generated
  PLAN.txt                 migration scope, what was built, equivalence plan
  DECISIONS.txt            decisions and owner questions

Setup
  make build
  make help

Task 1
  make api-up
  curl -s localhost:8080/healthz
  make extract
  make psql
  make schedule-up
  make schedule-down

  Findings versus openapi_public.yaml are in FINDINGS.txt.

Task 2 reports
  cd tools
  python3 -m audit_report --output ../AUDIT.txt
  python3 -m cost_report --output ../COST.txt

  make estate-stats only checks that the estate files are readable.
  It does not regenerate AUDIT.txt or COST.txt.

Dataform
  make compile

  The project that compiles is 13 targets:
    3 declarations   raw.payments, raw.subscriptions, raw.fx_rates
    6 tables         stg_payments, stg_subscriptions, stg_fx_daily,
                     reports.revenue_daily (sq_0104),
                     reports.revenue_by_region (sq_0106),
                     marts.revenue_unified (sq_5001)
    4 assertions     uniqueness of the grains those tables already produce

  The CLI summary counts 10 actions. That line omits declarations.
  sq_0105 is not in the graph. It remains an enabled hourly WRITE_APPEND
  to reports.revenue_daily. Disable it at production cutover, before
  Dataform writes that table. The hourly grain stays on sq_0114.
  See DECISIONS.txt section 2 and PLAN.txt section 4.

  sq_0111 (reports.exec_kpi_daily), sq_0112 (reports.kpi_snapshot), and
  sq_5005 (marts.exec_kpi_v2) are not implemented. sq_0111 and sq_5005
  filter payment_date after the upstream statement has renamed that
  column to report_date. sq_0112 reads metric_name and metric_value,
  which sq_0111 does not project, and it reads its own destination for
  history. schemas.json and table_catalog.csv do not name those columns.
  No substitute mapping was added. The three scheduled queries stay
  enabled. The owner questions are in DECISIONS.txt.

  dataform run will not succeed. There is no BigQuery project here.
  A clean compile is not a row-level match to production.

Tests
  cd tools
  python3 -m pytest . -p no:importnb -q
  python3 -m ruff check .
  python3 -m ruff format --check .

  Task 1 unit tests, with the API and Postgres up:

    cd task1_api/extractor
    python3 -m unittest discover -s tests

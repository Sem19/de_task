"""Tests for scheduled-query estate loading and identifier normalization."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tools.estate import (
    EstateInputError,
    config_id_from_transfer_name,
    format_summary,
    load_inventory,
    parse_sql_filename,
    parse_table_id,
)
from tools.inventory import main

_HISTORY_HEADER = (
    "run_id,config_id,destination,schedule_time,start_time,end_time,"
    "state,error_message,total_bytes_processed,rows_written"
)
_USAGE_HEADER = "table,consumer_kind,consumer_id,queries_30d"
_CATALOG_HEADER = "table,type,row_count,size_bytes,partition_column,clustering,created,last_modified,columns"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(
    config_id: str,
    dataset: str,
    table: str,
    query: str,
    *,
    state: str = "ENABLED",
    disabled: bool = False,
    write_disposition: str = "WRITE_TRUNCATE",
) -> dict[str, object]:
    return {
        "name": f"projects/1/locations/eu/transferConfigs/{config_id}",
        "displayName": config_id,
        "destinationDatasetId": dataset,
        "schedule": "every day 03:00",
        "scheduleOptionsV2": {"timeZone": "Europe/Kyiv"},
        "state": state,
        "disabled": disabled,
        "params": {
            "query": query,
            "destination_table_name_template": table,
            "write_disposition": write_disposition,
        },
        "_meta_sql_file": f"scheduled_queries/{config_id}__{dataset}__{table}.sql",
    }


def _write_estate(
    root: Path,
    *,
    sql_files: dict[str, str],
    configs: list[dict[str, object]],
    history: list[str] | None = None,
    usage: list[str] | None = None,
    catalog: list[str] | None = None,
    schemas: dict[str, list[dict[str, str]]] | None = None,
    configs_text: str | None = None,
    history_text: str | None = None,
) -> None:
    sql_dir = root / "scheduled_queries"
    sql_dir.mkdir(parents=True, exist_ok=True)
    for name, text in sql_files.items():
        (sql_dir / name).write_bytes(text.encode("utf-8"))
    if configs_text is None:
        configs_text = json.dumps({"transferConfigs": configs})
    (root / "transfer_configs.json").write_text(configs_text, encoding="utf-8")
    history_body = "\n".join(history or [])
    (root / "run_history.csv").write_text(
        history_text
        if history_text is not None
        else f"{_HISTORY_HEADER}\n{history_body}\n",
        encoding="utf-8",
    )
    usage_body = "\n".join(usage or [])
    (root / "downstream_usage.csv").write_text(
        f"{_USAGE_HEADER}\n{usage_body}\n",
        encoding="utf-8",
    )
    catalog_body = "\n".join(catalog or [])
    (root / "table_catalog.csv").write_text(
        f"{_CATALOG_HEADER}\n{catalog_body}\n",
        encoding="utf-8",
    )
    (root / "schemas.json").write_text(
        json.dumps(schemas or {}),
        encoding="utf-8",
    )


def _history(config_id: str, destination: str) -> str:
    return (
        f"run-{config_id},{config_id},{destination},2026-01-01T00:00:00+00:00,"
        "2026-01-01T00:00:01+00:00,2026-01-01T00:00:02+00:00,SUCCEEDED,,10,1"
    )


def _catalog(table: str) -> str:
    return f"{table},BASE TABLE,1,2,,,2026-01-01,2026-01-01T00:00:00+00:00,1"


class IdentifierTest(unittest.TestCase):
    def test_dataset_table(self) -> None:
        parsed = parse_table_id("staging.stg_payments")
        self.assertIsNone(parsed.project)
        self.assertEqual(parsed.dataset_table, "staging.stg_payments")
        self.assertIsNone(parsed.fully_qualified)

    def test_fully_qualified_keeps_project(self) -> None:
        parsed = parse_table_id("`acme-analytics-prod.raw.payments`")
        self.assertEqual(parsed.project, "acme-analytics-prod")
        self.assertEqual(parsed.dataset, "raw")
        self.assertEqual(parsed.table, "payments")
        self.assertEqual(parsed.dataset_table, "raw.payments")
        self.assertEqual(parsed.fully_qualified, "acme-analytics-prod.raw.payments")

    def test_colon_form_keeps_project(self) -> None:
        parsed = parse_table_id("acme-analytics-prod:raw.payments")
        self.assertEqual(parsed.project, "acme-analytics-prod")
        self.assertEqual(parsed.dataset_table, "raw.payments")

    def test_strips_surrounding_whitespace(self) -> None:
        parsed = parse_table_id("  raw.events  ")
        self.assertEqual(parsed.dataset_table, "raw.events")

    def test_rejects_incomplete_identifiers(self) -> None:
        for value in ("", "payments", "raw.", "a.b.c.d", "proj:only"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_table_id(value)

    def test_config_id_from_filename_and_transfer_name(self) -> None:
        parsed = parse_sql_filename("sq_0001__staging__stg_payments.sql")
        self.assertEqual(parsed.config_id, "sq_0001")
        self.assertEqual(parsed.dataset, "staging")
        self.assertEqual(parsed.table, "stg_payments")
        self.assertEqual(
            config_id_from_transfer_name(
                "projects/418822/locations/eu/transferConfigs/sq_0001"
            ),
            "sq_0001",
        )
        with self.assertRaises(ValueError):
            parse_sql_filename("stg_payments.sql")
        with self.assertRaises(ValueError):
            config_id_from_transfer_name("   /  ")


class InventoryMappingTest(unittest.TestCase):
    def test_valid_input_maps_sql_history_and_usage(self) -> None:
        query = "select 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0001__raw__events.sql": query},
                configs=[_config("sq_0001", "raw", "events", query)],
                history=[_history("sq_0001", "acme-analytics-prod:raw.events")],
                usage=["`acme-analytics-prod.raw.events`,scheduled_query,sq_0001,4"],
                catalog=[_catalog("raw.events")],
                schemas={
                    "acme-analytics-prod.raw.events": [
                        {"name": "event_id", "type": "STRING"}
                    ],
                },
            )
            inventory = load_inventory(root)

        self.assertEqual(len(inventory.queries), 1)
        mapped = inventory.queries[0]
        self.assertEqual(mapped.config_id, "sq_0001")
        self.assertEqual(mapped.destination_dataset, "raw")
        self.assertEqual(mapped.destination_table, "events")
        self.assertEqual(mapped.schedule, "every day 03:00")
        self.assertEqual(mapped.timezone, "Europe/Kyiv")
        self.assertTrue(mapped.enabled)
        self.assertEqual(mapped.state, "ENABLED")
        self.assertFalse(mapped.disabled)
        self.assertEqual(mapped.write_disposition, "WRITE_TRUNCATE")
        self.assertEqual(mapped.sql_text, query)
        self.assertEqual(mapped.params_query, query)
        self.assertEqual(mapped.raw_config["params"]["query"], query)
        self.assertEqual(
            mapped.destination.dataset_table if mapped.destination else None,
            "raw.events",
        )
        self.assertIsNone(
            mapped.destination.project if mapped.destination else "missing"
        )

        self.assertEqual(inventory.history[0].query, mapped)
        self.assertEqual(
            inventory.history[0].destination.project
            if inventory.history[0].destination
            else None,
            "acme-analytics-prod",
        )
        self.assertEqual(inventory.history[0].raw["total_bytes_processed"], "10")

        self.assertEqual(inventory.usage[0].query, mapped)
        self.assertEqual(inventory.usage[0].writer_config_ids, ("sq_0001",))
        self.assertEqual(
            inventory.usage[0].table.dataset_table
            if inventory.usage[0].table
            else None,
            "raw.events",
        )
        self.assertEqual(inventory.catalog[0].writer_config_ids, ("sq_0001",))
        self.assertTrue(inventory.schemas[0].in_catalog)
        self.assertEqual(inventory.schemas[0].table.project, "acme-analytics-prod")
        self.assertEqual(inventory.schemas[0].columns[0].name, "event_id")
        self.assertEqual([finding.kind for finding in inventory.findings], [])
        self.assertIn("sql_files: 1", format_summary(inventory))
        self.assertIn("mappings: consistent", format_summary(inventory))

    def test_config_id_comes_from_filename_and_transfer_name(self) -> None:
        query = "select 1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0007__reports__revenue_daily.sql": query},
                configs=[_config("sq_0007", "reports", "revenue_daily", query)],
                catalog=[_catalog("reports.revenue_daily")],
            )
            inventory = load_inventory(root)
        mapped = inventory.queries[0]
        self.assertEqual(
            parse_sql_filename(mapped.sql_path.name).config_id, mapped.config_id
        )
        self.assertEqual(
            config_id_from_transfer_name(mapped.transfer_name or ""), mapped.config_id
        )

    def test_missing_config_and_missing_sql(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0002__raw__events.sql": "select 1"},
                configs=[_config("sq_0003", "raw", "users", "select 2")],
            )
            inventory = load_inventory(root)
        kinds = Counter(finding.kind for finding in inventory.findings)
        self.assertEqual(kinds["missing_config"], 1)
        self.assertEqual(kinds["missing_sql"], 1)
        self.assertEqual(inventory.queries, ())
        self.assertEqual(inventory.sql_files[0].text, "select 1")
        self.assertEqual(inventory.transfer_configs[0].params_query, "select 2")

    def test_duplicate_config_ids_in_files_and_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={
                    "sq_0004__raw__events.sql": "select 1",
                    "sq_0004__raw__users.sql": "select 2",
                },
                configs=[
                    _config("sq_0004", "raw", "events", "select 1"),
                    _config("sq_0004", "raw", "users", "select 2"),
                ],
            )
            inventory = load_inventory(root)
        duplicates = [
            finding
            for finding in inventory.findings
            if finding.kind == "duplicate_config_id"
        ]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].config_id, "sq_0004")
        self.assertEqual(len(duplicates[0].evidence["sql_paths"]), 2)
        self.assertEqual(len(duplicates[0].evidence["transfer_names"]), 2)
        self.assertEqual(inventory.queries, ())

    def test_sql_text_mismatch_keeps_both_originals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0005__raw__events.sql": "select 1\n"},
                configs=[_config("sq_0005", "raw", "events", "select 2\n")],
                catalog=[_catalog("raw.events")],
            )
            inventory = load_inventory(root)
        mismatches = [
            finding
            for finding in inventory.findings
            if finding.kind == "sql_text_mismatch"
        ]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].evidence["file_text"], "select 1\n")
        self.assertEqual(mismatches[0].evidence["params_query"], "select 2\n")
        self.assertEqual(inventory.queries[0].sql_text, "select 1\n")
        self.assertEqual(inventory.queries[0].params_query, "select 2\n")

    def test_destination_in_filename_must_match_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0006__staging__stg_payments.sql": "select 1"},
                configs=[_config("sq_0006", "reports", "revenue_daily", "select 1")],
            )
            inventory = load_inventory(root)
        kinds = {finding.kind for finding in inventory.findings}
        self.assertIn("destination_mismatch", kinds)
        self.assertIn("meta_sql_file_mismatch", kinds)
        self.assertEqual(inventory.queries[0].destination_dataset, "reports")
        self.assertEqual(inventory.queries[0].filename_dataset, "staging")

    def test_enabled_flag_requires_state_and_disabled_to_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0008__raw__events.sql": "select 1"},
                configs=[
                    _config(
                        "sq_0008",
                        "raw",
                        "events",
                        "select 1",
                        state="ENABLED",
                        disabled=True,
                    )
                ],
                catalog=[_catalog("raw.events")],
            )
            inventory = load_inventory(root)
        self.assertIsNone(inventory.queries[0].enabled)
        self.assertTrue(inventory.queries[0].disabled)
        self.assertIn(
            "enabled_state_mismatch", {finding.kind for finding in inventory.findings}
        )

    def test_history_destination_and_unknown_config_are_reported(self) -> None:
        query = "select 1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0009__raw__events.sql": query},
                configs=[_config("sq_0009", "raw", "events", query)],
                history=[
                    _history("sq_0009", "raw.other"),
                    _history("sq_missing", "raw.events"),
                ],
                usage=[
                    "raw.events,scheduled_query,sq_missing,2",
                    "raw.ghost,dashboard,looker/x,3",
                ],
                catalog=[_catalog("raw.events")],
            )
            inventory = load_inventory(root)
        kinds = Counter(finding.kind for finding in inventory.findings)
        self.assertEqual(kinds["history_destination_mismatch"], 1)
        self.assertEqual(kinds["history_unknown_config"], 1)
        self.assertEqual(kinds["usage_unknown_config"], 1)
        self.assertEqual(kinds["usage_table_not_in_catalog"], 1)
        linked = [row for row in inventory.history if row.config_id == "sq_0009"]
        self.assertEqual(
            linked[0].query.config_id if linked[0].query else None, "sq_0009"
        )
        self.assertEqual(linked[0].raw["destination"], "raw.other")


class MalformedInputTest(unittest.TestCase):
    def test_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={},
                configs=[],
                configs_text="{not json",
            )
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("malformed JSON", str(caught.exception))

    def test_json_object_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(root, sql_files={}, configs=[], configs_text="[]")
            with self.assertRaises(EstateInputError):
                load_inventory(root)

    def test_malformed_csv_missing_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={},
                configs=[],
                history_text="run_id,config_id\n1,sq_0001\n",
            )
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("missing columns", str(caught.exception))

    def test_malformed_csv_extra_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={},
                configs=[],
                history_text=f"{_HISTORY_HEADER}\n{_history('sq_0001', 'raw.events')},extra\n",
            )
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("extra fields", str(caught.exception))

    def test_malformed_csv_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(root, sql_files={}, configs=[], history_text="")
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("missing header", str(caught.exception))


class RealEstateTest(unittest.TestCase):
    def test_repository_inventory_is_internally_consistent(self) -> None:
        inventory = load_inventory(_REPO_ROOT / "task2_estate")
        self.assertGreater(len(inventory.sql_files), 0)
        self.assertEqual(len(inventory.sql_files), len(inventory.transfer_configs))
        self.assertEqual(len(inventory.queries), len(inventory.sql_files))
        self.assertEqual(
            len(inventory.catalog), len({row.raw["table"] for row in inventory.catalog})
        )
        self.assertGreater(len(inventory.history), 0)
        self.assertGreater(len(inventory.usage), 0)
        self.assertGreater(len(inventory.schemas), 0)

        absent = {
            "missing_sql",
            "missing_config",
            "missing_config_id",
            "duplicate_config_id",
            "sql_text_mismatch",
            "destination_mismatch",
            "meta_sql_file_mismatch",
            "enabled_state_mismatch",
            "incomplete_config",
            "unparsed_sql_filename",
            "unparsed_table_id",
            "history_unknown_config",
            "history_destination_mismatch",
            "history_table_not_in_catalog",
            "usage_unknown_config",
            "schema_not_in_catalog",
        }
        kinds = Counter(finding.kind for finding in inventory.findings)
        self.assertTrue(absent.isdisjoint(kinds))

        for query in inventory.queries:
            self.assertEqual(
                parse_sql_filename(query.sql_path.name).config_id, query.config_id
            )
            self.assertEqual(
                config_id_from_transfer_name(query.transfer_name or ""), query.config_id
            )
            self.assertEqual(query.sql_text, query.params_query)
            self.assertEqual(
                query.sql_text, query.sql_path.read_bytes().decode("utf-8")
            )
            self.assertEqual(query.params_query, query.raw_config["params"]["query"])
            self.assertEqual(query.state, query.raw_config["state"])
            self.assertEqual(query.disabled, query.raw_config["disabled"])
            self.assertEqual(query.filename_dataset, query.destination_dataset)
            self.assertEqual(query.filename_table, query.destination_table)
            self.assertIsNotNone(query.enabled)
            self.assertIsNotNone(query.destination)

        for row in inventory.history:
            self.assertIsNotNone(row.query)
            self.assertIsNotNone(row.destination)
            self.assertEqual(row.raw["config_id"], row.config_id)
            assert (
                row.query is not None
                and row.destination is not None
                and row.query.destination is not None
            )
            self.assertEqual(
                row.destination.dataset_table, row.query.destination.dataset_table
            )

        for row in inventory.usage:
            if row.consumer_kind == "scheduled_query":
                self.assertIsNotNone(row.query)
                assert row.query is not None
                self.assertEqual(row.query.config_id, row.consumer_id)

        catalog_tables = {
            row.table.dataset_table
            for row in inventory.catalog
            if row.table is not None
        }
        for table in inventory.schemas:
            self.assertTrue(table.in_catalog)
            self.assertIn(table.table.dataset_table, catalog_tables)

        for finding in inventory.findings:
            if finding.kind == "usage_table_not_in_catalog":
                self.assertNotIn(
                    parse_table_id(finding.evidence["table"]).dataset_table,
                    catalog_tables,
                )
            if finding.kind == "shared_destination":
                config_ids = [item["config_id"] for item in finding.evidence["configs"]]
                self.assertGreaterEqual(len(config_ids), 2)
                destinations = {
                    query.destination.dataset_table
                    for query in inventory.queries
                    if query.config_id in config_ids and query.destination is not None
                }
                self.assertEqual(destinations, {finding.evidence["table"]})


class ContractTest(unittest.TestCase):
    def test_missing_file_is_a_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(root, sql_files={}, configs=[])
            (root / "schemas.json").unlink()
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("schemas.json", str(caught.exception))

    def test_missing_sql_directory_is_a_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(EstateInputError) as caught:
                load_inventory(root)
        self.assertIn("scheduled_queries", str(caught.exception))

    def test_unparsed_filename_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"notes.sql": "select 1"},
                configs=[],
            )
            inventory = load_inventory(root)
        self.assertEqual(inventory.sql_files[0].config_id, None)
        self.assertEqual(inventory.sql_files[0].text, "select 1")
        self.assertIn(
            "unparsed_sql_filename", {finding.kind for finding in inventory.findings}
        )

    def test_unparsed_destination_is_a_finding(self) -> None:
        query = "select 1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config("sq_0010", "raw", "not a table", query)
            _write_estate(
                root,
                sql_files={"sq_0010__raw__events.sql": query},
                configs=[config],
            )
            inventory = load_inventory(root)
        kinds = {finding.kind for finding in inventory.findings}
        self.assertIn("unparsed_table_id", kinds)
        self.assertIn("destination_mismatch", kinds)
        self.assertIsNone(inventory.queries[0].destination)
        self.assertEqual(inventory.queries[0].destination_table, "not a table")

    def test_repeated_loads_match(self) -> None:
        query = "select 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_estate(
                root,
                sql_files={"sq_0011__raw__events.sql": query},
                configs=[_config("sq_0011", "raw", "events", query)],
                history=[_history("sq_0011", "raw.events")],
                usage=["raw.events,scheduled_query,sq_0011,1"],
                catalog=[_catalog("raw.events")],
                schemas={"raw.events": [{"name": "event_id", "type": "STRING"}]},
            )
            first = load_inventory(root)
            second = load_inventory(root)
        self.assertEqual(first, second)
        self.assertEqual(format_summary(first), format_summary(second))
        self.assertEqual([query.config_id for query in first.queries], ["sq_0011"])

    def test_real_estate_load_is_deterministic(self) -> None:
        root = _REPO_ROOT / "task2_estate"
        first = load_inventory(root)
        second = load_inventory(root)
        self.assertEqual(format_summary(first), format_summary(second))
        self.assertEqual(
            [
                (finding.kind, finding.config_id, finding.message)
                for finding in first.findings
            ],
            [
                (finding.kind, finding.config_id, finding.message)
                for finding in second.findings
            ],
        )
        self.assertEqual(
            [query.config_id for query in first.queries],
            [query.config_id for query in second.queries],
        )
        self.assertEqual(
            [row.raw["run_id"] for row in first.history],
            [row.raw["run_id"] for row in second.history],
        )

    def test_cli_logs_a_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("tools.inventory", level="ERROR") as captured:
                code = main([tmp])
        self.assertEqual(code, 2)
        self.assertTrue(any("scheduled_queries" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()

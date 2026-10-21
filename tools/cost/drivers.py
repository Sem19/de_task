"""Describe the largest cost drivers from SQL, catalog, and schedule facts.

The text names what the files show and what to verify in BigQuery. It does
not estimate a percentage reduction or a replacement scan.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from audit.common import consumers, table_name as dataset_table
from audit.models import AuditFinding
from audit.sql_ast import partition_coverage, retention_days
from cost.models import ConfigCost
from cost.report_models import DriverReview, Recommendation, Scenario
from estate.models import Inventory, ScheduledQuery
from graph.models import Graph

TOP_CONTRIBUTORS = 10
DRIVER_LIMIT = 4

_OWNER = re.compile(r"^owner:\s*(.+)$", re.IGNORECASE)


def top_contributors(configs: tuple[ConfigCost, ...]) -> tuple[ConfigCost, ...]:
    """Return the first priced configs in the engine's rank order.

    Args:
        configs: Configurations already ranked by daily rate, then config_id.

    Returns:
        At most ``TOP_CONTRIBUTORS`` configs that have a daily rate.
    """
    priced = tuple(
        config for config in configs if config.normalized_daily_run_rate_usd is not None
    )
    return priced[:TOP_CONTRIBUTORS]


def review_drivers(
    inventory: Inventory,
    graph: Graph,
    findings: tuple[AuditFinding, ...],
    ranked: tuple[ConfigCost, ...],
) -> tuple[DriverReview, ...]:
    """Review the largest priced configs. Selection follows the cost ranking.

    Args:
        inventory: SQL, schedules, catalog partition columns, and usage.
        graph: Read edges and downstream query reads.
        findings: Audit findings, used only to list ids that name the config.
        ranked: Engine ranking.

    Returns:
        At most ``DRIVER_LIMIT`` reviews, taken from the front of the top
        contributors, in that order.
    """
    queries = {query.config_id: query for query in inventory.queries}
    partitions = _partition_columns(inventory)
    reviews = []
    for config in top_contributors(ranked)[:DRIVER_LIMIT]:
        reviews.append(
            _review(
                config,
                queries.get(config.config_id),
                inventory,
                graph,
                findings,
                partitions,
            )
        )
    return tuple(reviews)


def prioritize(
    reviews: tuple[DriverReview, ...],
    scenarios: tuple[Scenario, ...],
) -> tuple[Recommendation, ...]:
    """Order next steps. Hypothetical schedule math is not a priority action.

    Args:
        reviews: Largest-driver reviews.
        scenarios: Priced scenarios. Their dollar amounts are not recomputed.

    Returns:
        Recommendations numbered from 1. Driver investigations come first,
        then the no-recorded-consumer confirmation, then one comparison
        covering every assumed-replacement scenario, then a warning when a
        hypothetical scenario exists, then one cleanup item per zero-scan
        scenario.
        Conditionally avoidable cost is set only for the no-recorded-consumer
        scenario.
    """
    items: list[Recommendation] = []
    for review in reviews:
        items.append(
            Recommendation(
                rank=0,
                heading=f"Investigate {review.config_id}",
                config_ids=(review.config_id,),
                observed_daily_cost_usd=review.daily_cost_usd,
                conditionally_avoidable_usd=None,
                body=review.investigation,
            )
        )
    for scenario in scenarios:
        if scenario.money_role != "conditional_avoidable":
            continue
        items.append(
            Recommendation(
                rank=0,
                heading="Confirm queries with no recorded consumer before stopping them",
                config_ids=scenario.stops,
                observed_daily_cost_usd=scenario.daily_savings_usd,
                conditionally_avoidable_usd=scenario.daily_savings_usd,
                body=(
                    "The amount is the observed daily scan cost of those queries. "
                    "It is conditionally avoidable only if an owner confirms that "
                    "the usage extract and the graph did not miss a reader. "
                    "It is not a guaranteed saving. Do not delete the tables. "
                    + scenario.evidence
                ),
            )
        )
    assumed = [
        scenario.scenario_id
        for scenario in scenarios
        if scenario.money_role == "assumed_replacement"
    ]
    if assumed:
        items.append(
            Recommendation(
                rank=0,
                heading="Compare destinations before stopping a repeated or competing query",
                config_ids=(),
                observed_daily_cost_usd=None,
                conditionally_avoidable_usd=None,
                body=(
                    "Repeated SQL and competing writers are priced in the "
                    "scenario section as the observed cost of the copies that "
                    "would stop. Those figures assume a replacement that was "
                    "not run. Scenarios: " + ", ".join(assumed) + "."
                ),
            )
        )
    if any(scenario.money_role == "hypothetical" for scenario in scenarios):
        items.append(
            Recommendation(
                rank=0,
                heading="Do not apply the hourly collapse from this report",
                config_ids=(),
                observed_daily_cost_usd=None,
                conditionally_avoidable_usd=None,
                body=(
                    "The scenario section includes a hypothetical difference "
                    "for running each hourly query once per observed day. "
                    "That figure assumes each omitted execution scans the same "
                    "bytes as the average recorded execution. It is not a "
                    "recommended action and it is not conditionally avoidable "
                    "cost."
                ),
            )
        )
    cleanup = [
        scenario
        for scenario in scenarios
        if scenario.money_role == "operational_cleanup"
    ]
    for scenario in cleanup:
        items.append(
            Recommendation(
                rank=0,
                heading="Treat zero-scan failures as operational cleanup",
                config_ids=scenario.stops,
                observed_daily_cost_usd=scenario.daily_savings_usd,
                conditionally_avoidable_usd=None,
                body=(
                    "These enabled schedules already have an estimated scan "
                    "cost of zero. Disabling them does not reduce the "
                    "estimated bill. The failures still need an owner. "
                    "Do not delete the destination tables. " + scenario.evidence
                ),
            )
        )
    return tuple(item._replace(rank=index) for index, item in enumerate(items, start=1))


def _review(
    config: ConfigCost,
    query: ScheduledQuery | None,
    inventory: Inventory,
    graph: Graph,
    findings: tuple[AuditFinding, ...],
    partitions: dict[str, str],
) -> DriverReview:
    """Assemble one driver review from the files already loaded.

    Partition coverage is skipped when the query has no SQL or the catalog
    has no partition columns. Finding ids stay in finding order.

    Args:
        config: Ranked config estimate.
        query: Paired query, or ``None``.
        inventory: Usage rows, used to see whether the destination has a reader.
        graph: Certain reads for the scan text. Any read counts as a reader.
        findings: Audit findings. An id is listed when it names this config.
        partitions: ``dataset.table`` to catalog partition column.

    Returns:
        The review. It does not estimate a byte reduction.
    """
    sql = "" if query is None else query.sql_text
    tree = _parse(sql)
    retention = retention_days(sql) if sql else None
    coverage = partition_coverage(sql, partitions) if sql and partitions else None
    intervals = () if tree is None else _day_intervals(tree)
    sources = _sources(graph, config.config_id)
    comments = _comments(sql)
    owner = _owner(comments)
    header = next((line for line in comments if _OWNER.match(line) is None), None)
    dates = len(config.observed_schedule_dates)
    pattern = _pattern(config, query, dates, header, owner)
    scan = _scan(
        config,
        dates,
        sources,
        partitions,
        coverage,
        retention.days if retention is not None else None,
        bool(retention and retention.uncertain),
        intervals,
        tree,
    )
    investigation = _investigation(
        config,
        header,
        coverage,
        partitions,
        retention.days if retention is not None else None,
        intervals,
        tree,
    )
    risks = _risks(config, query, inventory, graph, tree, intervals)
    verification = _verification(coverage)
    named = tuple(
        finding.finding_id
        for finding in findings
        if config.config_id in finding.config_ids
    )
    return DriverReview(
        config_id=config.config_id,
        destination=config.destination,
        sql_path=config.sql_path,
        frequency=config.frequency,
        schedule=config.schedule,
        write_disposition=None if query is None else query.write_disposition,
        daily_cost_usd=config.normalized_daily_run_rate_usd,
        daily_tib=config.normalized_daily_scanned_tib,
        finding_ids=named,
        processing_pattern=pattern,
        scan_volume=scan,
        investigation=investigation,
        risks=risks,
        verification=verification,
    )


def _pattern(
    config: ConfigCost,
    query: ScheduledQuery | None,
    dates: int,
    header: str | None,
    owner: str | None,
) -> str:
    """Describe the schedule, destination, and SQL comments.

    Args:
        config: Config estimate. Its schedule text is quoted as stored.
        query: Paired query. A missing query makes the disposition unknown.
        dates: Number of distinct observed schedule dates.
        header: First full-line comment that is not an owner line.
        owner: Text of the first ``owner:`` comment, or ``None``.

    Returns:
        One paragraph. A comment containing ``daily`` on an hourly schedule
        is called out.
    """
    disposition = "unknown" if query is None else (query.write_disposition or "unknown")
    parts = [
        f"Schedule: {config.schedule or 'unknown'} ({config.frequency}). "
        f"Write disposition: {disposition}. "
        f"Destination: {config.destination or 'unknown'}. "
        f"Recorded executions: {config.execution_count} across {dates} observed "
        f"schedule dates, {config.failed_count} failed."
    ]
    if header:
        parts.append(f"SQL comment: {header}.")
    if owner:
        parts.append(f"Owner comment: {owner}.")
    if header and "daily" in header.lower() and config.frequency == "hourly":
        parts.append(
            "The SQL comment contains 'daily' and the transfer schedule is hourly."
        )
    return " ".join(parts)


def _scan(
    config: ConfigCost,
    dates: int,
    sources: tuple[str, ...],
    partitions: dict[str, str],
    coverage,
    lookback_days: int | None,
    lookback_uncertain: bool,
    intervals: tuple[int, ...],
    tree: exp.Expression | None,
) -> str:
    """Describe reads, partition filters, and the shape of the statement.

    Args:
        config: Config estimate. Used for the execution and date counts.
        dates: Number of distinct observed schedule dates.
        sources: Certain ``dataset.table`` reads, already sorted.
        partitions: Catalog partition columns, keyed by ``dataset.table``.
        coverage: Partition-filter result, or ``None`` when it was not run.
        lookback_days: Outer lower bound in days, or ``None``.
        lookback_uncertain: Whether that bound is not a simple outer predicate.
        intervals: Distinct ``DAY`` counts found on date subtractions.
        tree: Parsed statement, or ``None`` when the SQL did not parse.

    Returns:
        One paragraph. An unparsed statement says the pattern was not classified.
    """
    parts = []
    if sources:
        parts.append("Reads " + ", ".join(sources) + ".")
    else:
        parts.append("No certain read edge was recorded for this query.")
    if coverage is not None:
        for table in sorted(coverage.unfiltered):
            column = partitions.get(table, "unknown")
            parts.append(
                f"{table} is partitioned by {column} in table_catalog.csv. "
                "This statement does not filter that column."
            )
        for table in sorted(coverage.ambiguous):
            parts.append(f"The partition predicate for {table} is ambiguous.")
    if lookback_days is not None and not lookback_uncertain:
        parts.append(f"The outer WHERE lower bound is {lookback_days} days.")
    elif intervals:
        listed = ", ".join(str(day) for day in intervals)
        parts.append(
            f"INTERVAL {listed} DAY appears in the SQL. "
            "The lookback classifier does not treat the predicate as a simple "
            "outer lower bound."
        )
    if tree is not None and _projection_star(tree):
        parts.append("The statement contains a star select.")
    if tree is not None and tree.find(exp.RowNumber) is not None:
        parts.append("The statement uses ROW_NUMBER.")
    if tree is not None and tree.find(exp.Group) is not None:
        parts.append("The statement aggregates.")
    if dates and config.execution_count > dates:
        parts.append(
            "The recorded history repeats the statement more than once on an "
            "observed schedule date."
        )
    if tree is None:
        parts.append("The SQL did not parse, so the scan pattern was not classified.")
    return " ".join(parts)


def _investigation(
    config: ConfigCost,
    header: str | None,
    coverage,
    partitions: dict[str, str],
    lookback_days: int | None,
    intervals: tuple[int, ...],
    tree: exp.Expression | None,
) -> str:
    """Name the checks to run. No byte change is estimated.

    Args:
        config: Config estimate. An hourly schedule is mentioned only with
            a wide window or an unfiltered partition.
        header: First non-owner comment, or ``None``.
        coverage: Partition-filter result, or ``None``.
        partitions: Catalog partition columns.
        lookback_days: Outer lower bound in days, or ``None``.
        intervals: Distinct day counts from date subtractions.
        tree: Parsed statement, or ``None``.

    Returns:
        One paragraph. A statement with none of these signs still asks for
        a dry run.
    """
    parts = []
    wide = (lookback_days is not None and lookback_days > 1) or any(
        day > 1 for day in intervals
    )
    unfiltered = () if coverage is None else tuple(sorted(coverage.unfiltered))
    if unfiltered:
        listed = ", ".join(
            f"{table} ({partitions.get(table, 'unknown')})" for table in unfiltered
        )
        parts.append(
            "Investigate a predicate on the catalog partition column for "
            f"{listed}. Compare a BigQuery dry run with the current statement. "
            "This report does not estimate the byte change."
        )
    if config.frequency == "hourly" and (wide or unfiltered):
        parts.append(
            "Investigate whether the hourly schedule has to rebuild this "
            "window. The statement is not limited to the latest hour."
        )
    if header and "daily" in header.lower() and config.frequency == "hourly":
        parts.append(
            "Confirm whether the hourly schedule is still required. The SQL "
            "comment describes daily work."
        )
    if tree is not None and tree.find(exp.RowNumber) is not None:
        parts.append(
            "Investigate whether deduplication has to run on this schedule "
            "over the whole filtered window."
        )
    if not parts:
        parts.append(
            "Investigate the scan with a BigQuery dry run before changing the "
            "SQL or the schedule. No byte reduction is estimated here."
        )
    return " ".join(parts)


def _risks(
    config: ConfigCost,
    query: ScheduledQuery | None,
    inventory: Inventory,
    graph: Graph,
    tree: exp.Expression | None,
    intervals: tuple[int, ...],
) -> str:
    """State how a schedule or filter change can affect published rows.

    Args:
        config: Config estimate. Its destination is checked for readers.
        query: Paired query. ``WRITE_TRUNCATE`` and ``WRITE_APPEND`` are named.
        inventory: Usage rows.
        graph: Read edges. An uncertain read still counts as a reader.
        tree: Parsed statement, or ``None``.
        intervals: Day counts. A ``ROW_NUMBER`` warning is added only when
            this is not empty.

    Returns:
        One paragraph. A destination with no recorded reader still says the
        extract may have missed one.
    """
    parts = []
    disposition = None if query is None else query.write_disposition
    if disposition == "WRITE_TRUNCATE":
        parts.append(
            "WRITE_TRUNCATE replaces the destination on each successful run. "
            "Narrowing the window or the schedule changes which rows the "
            "table contains."
        )
    elif disposition == "WRITE_APPEND":
        parts.append(
            "WRITE_APPEND adds rows on each run. Changing the schedule can "
            "duplicate or skip appended grains. Confirm the destination grain "
            "before changing it."
        )
    if tree is not None and tree.find(exp.RowNumber) is not None and intervals:
        parts.append(
            "ROW_NUMBER runs inside the filtered window. A row whose timestamp "
            "falls outside that window does not compete. Confirm that this "
            "matches the correction rule before narrowing the filter."
        )
    if config.destination and _has_reader(inventory, graph, config.destination):
        parts.append(
            "The destination has a recorded reader in the graph or in "
            "downstream_usage.csv. A freshness change can affect that reader."
        )
    elif config.destination:
        parts.append(
            "The destination has no recorded reader in the graph or in "
            "downstream_usage.csv. The extract can still have missed one."
        )
    if not parts:
        parts.append(
            "Changing the schedule or the filter can change the published "
            "rows. Confirm the consumers before editing the query."
        )
    return " ".join(parts)


def _verification(coverage) -> str:
    """Return the BigQuery check for this driver.

    Args:
        coverage: Partition-filter result, or ``None``. An unfiltered table
            adds a sentence about comparing a partition predicate.

    Returns:
        The verification paragraph. It does not assume a byte reduction.
    """
    base = (
        "In BigQuery, dry-run the current SQL and any candidate predicate or "
        "schedule. Compare bytes processed and the destination grain. Do not "
        "change the schedule from this report alone."
    )
    if coverage is not None and coverage.unfiltered:
        return (
            base + " Include a predicate on the catalog partition column in that "
            "comparison. No reduction is assumed here."
        )
    return base


def _partition_columns(inventory: Inventory) -> dict[str, str]:
    """Map ``dataset.table`` to a non-blank catalog partition column.

    Args:
        inventory: Catalog rows. An unparsed table cell is skipped. A later
            row with the same key replaces the earlier column.

    Returns:
        The mapping. A blank partition column is omitted.
    """
    found = {}
    for row in inventory.catalog:
        if row.table is None:
            continue
        column = (row.raw.get("partition_column") or "").strip()
        if column:
            found[row.table.dataset_table] = column
    return found


def _sources(graph: Graph, config_id: str) -> tuple[str, ...]:
    """Return the certain tables this query reads.

    Args:
        graph: Dependency graph. Uncertain reads are omitted.
        config_id: Scheduled-query id.

    Returns:
        Sorted ``dataset.table`` values. Empty when the query has no certain read.
    """
    query_node = f"query:{config_id}"
    tables = {
        dataset_table(edge.source)
        for edge in graph.edges
        if edge.relation == "reads" and not edge.uncertain and edge.target == query_node
    }
    return tuple(sorted(tables))


def _has_reader(inventory: Inventory, graph: Graph, destination: str) -> bool:
    """Return whether the destination has a recorded reader.

    Args:
        inventory: Usage rows. Any consumer of ``destination`` counts.
        graph: Read edges. An uncertain read of this table counts.
        destination: ``dataset.table``.

    Returns:
        ``True`` when usage or a read edge names the table.
    """
    if consumers(inventory, destination):
        return True
    table_node = f"table:{destination}"
    return any(
        edge.relation == "reads" and edge.source == table_node for edge in graph.edges
    )


def _parse(sql: str) -> exp.Expression | None:
    """Parse one BigQuery statement.

    Args:
        sql: Scheduled-query text.

    Returns:
        The parsed tree, or ``None`` when the text is blank or sqlglot rejects it.
    """
    if not sql.strip():
        return None
    try:
        return sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return None


def _day_intervals(tree: exp.Expression) -> tuple[int, ...]:
    """Collect distinct whole-day counts from date subtractions.

    Args:
        tree: Parsed statement. Date additions are ignored. A non-digit
            interval, including a leading minus, is ignored.

    Returns:
        The day counts, sorted.
    """
    days: set[int] = set()
    nodes = tree.find_all(exp.DateSub, exp.TimestampSub, exp.DatetimeSub)
    for node in nodes:
        unit = node.args.get("unit")
        if unit is None or str(unit).upper() != "DAY":
            continue
        literal = node.args.get("expression")
        if isinstance(literal, exp.Literal) and str(literal.this).isdigit():
            days.add(int(literal.this))
    return tuple(sorted(days))


def _projection_star(tree: exp.Expression) -> bool:
    """Return whether any select list contains a star.

    Args:
        tree: Parsed statement. Nested selects are included. An alias around
            a star still counts.

    Returns:
        ``True`` when a star is present.
    """
    for select in tree.find_all(exp.Select):
        for expression in select.expressions:
            node = expression.this if isinstance(expression, exp.Alias) else expression
            if isinstance(node, exp.Star):
                return True
            if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
                return True
    return False


def _owner(comments: tuple[str, ...]) -> str | None:
    """Return the text of the first ``owner:`` comment.

    Args:
        comments: Full-line comment texts, without the leading dashes.

    Returns:
        The owner text, or ``None`` when no comment matches.
    """
    for line in comments:
        match = _OWNER.match(line)
        if match is not None:
            return match.group(1)
    return None


def _comments(sql: str) -> tuple[str, ...]:
    """Return non-empty full-line ``--`` comments, in source order.

    Args:
        sql: Scheduled-query text. A comment that follows code on the same
            line is not included. Block comments are not read.

    Returns:
        Comment text with the leading dashes and surrounding space removed.
    """
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            text = stripped[2:].strip()
            if text:
                lines.append(text)
    return tuple(lines)

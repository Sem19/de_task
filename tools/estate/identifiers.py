"""Normalize BigQuery table identifiers and scheduled-query names.

Fully qualified names keep their project. Joins use ``dataset.table``, so a
project-qualified reference still matches a dataset-qualified one. Identifiers
are not lowercased and are not rewritten to a canonical project.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_PROJECT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SQL_FILENAME = re.compile(
    r"(?P<config_id>.+)__"
    r"(?P<dataset>[A-Za-z_][A-Za-z0-9_]*)__"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]*)\.sql\Z"
)


class TableId(NamedTuple):
    """A parsed table reference. ``project`` is set only when the source had one."""

    project: str | None
    dataset: str
    table: str

    @property
    def dataset_table(self) -> str:
        return f"{self.dataset}.{self.table}"

    @property
    def fully_qualified(self) -> str | None:
        if self.project is None:
            return None
        return f"{self.project}.{self.dataset}.{self.table}"


class SqlFilename(NamedTuple):
    """Pieces of ``<config_id>__<dataset>__<table>.sql``."""

    config_id: str
    dataset: str
    table: str


def parse_table_id(value: str) -> TableId:
    """Parse a table id without dropping a project when the source has one.

    Accepted forms are ``dataset.table``, ``project.dataset.table``,
    ``project:dataset.table``, and any of those wrapped in one pair of
    backticks. ``dataset_table`` is the join key. The project stays on the
    result so provenance is not lost, and it is not required for two ids to
    refer to the same table.

    Args:
        value: Identifier text from a config, CSV cell, schema key, or filename.

    Returns:
        Parsed project, dataset, and table. Project is None when absent.

    Raises:
        ValueError: The text is empty or not a two- or three-part identifier.
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1].strip()
    if not text:
        raise ValueError(f"empty table identifier: {value!r}")

    project: str | None
    remainder: str
    if ":" in text:
        project, _, remainder = text.partition(":")
        project = project.strip()
        remainder = remainder.strip()
        if not _PROJECT.fullmatch(project) or ":" in remainder:
            raise ValueError(f"invalid table identifier: {value!r}")
    else:
        project = None
        remainder = text

    parts = remainder.split(".")
    if project is None and len(parts) == 3:
        project, dataset, table = parts
    elif len(parts) == 2:
        dataset, table = parts
    else:
        raise ValueError(f"invalid table identifier: {value!r}")

    dataset = dataset.strip()
    table = table.strip()
    if project is not None and not _PROJECT.fullmatch(project):
        raise ValueError(f"invalid project in table identifier: {value!r}")
    if not _IDENT.fullmatch(dataset) or not _IDENT.fullmatch(table):
        raise ValueError(f"invalid table identifier: {value!r}")
    return TableId(project, dataset, table)


def try_parse_table_id(value: str) -> TableId | None:
    """Parse a table id, or return None when the cell is not one.

    Unparseable ids are source findings. They must not abort the inventory.
    """
    try:
        return parse_table_id(value)
    except ValueError:
        return None


def parse_sql_filename(filename: str) -> SqlFilename:
    """Derive config_id, dataset, and table from a scheduled-query filename.

    Args:
        filename: Base name, not a path.

    Returns:
        The three filename parts. The config_id may itself contain underscores;
        the separators are double underscores.

    Raises:
        ValueError: The name does not match ``<config_id>__<dataset>__<table>.sql``.
    """
    match = _SQL_FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError(
            f"SQL filename must be <config_id>__<dataset>__<table>.sql, got {filename!r}"
        )
    return SqlFilename(
        match.group("config_id"),
        match.group("dataset"),
        match.group("table"),
    )


def config_id_from_transfer_name(name: str) -> str:
    """Derive config_id from a transfer-config resource name.

    ``projects/<p>/locations/<l>/transferConfigs/<config_id>`` yields the last
    path segment. A bare id is accepted unchanged.

    Args:
        name: Transfer config ``name`` field.

    Returns:
        The config_id segment.

    Raises:
        ValueError: The name has no final path segment.
    """
    segment = name.strip().rstrip("/").rsplit("/", 1)[-1]
    if not segment:
        raise ValueError(f"transfer config name has no config_id: {name!r}")
    return segment

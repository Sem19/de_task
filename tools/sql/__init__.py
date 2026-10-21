"""Parse BigQuery scheduled-query SQL into table references.

This package does not build the dependency graph. Callers relate reads to
transfer-config destinations themselves.
"""

from sql.models import ParseIssue, SqlParse, TableRef, UnresolvedRef
from sql.parse import format_issue, parse_inventory, parse_sql

__all__ = [
    "ParseIssue",
    "SqlParse",
    "TableRef",
    "UnresolvedRef",
    "format_issue",
    "parse_inventory",
    "parse_sql",
]

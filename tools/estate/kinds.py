"""Names for source-data findings.

A finding records an inconsistency in an otherwise readable estate. It is not
an execution failure. Missing files and malformed JSON or CSV raise
EstateInputError instead, so analysis does not continue on a partial read.
"""

from __future__ import annotations

UNPARSED_SQL_FILENAME = "unparsed_sql_filename"
INCOMPLETE_CONFIG = "incomplete_config"
MISSING_CONFIG_ID = "missing_config_id"
DUPLICATE_CONFIG_ID = "duplicate_config_id"
MISSING_SQL = "missing_sql"
MISSING_CONFIG = "missing_config"
SQL_TEXT_MISMATCH = "sql_text_mismatch"
DESTINATION_MISMATCH = "destination_mismatch"
META_SQL_FILE_MISMATCH = "meta_sql_file_mismatch"
ENABLED_STATE_MISMATCH = "enabled_state_mismatch"
SHARED_DESTINATION = "shared_destination"
UNPARSED_TABLE_ID = "unparsed_table_id"
HISTORY_TABLE_NOT_IN_CATALOG = "history_table_not_in_catalog"
HISTORY_UNKNOWN_CONFIG = "history_unknown_config"
HISTORY_DESTINATION_MISMATCH = "history_destination_mismatch"
USAGE_UNKNOWN_CONFIG = "usage_unknown_config"
USAGE_TABLE_NOT_IN_CATALOG = "usage_table_not_in_catalog"
SCHEMA_NOT_IN_CATALOG = "schema_not_in_catalog"


def count_label(count: int, noun: str) -> str:
    """Format a count for a finding message.

    Args:
        count: Number of rows or files.
        noun: Singular noun. A count other than 1 appends ``s``.

    Returns:
        ``"1 row"`` or ``"2 rows"``.
    """
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

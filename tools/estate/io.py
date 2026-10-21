"""Read estate files. Structural failures raise; they are not findings.

Each path is read once. SQL newlines are preserved so a later exact compare
against params.query is not disturbed by universal-newline translation.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from tools.estate.errors import EstateInputError


def read_text(path: Path) -> str:
    """Read UTF-8 text without rewriting newlines.

    Args:
        path: File to read.

    Returns:
        The file text, including its original newline characters.

    Raises:
        EstateInputError: The file is missing or not valid UTF-8.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError as exc:
        raise EstateInputError(f"missing file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise EstateInputError(f"not valid UTF-8: {path}") from exc


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON object.

    Args:
        path: JSON file.

    Returns:
        The parsed object.

    Raises:
        EstateInputError: The file is missing, not valid JSON, or not an object.
    """
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise EstateInputError(
            f"malformed JSON in {path}: {exc.msg} at line {exc.lineno}"
        ) from exc
    if not isinstance(payload, dict):
        raise EstateInputError(f"{path}: expected a JSON object")
    return payload


def load_csv(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    """Load a CSV file and require a header plus one value per required column.

    Args:
        path: CSV file.
        required: Column names that must be present and non-empty on every row.

    Returns:
        Rows in file order. Cell values are the original strings.

    Raises:
        EstateInputError: The file is missing, has no header, omits a required
            column, or has a short or long row.
    """
    text = read_text(path)
    if not text.strip():
        raise EstateInputError(f"malformed CSV in {path}: missing header")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise EstateInputError(f"malformed CSV in {path}: missing header")
    missing = [column for column in required if column not in fieldnames]
    if missing:
        raise EstateInputError(
            f"malformed CSV in {path}: missing columns {', '.join(missing)}"
        )
    rows: list[dict[str, str]] = []
    for line_number, row in enumerate(reader, start=2):
        if row.get(None):
            raise EstateInputError(
                f"malformed CSV in {path}: extra fields on line {line_number}"
            )
        if any(row.get(column) is None for column in required):
            raise EstateInputError(
                f"malformed CSV in {path}: missing values on line {line_number}"
            )
        rows.append(
            {column: row[column] for column in fieldnames if column is not None}
        )
    return rows

"""
Partner report extractor — SKELETON.

What exists here: the container, the connection settings, and a place to put
things. Everything that matters is a TODO.

Run it with:  docker compose run --rm extractor
"""

from __future__ import annotations

import os

API_BASE = os.environ.get("API_BASE", "http://mock-api:8080")
CLIENT_ID = os.environ["API_CLIENT_ID"]
CLIENT_SECRET = os.environ["API_CLIENT_SECRET"]
PG_DSN = os.environ.get("PG_DSN", "postgresql://acme:acme@warehouse:5432/acme")

DIMENSIONS = ["date", "country", "channel"]
METRICS = ["impressions", "clicks", "installs", "signups", "payers", "spend", "revenue"]


def get_token() -> str:
    raise NotImplementedError


def submit_report(token: str, date_from: str, date_to: str) -> str:
    raise NotImplementedError


def wait_for_report(token: str, report_id: str) -> dict:
    raise NotImplementedError


def fetch_rows(token: str, report_id: str):
    """Yield every row of a completed report."""
    raise NotImplementedError


def load(rows, meta: dict) -> None:
    """Land rows in `partner_report`. Must be safe to call twice."""
    raise NotImplementedError


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()

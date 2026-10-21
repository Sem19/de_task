"""Unit tests for report row pagination and completeness checks."""

from __future__ import annotations

import unittest

from api_client import ReportNotFoundError, ReportPaginationError, ReportRows
from config import REPORT_ORDER_BY, REPORT_PAGE_LIMIT

from tests.helpers import ROW_META, MockAPI, make_client, page, row


class ReportRowsTest(unittest.TestCase):
    """Paged downloads and completeness checks."""

    def test_fetch_rows_multiple_pages(self) -> None:
        api = MockAPI()
        first = [row(0), row(1)]
        second = [row(2)]
        api.row_pages = [
            page(first, total=3, has_more=True),
            page(second, total=3, has_more=False),
        ]
        with make_client(api) as client:
            result = client.fetch_rows("rep_1")
        self.assertEqual(
            result,
            ReportRows(
                rows=first + second,
                total=3,
                currency="EUR",
                timezone="America/Los_Angeles",
                data_version=42,
                restated_through="2026-10-19",
                truncated=False,
            ),
        )
        self.assertEqual(api.row_calls, 2)
        self.assertEqual(
            api.row_params[0],
            {
                "offset": "0",
                "limit": str(REPORT_PAGE_LIMIT),
                "order_by": REPORT_ORDER_BY,
            },
        )
        self.assertEqual(api.row_params[1]["offset"], "2")

    def test_fetch_rows_uses_page_limit_1000(self) -> None:
        api = MockAPI()
        first = [row(i) for i in range(REPORT_PAGE_LIMIT)]
        second = [row(REPORT_PAGE_LIMIT)]
        total = REPORT_PAGE_LIMIT + 1
        api.row_pages = [
            page(first, total=total, has_more=True),
            page(second, total=total, has_more=False),
        ]
        with make_client(api) as client:
            result = client.fetch_rows("rep_1")
        self.assertEqual(len(result.rows), total)
        self.assertEqual(api.row_params[0]["limit"], "1000")
        self.assertEqual(api.row_params[1]["offset"], "1000")
        self.assertEqual(api.row_params[1]["limit"], "1000")

    def test_fetch_rows_empty_report(self) -> None:
        api = MockAPI()
        api.row_pages = [page([], total=0, has_more=False)]
        with make_client(api) as client:
            result = client.fetch_rows("rep_1")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.total, 0)
        self.assertEqual(api.row_calls, 1)

    def test_fetch_rows_keeps_duplicate_rows(self) -> None:
        api = MockAPI()
        duplicate = row(1)
        api.row_pages = [page([duplicate, duplicate], total=2, has_more=False)]
        with make_client(api) as client:
            result = client.fetch_rows("rep_1")
        self.assertEqual(result.rows, [duplicate, duplicate])

    def test_fetch_rows_rejects_empty_page_with_has_more(self) -> None:
        api = MockAPI()
        api.row_pages = [page([], total=10, has_more=True)]
        with make_client(api) as client:
            with self.assertRaises(ReportPaginationError) as ctx:
                client.fetch_rows("rep_1")
        self.assertEqual(ctx.exception.error, "empty_page")

    def test_fetch_rows_rejects_inconsistent_totals(self) -> None:
        api = MockAPI()
        api.row_pages = [
            page([row(0)], total=3, has_more=True),
            page([row(1)], total=2, has_more=False),
        ]
        with make_client(api) as client:
            with self.assertRaises(ReportPaginationError) as ctx:
                client.fetch_rows("rep_1")
        self.assertEqual(ctx.exception.error, "inconsistent_total")

    def test_fetch_rows_rejects_count_mismatch(self) -> None:
        api = MockAPI()
        api.row_pages = [page([row(0), row(1)], total=5, has_more=False)]
        with make_client(api) as client:
            with self.assertRaises(ReportPaginationError) as ctx:
                client.fetch_rows("rep_1")
        self.assertEqual(ctx.exception.error, "inconsistent_total")

    def test_fetch_rows_rejects_truncation(self) -> None:
        api = MockAPI()
        meta = dict(ROW_META, truncated=True)
        api.row_pages = [page([row(0)], total=1, has_more=False, meta=meta)]
        with make_client(api) as client:
            with self.assertRaises(ReportPaginationError) as ctx:
                client.fetch_rows("rep_1")
        self.assertEqual(ctx.exception.error, "truncated")

    def test_fetch_rows_missing_report(self) -> None:
        api = MockAPI()
        api.missing_ids.add("rep_gone")
        with make_client(api) as client:
            with self.assertRaises(ReportNotFoundError):
                client.fetch_rows("rep_gone")
        self.assertEqual(api.submit_calls, 0)

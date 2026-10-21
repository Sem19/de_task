"""Unit tests for report submission and asynchronous polling."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api_client import (
    PartnerAPIError,
    Report,
    ReportNotFoundError,
    ReportStatusError,
    ReportTimeoutError,
)
from config import REPORT_DIMENSIONS, REPORT_METRICS, REPORT_POLL_TIMEOUT_S

from tests.helpers import FakeClock, MockAPI, make_client


class ReportClientTest(unittest.TestCase):
    def test_submit_report_payload(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
            report = client.submit_report("2026-10-01", "2026-10-07")
        self.assertEqual(
            report,
            Report(report_id="rep_1", status="PENDING", row_count=None),
        )
        self.assertEqual(api.submit_calls, 1)
        self.assertEqual(
            api.last_submit_body,
            {
                "date_from": "2026-10-01",
                "date_to": "2026-10-07",
                "dimensions": list(REPORT_DIMENSIONS),
                "metrics": list(REPORT_METRICS),
            },
        )
        self.assertEqual(api.last_authorization, "Bearer tok_1")

    def test_submit_rejects_window_wider_than_seven_days(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.submit_report("2026-10-01", "2026-10-08")
        self.assertEqual(ctx.exception.error, "range_too_wide")
        self.assertEqual(api.submit_calls, 0)
        self.assertEqual(api.token_calls, 0)

    def test_submit_rejects_inverted_range(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.submit_report("2026-10-07", "2026-10-01")
        self.assertEqual(ctx.exception.error, "bad_range")
        self.assertEqual(api.submit_calls, 0)

    def test_wait_for_report_polls_until_done(self) -> None:
        api = MockAPI()
        api.status_queue = [
            {"report_id": "rep_1", "status": "PENDING"},
            {"report_id": "rep_1", "status": "RUNNING"},
            {"report_id": "rep_1", "status": "DONE", "row_count": 12},
        ]
        with patch("api_client.reports.time.sleep") as sleep:
            with make_client(api) as client:
                submitted = client.submit_report("2026-10-01", "2026-10-03")
                done = client.wait_for_report(submitted.report_id)
        self.assertEqual(submitted.report_id, "rep_1")
        self.assertEqual(submitted.status, "PENDING")
        self.assertEqual(
            done,
            Report(report_id="rep_1", status="DONE", row_count=12),
        )
        self.assertEqual(api.status_calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_wait_for_report_terminal_failure(self) -> None:
        api = MockAPI()
        api.status_queue = [{"report_id": "rep_1", "status": "FAILED"}]
        with patch("api_client.reports.time.sleep") as sleep:
            with make_client(api) as client:
                with self.assertRaises(ReportStatusError) as ctx:
                    client.wait_for_report("rep_1")
        self.assertEqual(ctx.exception.status, "FAILED")
        self.assertEqual(ctx.exception.error, "report_failed")
        self.assertEqual(api.submit_calls, 0)
        sleep.assert_not_called()

    def test_wait_for_report_unknown_status(self) -> None:
        api = MockAPI()
        api.status_queue = [{"report_id": "rep_1", "status": "WEIRD"}]
        with make_client(api) as client:
            with self.assertRaises(ReportStatusError) as ctx:
                client.wait_for_report("rep_1")
        self.assertEqual(ctx.exception.error, "unknown_status")

    def test_wait_for_report_timeout(self) -> None:
        api = MockAPI()
        api.status_default = {"report_id": "rep_1", "status": "RUNNING"}
        clock = FakeClock()
        with patch("api_client.reports.time.monotonic", clock.monotonic):
            with patch("api_client.reports.time.sleep", clock.sleep):
                with make_client(api) as client:
                    with self.assertRaises(ReportTimeoutError) as ctx:
                        client.wait_for_report("rep_1")
        self.assertEqual(ctx.exception.report_id, "rep_1")
        self.assertEqual(ctx.exception.last_status, "RUNNING")
        self.assertEqual(ctx.exception.error, "report_timeout")
        self.assertGreaterEqual(clock.now, REPORT_POLL_TIMEOUT_S)
        self.assertEqual(api.submit_calls, 0)

    def test_missing_report_id_does_not_resubmit(self) -> None:
        api = MockAPI()
        api.missing_ids.add("rep_gone")
        with make_client(api) as client:
            with self.assertRaises(ReportNotFoundError) as ctx:
                client.wait_for_report("rep_gone")
        self.assertEqual(ctx.exception.report_id, "rep_gone")
        self.assertEqual(ctx.exception.error, "unknown_report")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(api.submit_calls, 0)
        self.assertEqual(api.status_calls, 1)

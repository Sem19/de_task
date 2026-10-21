"""Exit-code tests for the extractor entry point."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from config import ConfigError
from main import exit_code, main
from pipeline import ExtractResult


class ExitCodeTest(unittest.TestCase):
    """Process exit codes for success, skip, and failure."""

    def test_success_is_zero(self) -> None:
        self.assertEqual(exit_code(ExtractResult(status="success", run_id="r1")), 0)

    def test_skipped_is_zero(self) -> None:
        self.assertEqual(exit_code(ExtractResult(status="skipped", run_id="r1")), 0)

    def test_failed_is_nonzero(self) -> None:
        self.assertEqual(
            exit_code(ExtractResult(status="failed", run_id="r1", error="x")), 1
        )


class MainTest(unittest.TestCase):
    """``main`` maps configuration and extraction outcomes to exit codes."""

    @patch("main.run_extraction")
    @patch("main.Settings.from_env")
    def test_successful_run_exits_zero(self, from_env, run) -> None:
        from_env.return_value = object()
        run.return_value = ExtractResult(
            status="success", run_id="abc", chunks_planned=1, chunks_committed=1
        )
        self.assertEqual(main(), 0)

    @patch("main.run_extraction")
    @patch("main.Settings.from_env")
    def test_skipped_run_exits_zero(self, from_env, run) -> None:
        from_env.return_value = object()
        run.return_value = ExtractResult(status="skipped", run_id="abc")
        self.assertEqual(main(), 0)

    @patch("main.run_extraction")
    @patch("main.Settings.from_env")
    def test_failed_run_exits_nonzero(self, from_env, run) -> None:
        from_env.return_value = object()
        run.return_value = ExtractResult(status="failed", run_id="abc", error="boom")
        self.assertEqual(main(), 1)

    @patch("main.Settings.from_env", side_effect=ConfigError("missing API_CLIENT_ID"))
    def test_config_error_exits_nonzero(self, _from_env) -> None:
        self.assertEqual(main(), 1)

    @patch("main.run_extraction", side_effect=RuntimeError("crash"))
    @patch("main.Settings.from_env")
    def test_unexpected_crash_exits_nonzero(self, from_env, _run) -> None:
        from_env.return_value = object()
        self.assertEqual(main(), 1)

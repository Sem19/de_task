"""Contract tests for opt-in 15-minute scheduling (no Docker required)."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

EXTRACTOR_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EXTRACTOR_DIR.parents[1]
CRONTAB = EXTRACTOR_DIR / "crontab"
SCHEDULE_RUN = EXTRACTOR_DIR / "schedule-run.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"
MAKEFILE = REPO_ROOT / "Makefile"


class CrontabContractTest(unittest.TestCase):
    """Supercronic crontab and the schedule-run script, without Docker."""

    def test_schedule_is_every_fifteen_minutes(self) -> None:
        text = CRONTAB.read_text(encoding="utf-8")
        jobs = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(jobs, ["*/15 * * * * /app/schedule-run.sh"])
        self.assertNotIn("s3cr3t", text)
        self.assertNotIn("acme-de-takehome", text)

    def test_schedule_run_uses_one_shot_entrypoint(self) -> None:
        text = SCHEDULE_RUN.read_text(encoding="utf-8")
        self.assertIn("python main.py", text)
        self.assertNotIn("while True", text)
        self.assertNotIn("EXTRACT_START_DATE", text)
        self.assertTrue(SCHEDULE_RUN.stat().st_mode & stat.S_IXUSR)

    def test_failed_extract_exit_code_is_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "python"
            stub.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            stub.chmod(0o755)
            script = root / "schedule-run.sh"
            script.write_text(
                SCHEDULE_RUN.read_text(encoding="utf-8"), encoding="utf-8"
            )
            script.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            completed = subprocess.run(
                [str(script)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 7)
        self.assertIn("exited 7", completed.stderr)

    def test_dockerfile_does_not_pin_supercronic_arch(self) -> None:
        dockerfile = (EXTRACTOR_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("supercronic-linux-${TARGETARCH}", dockerfile)
        self.assertNotIn("TARGETARCH=amd64", dockerfile)

    def test_compose_scheduler_is_opt_in_profile(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("extractor-scheduler:", text)
        self.assertIn("profiles:", text)
        self.assertIn("schedule", text)
        self.assertIn("/usr/local/bin/supercronic", text)
        self.assertIn("/app/crontab", text)
        scheduler_block = text.split("extractor-scheduler:")[1]
        self.assertNotIn("EXTRACT_START_DATE", scheduler_block)
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("docker compose run --rm extractor", makefile)
        self.assertIn("--profile schedule", makefile)

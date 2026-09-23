"""The public bootstrap never treats unavailable private delivery as success."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_private_daily import main


class BootstrapTests(unittest.TestCase):
    def test_missing_writer_fails_without_using_read_token_for_writes(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "geniusgrok/uquant-cli",
                                     "UQUANT_READ_TOKEN": "CANARY_NOT_A_REAL_SECRET"}, clear=True):
            with patch("subprocess.run") as command, contextlib.redirect_stdout(output):
                self.assertEqual(main(), 78)
                command.assert_not_called()
        self.assertIn("PRIVATE_DELIVERY_UNCONFIGURED", output.getvalue())
        self.assertNotIn("CANARY_NOT_A_REAL_SECRET", output.getvalue())

    def test_unapproved_repository_never_starts(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "unapproved/runner"}, clear=True):
            with patch("subprocess.run") as command, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                command.assert_not_called()

    def test_daily_workflow_has_one_schedule_and_no_public_payload_upload(self):
        workflow = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(workflow.count("cron:"), 1)
        self.assertIn("1 9 * * 1-5", workflow)
        self.assertIn("python scripts/run_private_daily.py", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertNotIn("Report delivery gate", workflow)


if __name__ == "__main__":
    unittest.main()

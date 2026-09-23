"""The public report path uses this repository's token without a private writer."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_private_daily import main, state_key


class BootstrapTests(unittest.TestCase):
    def test_missing_job_token_fails_before_checkout(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "geniusgrok/uquant-cli",
                                     "UQUANT_READ_TOKEN": "CANARY_NOT_A_REAL_SECRET"}, clear=True):
            with patch("subprocess.run") as command, contextlib.redirect_stdout(output):
                self.assertEqual(main(), 78)
                command.assert_not_called()
        self.assertIn("REQUIRED_CREDENTIAL_UNAVAILABLE", output.getvalue())
        self.assertNotIn("CANARY_NOT_A_REAL_SECRET", output.getvalue())

    def test_unapproved_repository_never_starts(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "unapproved/runner"}, clear=True):
            with patch("subprocess.run") as command, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                command.assert_not_called()

    def test_daily_workflow_uses_own_token_and_one_schedule(self):
        workflow = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(workflow.count("cron:"), 1)
        self.assertIn("1 9 * * 1-5", workflow)
        self.assertIn("python scripts/run_private_daily.py", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("UQUANT_REPORT_WRITE_TOKEN", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)

    def test_state_key_is_stable_and_not_the_read_credential(self):
        token = "CANARY_NOT_A_REAL_SECRET"
        key = state_key(token)
        self.assertEqual(len(key), 64)
        self.assertEqual(key, state_key(token))
        self.assertNotEqual(key, state_key(token + "rotated"))
        self.assertNotIn(token, key)


if __name__ == "__main__":
    unittest.main()

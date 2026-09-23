import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import main


class BootstrapTests(unittest.TestCase):
    def test_unapproved_runner_does_not_execute(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "other/repo"}, clear=True):
            with patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                run.assert_not_called()

    def test_missing_checkouts_do_not_start(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "geniusgrok/uquant-cli"}, clear=True):
            with patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 78)
                run.assert_not_called()

    def test_credentials_are_managed_only_by_official_checkout(self):
        launcher = Path("runner.py").read_text()
        store = Path("uquant_cli/store.py").read_text()
        for text in (launcher, store):
            self.assertNotIn("UQUANT_READ_TOKEN", text)
            self.assertNotIn("PASSPHRASE", text)
            self.assertNotIn("import base64", text)
        text = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(text.count("token: ${{ secrets.UQUANT_READ_TOKEN }}"), 1)
        self.assertIn("ref: uquant-daily-reports", text)
        self.assertIn("disabled://source-read-only", launcher)

    def test_workflow_writes_only_to_cli_and_has_one_schedule(self):
        text = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(text.count("cron:"), 1)
        self.assertIn("1 9 * * 1-5", text)
        self.assertIn("contents: write", text)
        self.assertNotIn("UQUANT_REPORT_WRITE_TOKEN", text)
        self.assertNotIn("upload-artifact", text)


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import main
from bootstrap import SOURCE_REPOSITORY, prepare
from uquant_cli.store import GitStore


class BootstrapTests(unittest.TestCase):
    def test_all_production_checkouts_use_transferred_repository(self):
        self.assertEqual(SOURCE_REPOSITORY, "https://github.com/geniusgrok/uquant.git")
        for name in ("uquant-daily-report.yml", "delivery-validation.yml", "uquant-smoke.yml"):
            text = (Path(".github/workflows") / name).read_text()
            with self.subTest(workflow=name):
                self.assertNotIn("ychenracing/uquant", text)
                self.assertEqual(text.count("repository: geniusgrok/uquant\n"), 1)
                checkout = text.split("repository: geniusgrok/uquant\n", 1)[1].split("\n      - ", 1)[0]
                self.assertIn("ref: main", checkout)
                self.assertIn("token: ${{ secrets.UQUANT_READ_TOKEN }}", checkout)
                self.assertIn("persist-credentials: false", checkout)

    def test_transferred_source_is_not_a_report_destination(self):
        with self.assertRaisesRegex(ValueError, "unapproved write destination"):
            GitStore(Path("unused"), SOURCE_REPOSITORY)

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
        bootstrap = Path("bootstrap.py").read_text()
        store = Path("uquant_cli/store.py").read_text()
        for text in (launcher, bootstrap, store):
            self.assertNotIn("UQUANT_READ_TOKEN", text)
            self.assertNotIn("PASSPHRASE", text)
            self.assertNotIn("import base64", text)
        text = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(text.count("token: ${{ secrets.UQUANT_READ_TOKEN }}"), 1)
        self.assertIn("ref: uquant-daily-reports", text)
        self.assertIn("disabled://source-read-only", bootstrap)

    def test_adapter_install_requires_hashes_and_current_production_constraints(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "uquant").mkdir(parents=True)
            with patch("bootstrap.CLI_ROOT", root), patch.dict(os.environ, {"UQUANT_SOURCE_DIR": str(source)}), \
                    patch("bootstrap.subprocess.run"), \
                    patch("bootstrap.subprocess.check_output", return_value="a" * 40), \
                    patch("bootstrap.call", return_value=0) as call:
                prepare(root / "operation", [])
            commands = [item.args[0] for item in call.call_args_list]
            self.assertIn(["uv", "sync", "--frozen", "--extra", "data"], commands)
            exported = next(command for command in commands if command[:2] == ["uv", "export"])
            self.assertIn("--frozen", exported)
            self.assertIn("--all-extras", exported)
            adapter = commands[-1]
            self.assertIn("--require-hashes", adapter)
            self.assertEqual(adapter[adapter.index("--build-constraints") + 1], str(root / "requirements-market.lock"))
            self.assertEqual(adapter[adapter.index("--constraint") + 1], exported[-1])
            self.assertEqual(adapter[adapter.index("--requirements") + 1], str(root / "requirements-market.lock"))

    def test_workflow_writes_only_to_cli_and_retries_late_market_data(self):
        text = Path(".github/workflows/uquant-daily-report.yml").read_text()
        self.assertEqual(text.count("cron:"), 3)
        for schedule in ("31 8 * * 1-5", "36 9 * * 1-5", "36 10 * * 1-5"):
            self.assertIn("cron: '" + schedule + "'", text)
        self.assertIn("contents: write", text)
        self.assertNotIn("UQUANT_REPORT_WRITE_TOKEN", text)
        artifact = text.split("name: Retain approved originals when a run fails", 1)[1]
        self.assertIn("if: failure()", artifact)
        self.assertIn("operation/publishable/inputs/audit.json", artifact)
        self.assertIn("operation/publishable/inputs/sh*.csv", artifact)
        self.assertIn("operation/publishable/inputs/sz*.csv", artifact)
        for forbidden in (".runtime/production", "uv-cache", "/venv/", "/journal/"):
            self.assertNotIn(forbidden, artifact)


if __name__ == "__main__":
    unittest.main()

"""Verify the CLI against read-only current production main, publishing safe metadata only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.run_private_daily import CLI_ROOT, call, prepare


def main() -> int:
    root = Path(os.environ["RUNNER_TEMP"]) / ("uquant-cli-validation-" + os.environ["GITHUB_RUN_ID"])
    root.mkdir(mode=0o700, exist_ok=False)
    events, status = [], 1
    output = CLI_ROOT / "validation"
    output.mkdir(exist_ok=True)
    try:
        source, env = prepare(root, events, validation=True)
        guard = ["python", "-m", "tools.cloud_guard", "--root", str(root / "journal")]
        code = call([*guard, "run", "--name", "observer-integration", "--timeout", "300", "--",
                     str(root / "venv/bin/python"), "-m", "cli_runtime.integration", "--source", str(source),
                     "--output", str(output / "integration.json")], source, env, events,
                    "PRODUCTION_INTEGRATION", timeout=330)
        status = code
        if (output / "integration.json").exists():
            print((output / "integration.json").read_text())
        # Public tests reference no private source lines; pytest output stays local.
        tests = call([str(root / "venv/bin/python"), "-m", "pytest", "tests/test_runtime.py", "tests/test_publication.py", "tests/test_calendar.py", "-q",
                      "--junitxml=" + str(root / "junit.xml")], CLI_ROOT, env, events, "RUNTIME_TESTS")
        import xml.etree.ElementTree as ET
        suites = ET.parse(root / "junit.xml").getroot()
        totals = {key: sum(int(item.get(key, 0)) for item in suites.iter("testsuite"))
                  for key in ("tests", "failures", "errors", "skipped")}
        (output / "runtime-tests.json").write_text(json.dumps(totals, indent=2) + "\n")
        print("RUNTIME_TESTS=" + json.dumps(totals))
        status = 0 if code == 0 and tests == 0 and totals["skipped"] == 0 else 1
    except Exception as exc:
        status = 1
        events.append({"stage": "VALIDATION", "error_type": type(exc).__name__})
    (output / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

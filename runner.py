"""编排一次生产观察运行，并把运行原件保存在本仓库。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from bootstrap import CLI_ROOT, call, prepare
from preflight import validate_environment


def main() -> int:
    failure = validate_environment(os.environ)
    if failure:
        status, code = failure
        print("UQUANT_STATUS=" + status)
        return code
    root = Path(os.environ["RUNNER_TEMP"]) / ("uquant-cli-" + os.environ["GITHUB_RUN_ID"]
                                                + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    root.mkdir(mode=0o700, exist_ok=False)
    events, status = [], 1
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CLI_ROOT)
    try:
        source, env = prepare(root, events)
        guard = ["python", "-m", "tools.cloud_guard", "--root", str(root / "journal")]
        if call([*guard, "inspect"], source, env, events, "JOURNAL_INSPECT"):
            raise RuntimeError("execution journal not clear")
        status = call([*guard, "run", "--name", "daily-scan", "--timeout", "720", "--",
            str(root / "venv/bin/python"), "-m", "uquant_cli.daily", "--root", str(root / "operation")],
            source, env, events, "DAILY_SCAN", timeout=780)
    except Exception as exc:
        events.append({"stage": "BOOTSTRAP", "error_type": type(exc).__name__})
    finally:
        (root / "events.json").write_text(json.dumps({"source_sha": env.get("UQUANT_SOURCE_SHA"),
            "runner_sha": os.environ.get("GITHUB_SHA"), "events": events}, indent=2) + "\n")
        try:
            result = call(["python", "-m", "uquant_cli.publish", "--root", str(root)],
                          CLI_ROOT, env, [], "PUBLIC_PRESERVATION")
            if result:
                status = 1
                print("UQUANT_PRESERVATION=FAILED; originals remain temporary")
        except Exception:
            status = 1
            print("UQUANT_PRESERVATION=FAILED; originals remain temporary")
    report = root / "operation/publishable/status.json"
    if report.exists():
        outcome = json.loads(report.read_text())
        allowed = {"COMPLETE", "PARTIAL", "REUSED", "MARKET_CLOSED", "MARKET_NOT_CLOSED", "FAILED"}
        if outcome.get("status") in allowed:
            print("UQUANT_STATUS=" + outcome["status"])
            print("TARGET_DATE=" + outcome["target_date"])
    else:
        print("UQUANT_STATUS=BOOTSTRAP_OR_PROCESS_FAILED")
    print("REPORT_REPOSITORY=geniusgrok/uquant-cli; SOURCE_REPOSITORY=READ_ONLY")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

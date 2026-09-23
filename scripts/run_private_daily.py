"""Read production source; run and persist exclusively in geniusgrok/uquant-cli."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CLI_ROOT = Path(__file__).resolve().parents[1]
SOURCE_REPOSITORY = "https://github.com/ychenracing/uquant.git"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def call(command: list[str], cwd: Path, env: dict[str, str], events: list,
         stage: str, timeout: int = 240) -> int:
    started = datetime.now(SHANGHAI).isoformat()
    code, error = 1, None
    try:
        # Arbitrary dependency/source output is not a public diagnostic channel.
        code = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode
        return code
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        events.append({"stage": stage, "started_at": started,
                       "finished_at": datetime.now(SHANGHAI).isoformat(),
                       "exit_code": code, "error_type": error})


def prepare(root: Path, events: list, *, validation: bool = False) -> tuple[Path, dict]:
    source = Path(os.environ["UQUANT_SOURCE_DIR"])
    if not source.resolve().is_relative_to(CLI_ROOT.resolve()) or not (source / "uquant").is_dir():
        raise RuntimeError("production source checkout missing")
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(CLI_ROOT) + os.pathsep + str(source),
                "UV_CACHE_DIR": str(root / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never",
                "PYTHONDONTWRITEBYTECODE": "1", "UV_PROJECT_ENVIRONMENT": str(root / "venv")})
    # Defense in depth: this checkout has no write token and no usable push URL.
    subprocess.run(["git", "config", "remote.origin.pushurl", "disabled://source-read-only"],
                   cwd=source, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    env["UQUANT_SOURCE_SHA"] = sha
    env["UQUANT_STARTED_AT"] = datetime.now(SHANGHAI).isoformat()
    install = ["uv", "sync", "--frozen", "--extra", "data"]
    if validation:
        install.extend(["--extra", "dev"])
    for command in (["python", "-m", "pip", "install", "--disable-pip-version-check", "uv==0.11.33"], install):
        if call(command, source, env, events, "LOCKED_INSTALL"):
            raise RuntimeError("locked installation failed")
    return source, env


def main() -> int:
    if os.environ.get("GITHUB_REPOSITORY") != "geniusgrok/uquant-cli":
        print("UQUANT_STATUS=UNAPPROVED_RUNNER")
        return 1
    if not os.environ.get("UQUANT_SOURCE_DIR") or not os.environ.get("UQUANT_REPORT_STATE"):
        print("UQUANT_STATUS=REQUIRED_CHECKOUT_UNAVAILABLE")
        return 78
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
            str(root / "venv/bin/python"), "-m", "cli_runtime.daily", "--root", str(root / "operation")],
            source, env, events, "DAILY_SCAN", timeout=780)
    except Exception as exc:
        events.append({"stage": "BOOTSTRAP", "error_type": type(exc).__name__})
    finally:
        (root / "events.json").write_text(json.dumps({"source_sha": env.get("UQUANT_SOURCE_SHA"),
            "runner_sha": os.environ.get("GITHUB_SHA"), "events": events}, indent=2) + "\n")
        try:
            result = call(["python", "-m", "cli_runtime.publish", "--root", str(root)],
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

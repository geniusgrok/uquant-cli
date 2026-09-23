"""Public bootstrap only. Private source, execution output and state never go to public artifacts."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import date
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str], log) -> int:
    return subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                          timeout=840, check=False).returncode


def main() -> int:
    if os.environ.get("GITHUB_REPOSITORY") != "geniusgrok/uquant-cli":
        print("UQUANT_SCAN_STATUS=UNAPPROVED_RUNNER")
        return 1
    source_token = os.environ.get("UQUANT_READ_TOKEN", "")
    writer_token = os.environ.get("UQUANT_REPORT_WRITE_TOKEN", "")
    if not source_token or not writer_token:
        print("UQUANT_SCAN_STATUS=PRIVATE_DELIVERY_UNCONFIGURED")
        print("No production scan was started. The read-only source token is not a report writer.")
        return 78
    identity = os.environ["GITHUB_RUN_ID"] + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not all(part.isdigit() for part in identity.split("-")):
        return 1
    root = Path(os.environ["RUNNER_TEMP"]) / ("uquant-daily-" + identity)
    root.mkdir(mode=0o700, exist_ok=False)
    logs = root / "logs"
    logs.mkdir(mode=0o700)
    source = root / "source"
    env = os.environ.copy()
    env.pop("UQUANT_READ_TOKEN", None)
    env.update({"UV_CACHE_DIR": str(root / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never"})
    guard = ["python", "-m", "tools.cloud_guard", "--root", str(root / "journal")]
    auth = base64.b64encode(("x-access-token:" + source_token).encode()).decode()
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
               "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth}
    status = 1
    try:
        with (logs / "bootstrap.log").open("wb") as log:
            if run(["git", "clone", "--quiet", "--depth=1", "--branch=main",
                    "https://github.com/ychenracing/uquant.git", str(source)], cwd=root, env=git_env, log=log):
                raise RuntimeError("source checkout failed")
            for command in (["python", "-m", "pip", "install", "--disable-pip-version-check", "uv==0.11.33"],
                            ["uv", "sync", "--frozen", "--extra", "data"]):
                if run(command, cwd=source, env=env, log=log):
                    raise RuntimeError("locked runtime installation failed")
            if run([*guard, "inspect"], cwd=source, env=env, log=log):
                raise RuntimeError("unreconciled execution journal")
        with (logs / "scan.log").open("wb") as log:
            status = run([*guard, "run", "--name", "daily-scan", "--timeout", "720", "--",
                          "uv", "run", "--frozen", "--extra", "data", "python", "-m", "scripts.daily_scan",
                          "--work-root", str(root / "operation")], cwd=source, env=env, log=log)
    except Exception:
        print("UQUANT_SCAN_STATUS=BOOTSTRAP_OR_EXECUTION_FAILED")
    finally:
        saved = False
        if (source / "scripts/daily_scan_store.py").exists():
            try:
                with (root / "preservation.log").open("wb") as log:
                    run([*guard, "export", "--include-private-logs",
                         "--output", str(logs / "cloud.zip")], cwd=source, env=env, log=log)
                    saved = run(["python", "-m", "scripts.daily_scan_store", "--logs", str(logs),
                                 "--checkout", str(root / "log-state")], cwd=source, env=env, log=log) == 0
            except Exception:
                saved = False
        if not saved:
            print("UQUANT_SCAN_STATUS=PRIVATE_LOG_PRESERVATION_FAILED")
            status = 1
    public_status = root / "operation/public_status.json"
    if status == 0 and public_status.exists():
        outcome = json.loads(public_status.read_text(encoding="utf-8"))
        if outcome.get("status") not in {"COMPLETE", "PARTIAL", "REUSED", "MARKET_CLOSED", "MARKET_NOT_CLOSED"}:
            raise ValueError("invalid public status")
        day = date.fromisoformat(outcome["target_date"]).isoformat()
        print("UQUANT_SCAN_STATUS=" + outcome["status"])
        print("TARGET_DATE=" + day)
    elif status == 0:
        status = 1
    if status:
        print("UQUANT_SCAN_STATUS=FAILED_OR_BLOCKED; inspect private records; do not rerun blindly")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

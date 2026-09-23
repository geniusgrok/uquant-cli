"""Verify a pinned private candidate, emitting no source or raw execution logs."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.run_private_daily import state_key


def main() -> int:
    token = os.environ.get("UQUANT_READ_TOKEN", "")
    revision = os.environ.get("PRIVATE_CANDIDATE_SHA", "")
    if not token or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        print("PRIVATE_CHECK=UNCONFIGURED")
        return 1
    root = Path(os.environ["RUNNER_TEMP"]) / ("private-check-" + os.environ["GITHUB_RUN_ID"])
    root.mkdir(mode=0o700, exist_ok=False)
    source = root / "source"
    source.mkdir()
    logs = root / "logs"
    logs.mkdir()
    env = os.environ.copy()
    env.pop("UQUANT_READ_TOKEN", None)
    env.update({"UV_CACHE_DIR": str(root / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never",
                "UQUANT_STATE_PASSPHRASE": state_key(token)})
    authorization = base64.b64encode(("x-access-token:" + token).encode()).decode()
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
               "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + authorization}
    stage = "CHECKOUT"
    outcome = 1
    try:
        with (logs / "bootstrap.log").open("wb") as log:
            for command in (["git", "init", "--quiet"],
                            ["git", "remote", "add", "origin", "https://github.com/ychenracing/uquant.git"],
                            ["git", "fetch", "--quiet", "--depth=1", "origin", revision],
                            ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"]):
                subprocess.run(command, cwd=source, env=git_env, stdout=log, stderr=subprocess.STDOUT, timeout=240, check=True)
            if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() != revision:
                raise ValueError("candidate identity mismatch")
            stage = "LOCKED_INSTALL"
            for command in (["python", "-m", "pip", "install", "--disable-pip-version-check", "uv==0.11.33"],
                            ["uv", "sync", "--frozen", "--extra", "dev"]):
                subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240, check=True)
        print("PRIVATE_CANDIDATE_SHA=" + revision)
        print("TEST_FIXTURE_ONLY=TRUE; not a current-day signal run")
        paths = sorted(path.relative_to(source).as_posix() for pattern in ("scripts/daily_scan*.py", "tests/test_daily_scan*.py") for path in source.glob(pattern))
        for relative in paths:
            data = (source / relative).read_bytes()
            original = subprocess.check_output(["git", "show", revision + ":" + relative], cwd=source)
            if data != original:
                raise ValueError("immutable source byte mismatch")
            print("SOURCE_IDENTITY=" + json.dumps({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "git_blob_sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}))
        stage = "TESTS"
        with (logs / "scan.log").open("wb") as log:
            tests = subprocess.run(["python", "-m", "tools.cloud_guard", "--root", str(root / "journal"),
                                    "run", "--name", "observer-delivery-check", "--timeout", "420", "--",
                                    "uv", "run", "pytest", "tests/test_daily_scan.py", "tests/test_daily_scan_public.py",
                                    "--junitxml=" + str(root / "junit.xml")], cwd=source, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=450, check=False)
        suites = ET.parse(root / "junit.xml").getroot()
        totals = {key: sum(int(suite.attrib.get(key, 0)) for suite in suites.iter("testsuite")) for key in ("tests", "failures", "errors", "skipped")}
        print("PRIVATE_TEST_TOTALS=" + json.dumps(totals))
        for case in suites.iter("testcase"):
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is not None:
                # Only a bounded synthetic-test exception message, never traceback source lines.
                message = problem.attrib.get("message", "WITHHELD").splitlines()[0][:350]
                for secret in (token, env["UQUANT_STATE_PASSPHRASE"], os.environ.get("GITHUB_TOKEN", "")):
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
                print("TEST_FAILURE=" + json.dumps({"case": case.attrib.get("name"), "message": message}))
        stage = "LINT"
        with (root / "lint.json").open("wb") as log:
            lint = subprocess.run(["uv", "run", "ruff", "check", "--output-format=json", *paths], cwd=source, env=env,
                                  stdout=log, stderr=subprocess.PIPE, timeout=90, check=False)
        for issue in json.loads((root / "lint.json").read_text()):
            relative = str(Path(issue["filename"]).relative_to(source))
            print(f"PRIVATE_LINT={relative}:{int(issue['location']['row'])}:{issue['code']}")
        outcome = 0 if tests.returncode == 0 and lint.returncode == 0 and totals["skipped"] == 0 else 1
    except Exception:
        print("PRIVATE_CHECK_FAILED_STAGE=" + stage)
    finally:
        # This path is the same encrypted original store used by the daily runner.
        # No test-produced account or signal is published as a real daily result.
        if (source / "scripts/daily_scan_public.py").exists():
            try:
                for name in ("junit.xml", "lint.json"):
                    if (root / name).is_file():
                        shutil.copyfile(root / name, logs / name)
                with (root / "preservation.log").open("wb") as log:
                    subprocess.run([sys.executable, "-m", "tools.cloud_guard", "--root", str(root / "journal"),
                                    "export", "--include-private-logs", "--output", str(logs / "cloud.zip")],
                                   cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=60, check=False)
                    saved = subprocess.run([sys.executable, "-m", "scripts.daily_scan_store", "--logs", str(logs),
                                            "--checkout", str(root / "verification-state")], cwd=source, env=env,
                                           stdout=log, stderr=subprocess.STDOUT, timeout=180, check=False)
                print("ENCRYPTED_TEST_ORIGINALS_SAVED=" + str(saved.returncode == 0))
                if saved.returncode:
                    outcome = 1
            except Exception:
                print("ENCRYPTED_TEST_ORIGINALS_SAVED=False")
                outcome = 1
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())

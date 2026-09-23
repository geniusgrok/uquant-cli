"""锁定并准备只读生产源码的运行环境。"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CLI_ROOT = Path(__file__).resolve().parent
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

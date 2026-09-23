"""验证正式工作流提供了正确的运行器和状态仓库。"""
from __future__ import annotations

from collections.abc import Mapping


def validate_environment(env: Mapping[str, str]) -> tuple[str, int] | None:
    if env.get("GITHUB_REPOSITORY") != "geniusgrok/uquant-cli":
        return "UNAPPROVED_RUNNER", 1
    if not env.get("UQUANT_SOURCE_DIR") or not env.get("UQUANT_REPORT_STATE"):
        return "REQUIRED_CHECKOUT_UNAVAILABLE", 78
    return None

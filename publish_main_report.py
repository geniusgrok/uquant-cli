"""Publish a verified observer Markdown report on the CLI repository's main branch."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from uquant_cli.store import BRANCH, REPOSITORY, identity, read

API = f"https://api.github.com/repos/{REPOSITORY}"


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=payload, method=method, headers={
        "Authorization": "Bearer " + os.environ["REPORT_PUBLISH_TOKEN"],
        "Accept": "application/vnd.github+json", "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("report API response too large")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise RuntimeError(f"report API returned HTTP {exc.code}") from None


def read_main(path: str) -> bytes | None:
    result = api("GET", "/contents/" + path + "?ref=main")
    if result is None:
        return None
    if result.get("type") != "file" or result.get("encoding") != "base64":
        raise ValueError("unexpected main report object")
    data = base64.b64decode("".join(result["content"].split()), validate=True)
    if len(data) != result["size"] or blob_id(data) != result["sha"]:
        raise ValueError("main report integrity mismatch")
    return data


def source_report(root: Path) -> tuple[str, bytes] | None:
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root).decode().strip()
    if branch != BRANCH:
        raise ValueError("wrong report state branch")
    receipt = read(root, "latest.json") if (root / "latest.json").exists() else None
    if receipt is None:
        return None
    result = read(root, receipt["result_path"])
    if result["status"] not in {"COMPLETE", "PARTIAL"}:
        return None
    day = result["target_date"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid report date")
    path = f"reports/{day}.md"
    expected = receipt["files"][path]
    data = (root / path).read_bytes()
    if len(data) > 500_000 or identity(root / path) != expected:
        raise ValueError("unverified or oversized observer report")
    committed = subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=root)
    if committed != data:
        raise ValueError("observer report differs from committed state")
    return path, data


def publish(root: Path) -> str | None:
    found = source_report(root)
    if found is None:
        return None
    path, data = found
    existing = read_main(path)
    if existing is None:
        try:
            response = api("PUT", "/contents/" + path, {
                "message": "保存 " + Path(path).stem + " 盘后日报",
                "branch": "main", "content": base64.b64encode(data).decode(),
            })
        except RuntimeError:
            if read_main(path) != data:
                raise
        else:
            if response["content"]["sha"] != blob_id(data):
                raise ValueError("main publication receipt mismatch")
    elif existing != data:
        raise ValueError("existing main report differs from validated result")
    if read_main(path) != data:
        raise ValueError("main report readback mismatch")
    return f"{path} sha256={hashlib.sha256(data).hexdigest()}"


def main() -> None:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("unapproved publisher")
    root = Path(os.environ["REPORT_STATE_DIR"]).resolve()
    if not root.is_relative_to(Path(os.environ["GITHUB_WORKSPACE"]).resolve()):
        raise ValueError("report state outside workspace")
    result = publish(root)
    print("已核验并发布到 main：" + result if result else "本次没有完整盘后日报可发布")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("main 日报发布或回读失败；未重新计算策略")
        raise SystemExit(1) from None

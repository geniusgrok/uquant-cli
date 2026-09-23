"""Verify that only a committed observer report reaches main."""
import base64

import pytest

import publish_main_report as main_report
from tests.test_runtime import store
from uquant_cli.store import identity, put


def test_main_report_is_verified_idempotent_and_never_overwritten(tmp_path, monkeypatch):
    state, _ = store(tmp_path)
    path = "reports/2026-09-23.md"
    (state.root / "reports").mkdir()
    (state.root / path).write_text("# 2026-09-23 已完成日报\n", encoding="utf-8")
    put(state.root, "reports/2026-09-23/result.json",
        {"status": "COMPLETE", "target_date": "2026-09-23"})
    put(state.root, "latest.json", {"result_path": "reports/2026-09-23/result.json",
        "files": {path: identity(state.root / path)}})
    state.publish([path, "reports/2026-09-23/result.json", "latest.json"], "publish")
    stored = {}
    writes = []

    def fake_api(method, resource, body=None):
        if method == "GET":
            if "data" not in stored:
                return None
            data = stored["data"]
            return {"type": "file", "encoding": "base64", "size": len(data),
                    "content": base64.b64encode(data).decode(), "sha": main_report.blob_id(data)}
        assert method == "PUT" and body["branch"] == "main"
        writes.append(body)
        stored["data"] = base64.b64decode(body["content"])
        return {"content": {"sha": main_report.blob_id(stored["data"])}}

    monkeypatch.setattr(main_report, "api", fake_api)
    assert main_report.publish(state.root).startswith(path + " sha256=")
    assert main_report.publish(state.root).startswith(path + " sha256=")
    assert len(writes) == 1
    stored["data"] = b"different public report"
    with pytest.raises(ValueError, match="differs"):
        main_report.publish(state.root)

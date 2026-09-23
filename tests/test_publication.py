from test_runtime import store

import pytest

from cli_runtime.store import put


def test_recovery_preserves_only_approved_outputs(tmp_path, monkeypatch):
    from cli_runtime import publish
    st, _ = store(tmp_path)
    root = tmp_path / "run"
    candidate = root / "operation/publishable"
    put(candidate, "status.json", {"status": "FAILED"})
    put(candidate, "result.json", {"computed_but_unpublished": True})
    put(root, "events.json", {"events": []})
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(publish, "open_store", lambda _: st)
    publish.preserve(root)
    assert (st.root / "runs/123-1/result.json").read_bytes() == (candidate / "result.json").read_bytes()
    put(candidate, "unexpected.json", {"not_a_report": True})
    with pytest.raises(ValueError, match="unapproved runtime output path"):
        publish.preserve(root)

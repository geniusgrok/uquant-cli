import copy
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from uquant_cli import daily
from uquant_cli.market import SHANGHAI, SYMBOLS, WATCHLIST, session_context
from uquant_cli.report import compare, render, signals
from uquant_cli.store import GitStore, REMOTE, identity, path_in, put, verify

DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28"]


def result():
    raw = {"risk_summary": {}, "opportunity": "NORMAL", "risk": "CAUTION",
           "targets": [], "pending_orders": []}
    return {"observer_id": daily.OBSERVER, "status": "COMPLETE", "config_sha256": "cfg",
            "economic_code_hash": "code", "source_sha": "a" * 40, "target_date": "2026-09-23",
            "previous_session": "2026-09-22", "run_url": "https://example.invalid",
            "started_at": "test", "finished_at": "test", "observer_start": "test",
            "initial_cash": 2000000, "signals": signals(raw, {})}


def store(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    return GitStore(tmp_path / "writer", str(remote)), remote


def test_calendar_selects_latest_completed_session_before_1530():
    before_close = session_context(DATES, datetime(2026, 9, 23, 15, 29, tzinfo=SHANGHAI))
    assert before_close["status"] == "READY"
    assert before_close["target_date"] == "2026-09-22"
    assert before_close["previous_session"] == "2026-09-21"
    assert "15:30前" in before_close["selection_reason"]

    at_close = session_context(DATES, datetime(2026, 9, 23, 15, 30, tzinfo=SHANGHAI))
    assert at_close["target_date"] == "2026-09-23"
    assert at_close["previous_session"] == "2026-09-22"

    holiday = session_context(DATES, datetime(2026, 9, 25, 17, tzinfo=SHANGHAI))
    assert holiday["status"] == "READY"
    assert holiday["target_date"] == "2026-09-24"
    assert holiday["previous_session"] == "2026-09-23"

    weekend = session_context(DATES, datetime(2026, 9, 26, 12, tzinfo=SHANGHAI))
    assert weekend["target_date"] == "2026-09-24"


@pytest.mark.parametrize("dates", [[], DATES[:2], [*DATES, "2026-09-26"]])
def test_bad_calendar_fails_closed(dates):
    with pytest.raises(ValueError):
        session_context(dates, datetime(2026, 9, 23, 17, tzinfo=SHANGHAI))


def test_full_report_order_without_invented_qualifications():
    value = result()
    value["comparison"] = compare(value, None)
    text = render(value)
    positions = [text.index(symbol[2:]) for symbol in SYMBOLS]
    assert positions == sorted(positions)
    assert "不可比较" in text and "未提供" in text
    assert all(f"{symbol[2:]} {name}" in text for symbol, name in WATCHLIST)
    assert all(row["qualification"] is None for row in value["signals"]["stocks"])
    value["signals"]["market"]["selected_symbols"] = ["sh688498", "300308"]
    report = render(value)
    assert "688498 源杰科技" in report and "300308 中际旭创" in report
    assert "## 市场状态与风险信号" in report
    assert "## 全部13只标的信号总览" in report
    assert "## 逐只标的信号与风险明细" in report


def test_comparison_detects_weight_but_preserves_missing_fields():
    current, previous = result(), result()
    previous["target_date"] = current["previous_session"]
    previous["signals"]["stocks"][0]["orders"] = [{"side": "BUY", "target_weight": .1}]
    current["signals"]["stocks"][0]["orders"] = [{"side": "BUY", "target_weight": .2}]
    value = compare(current, previous)
    assert value["unavailable"] and any(item["field"] == "action" for item in value["changes"])
    previous["economic_code_hash"] = "different"
    assert compare(current, previous)["status"] == "INCOMPARABLE"


def test_store_rejects_upstream_destination(tmp_path):
    with pytest.raises(ValueError, match="destination"):
        GitStore(tmp_path / "bad", "https://github.com/ychenracing/uquant.git")
    assert REMOTE == "https://github.com/geniusgrok/uquant-cli.git"


def test_byte_readback_and_competing_claim(tmp_path):
    first, remote = store(tmp_path)
    put(first.root, "status.json", {"initialized": True})
    first.publish(["status.json"], "Initialize")
    second = GitStore(tmp_path / "second", str(remote))
    path = "claims/2026-09-23.json"
    put(first.root, path, {"status": "STARTED", "writer": 1})
    first.publish([path], "First claim")
    put(second.root, path, {"status": "STARTED", "writer": 2})
    with pytest.raises(RuntimeError, match="conflict"):
        second.publish([path], "Competing claim")
    assert b'"writer": 1' in first.git("show", "FETCH_HEAD:" + path).stdout


def test_missing_continuity_never_resets_account(tmp_path):
    put(tmp_path, "state/account.json", {})
    with pytest.raises(RuntimeError, match="reset"):
        daily.prior(tmp_path, {})
    put(tmp_path, "claims/2026-09-23.json", {"status": "STARTED"})
    with pytest.raises(RuntimeError, match="claim"):
        daily.prior(tmp_path, {})


def test_legacy_encrypted_account_is_not_silently_reset(tmp_path):
    put(tmp_path, ".state/account.json.manifest.json", {})
    with pytest.raises(RuntimeError, match="reset"):
        daily.prior(tmp_path, {})


def test_bad_paths_and_manifest(tmp_path):
    for name in ("../x", ".git/config", "/absolute"):
        with pytest.raises(ValueError):
            path_in(tmp_path, name)
    put(tmp_path, "data.json", {})
    verify(tmp_path, {"data.json": identity(tmp_path / "data.json")})
    with pytest.raises(ValueError):
        verify(tmp_path, {"data.json": {"bytes": 0, "sha256": "bad"}})


def test_preclose_publishes_status_markdown_without_deciding(tmp_path):
    st, _ = store(tmp_path)
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    metadata = {**result(), "status": "MARKET_NOT_CLOSED"}
    metadata.pop("signals")
    with patch.object(daily, "refresh") as refresh, patch.object(daily, "compute") as compute:
        outcome = daily.run_once(st, work, metadata)
        refresh.assert_not_called()
        compute.assert_not_called()
    report = "reports/2026-09-23.md"
    content = (st.root / report).read_bytes()
    assert b"MARKET_NOT_CLOSED" in content
    assert "本次未产生新的生产信号".encode("utf-8") in content
    assert st.git("show", "FETCH_HEAD:" + report).stdout == content


def test_failed_refresh_never_claims_or_decides(tmp_path):
    st, _ = store(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    with patch.object(daily, "refresh", side_effect=ValueError("missing close")):
        with patch.object(daily, "compute") as compute:
            with pytest.raises(ValueError):
                daily.run_once(st, work, {**result(), "status": "READY"})
            compute.assert_not_called()
    assert not list((st.root / "claims").glob("*.json"))


def test_reused_result_does_not_execute(tmp_path):
    st, _ = store(tmp_path)
    value = result()
    put(st.root, "reports/2026-09-23/result.json", value)
    name = "reports/2026-09-23/result.json"
    put(st.root, "latest.json", {"result_path": name, "files": {name: identity(st.root / name)}})
    work = tmp_path / "work"
    work.mkdir()
    with patch.object(daily, "refresh") as refresh, patch.object(daily, "compute") as compute:
        assert daily.run_once(st, work, {**value, "status": "READY"})["status"] == "REUSED"
        refresh.assert_not_called()
        compute.assert_not_called()

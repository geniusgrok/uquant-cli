import copy
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from uquant_cli import daily, market
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


def test_failed_same_day_claim_can_resume_only_from_verified_previous_receipt(tmp_path):
    previous = result()
    name = "reports/2026-09-23/result.json"
    put(tmp_path, name, previous)
    put(tmp_path, "state/account.json", {"continuous": True})
    put(tmp_path, "latest.json", {"result_path": name, "files": {
        name: identity(tmp_path / name),
        "state/account.json": identity(tmp_path / "state/account.json"),
    }})
    claim = {"status": "STARTED", "target_date": "2026-09-24",
             "previous_session": "2026-09-23",
             "run_url": "https://github.com/geniusgrok/uquant-cli/actions/runs/123"}
    put(tmp_path, "claims/2026-09-24.json", claim)
    context = {"target_date": "2026-09-24", "previous_session": "2026-09-23"}
    assert daily.prior(tmp_path, context) == previous
    put(tmp_path, "state/account.json", {"continuous": False})
    with pytest.raises(ValueError, match="manifest"):
        daily.prior(tmp_path, context)
    put(tmp_path, "state/account.json", {"continuous": True})
    put(tmp_path, "reports/2026-09-24/result.json", {})
    with pytest.raises(RuntimeError, match="claim"):
        daily.prior(tmp_path, context)


def test_missing_trading_day_is_recovered_before_current_day(tmp_path):
    previous = result()
    previous_path = "reports/2026-09-23/result.json"
    put(tmp_path, previous_path, previous)
    put(tmp_path, "state/account.json", {"continuous": True})
    put(tmp_path, "latest.json", {"result_path": previous_path, "files": {
        previous_path: identity(tmp_path / previous_path),
        "state/account.json": identity(tmp_path / "state/account.json")}})
    put(tmp_path, "claims/2026-09-24.json", {"status": "STARTED", "target_date": "2026-09-24",
        "previous_session": "2026-09-23",
        "run_url": "https://github.com/geniusgrok/uquant-cli/actions/runs/123"})
    today = {"target_date": "2026-09-25", "previous_session": "2026-09-24"}
    recovered = daily.next_unfinished_session(tmp_path, today,
        ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"])
    assert recovered["target_date"] == "2026-09-24"
    assert recovered["previous_session"] == "2026-09-23"
    assert recovered["calendar_target_date"] == "2026-09-25"
    assert daily.prior(tmp_path, recovered) == previous
    put(tmp_path, "state/account.json", {"continuous": False})
    with pytest.raises(ValueError, match="manifest"):
        daily.next_unfinished_session(tmp_path, today, ["2026-09-24", "2026-09-25"])
    put(tmp_path, "state/account.json", {"continuous": True})
    with pytest.raises(RuntimeError, match="calendar"):
        daily.next_unfinished_session(tmp_path, today, ["2026-09-24", "2026-09-25"])


def test_unfinished_first_day_can_resume_without_resetting_existing_state(tmp_path):
    put(tmp_path, "claims/2026-09-23.json", {"status": "STARTED", "target_date": "2026-09-23",
        "previous_session": "2026-09-22",
        "run_url": "https://github.com/geniusgrok/uquant-cli/actions/runs/123"})
    current = {"target_date": "2026-09-25", "previous_session": "2026-09-24"}
    selected = daily.next_unfinished_session(tmp_path, current,
        ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"])
    assert selected["target_date"] == "2026-09-23"
    assert selected["previous_session"] == "2026-09-22"
    assert daily.prior(tmp_path, selected) is None
    put(tmp_path, "state/account.json", {})
    with pytest.raises(RuntimeError, match="reset"):
        daily.prior(tmp_path, selected)


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



def test_observer_code_update_preserves_account_before_decision(tmp_path):
    from types import SimpleNamespace
    from uquant.account import economic_state_sha256, load_account, save_account
    from uquant.config import DEFAULT_CONFIG, config_fingerprint
    from uquant.engine import code_fingerprint
    from uquant.types import AccountState

    account = AccountState.empty(DEFAULT_CONFIG.initial_cash)
    account.code_hash = "previous-production-code"
    account.data_hash = "previous-market-data"
    before = economic_state_sha256(account)
    source = tmp_path / "state/account.json"
    source.parent.mkdir()
    save_account(account, source)
    work = tmp_path / "work"
    work.mkdir()

    class ReachedDecision(Exception):
        pass

    def decide(*, symbols, as_of, account):
        assert as_of == "2026-09-24"
        assert account.code_hash == code_fingerprint()
        assert economic_state_sha256(account) == before
        assert account.account_migrations[-1]["migration_type"] == "code_identity_only"
        raise ReachedDecision

    with patch("uquant.engine.ProductionEngine", return_value=SimpleNamespace(decide=decide)):
        with pytest.raises(ReachedDecision):
            daily.compute(tmp_path, work, {"target_date": "2026-09-24"},
                          {"config_sha256": config_fingerprint(DEFAULT_CONFIG)})
    assert economic_state_sha256(load_account(work / "account_before.json")) == before


def test_market_request_retries_transient_failures_only(monkeypatch):
    assert market._failure_category(FileNotFoundError("missing previous input")) == "data_contract"
    monkeypatch.setattr(market, "pause", lambda _: None)
    calls = 0

    def transient_then_success():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert market._retry_request(transient_then_success) == "ok"
    assert calls == 3

    calls = 0

    def invalid_response():
        nonlocal calls
        calls += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        market._retry_request(invalid_response)
    assert calls == 1


def test_source_failover_validates_prices_and_units():
    import pandas as pd
    raw = pd.DataFrame({'date': ['2026-09-22', '2026-09-23'],
                        'open': [10, 11], 'high': [12, 13], 'low': [9, 10],
                        'close': [11, 12], 'volume': [1200, 1500], 'amount': [13000, 18000]})
    invalid = raw.copy()
    invalid.loc[1, 'high'] = 1
    attempts = []

    def disconnected():
        raise ConnectionError('private diagnostic must not be recorded')

    frame, provider, estimated = market._select_source(
        {'Eastmoney': disconnected, 'Sina': lambda: invalid, 'Tencent': lambda: raw}, None,
        lambda value, name: market._normalized(value, 'sz300308', '2026-09-23', name), attempts)
    assert provider == 'Tencent' and not estimated
    assert frame.iloc[-1]['volume'] == 1500
    assert [item['status'] for item in attempts] == ['FAILED', 'FAILED', 'OK']
    assert 'private diagnostic' not in str(attempts)
    eastmoney, _ = market._normalized(raw, 'sz300308', '2026-09-23', 'Eastmoney')
    assert eastmoney.iloc[-1]['volume'] == 150000
    mainboard, _ = market._normalized(raw, 'sz000636', '2026-09-23', 'Tencent')
    assert mainboard.iloc[-1]['volume'] == 150000
    index, estimated = market._normalized(raw.drop(columns=['volume']).assign(amount=[100, 200]),
                                         'sh000300', '2026-09-23', 'Tencent', index=True)
    assert index.iloc[-1]['volume'] == 200 and estimated
    with pytest.raises(ValueError, match='target close'):
        market._normalized(raw, 'sz300308', '2026-09-24', 'Sina')


def test_source_failover_prefers_last_healthy_source_and_all_fail_closed():
    calls = []

    def failed():
        calls.append('failed')
        raise ConnectionError('offline')

    frame, provider, _ = market._select_source(
        {'Eastmoney': failed, 'Sina': lambda: [1, 2]}, 'Sina', lambda value, name: (value, False), [])
    assert provider == 'Sina' and not calls
    with pytest.raises(ConnectionError):
        market._select_source({'Eastmoney': failed, 'Sina': failed}, None,
                              lambda value, name: (value, False), [])


def test_adjusted_rebase_keeps_verified_prefix_and_rejects_other_revisions(tmp_path):
    import pandas as pd

    prior, today = tmp_path / "prior", tmp_path / "today"
    prior.mkdir()
    today.mkdir()
    symbol = "sh688498"
    old = pd.DataFrame({"date": ["2026-09-22", "2026-09-23"],
                        "open": [100.0, 110.0], "high": [101.0, 111.0],
                        "low": [99.0, 109.0], "close": [100.0, 110.0],
                        "volume": [1200.0, 1300.0], "amount": [120000.0, 143000.0],
                        "outstanding_share": [100000.0, 100000.0],
                        "turnover": [.012, .013]})
    fresh = pd.concat([old.assign(**{p: old[p] / 1.01 for p in ("open", "high", "low", "close")}),
                       pd.DataFrame({"date": ["2026-09-24"], "open": [99.0], "high": [100.0],
                                     "low": [98.0], "close": [99.0], "volume": [1400.0],
                                     "amount": [138600.0]})], ignore_index=True)
    old.to_csv(prior / (symbol + ".csv"), index=False)
    fresh.to_csv(today / (symbol + ".csv"), index=False)
    raw = old.tail(1)
    raw.to_csv(prior / (symbol + ".raw.csv"), index=False)
    pd.concat([raw, fresh.tail(1)]).to_csv(today / (symbol + ".raw.csv"), index=False)
    factor = market._anchor_adjusted_history(today, prior, symbol, "2026-09-23", "2026-09-24")
    anchored = pd.read_csv(today / (symbol + ".csv"))
    assert factor == pytest.approx(1.01)
    pd.testing.assert_frame_equal(anchored.iloc[:-1], old, check_dtype=False)
    assert anchored.iloc[-1]["close"] == pytest.approx(99.99)

    changed = fresh.copy()
    changed.loc[0, "volume"] += 1
    changed.to_csv(today / (symbol + ".csv"), index=False)
    with pytest.raises(ValueError, match="nonprice"):
        market._anchor_adjusted_history(today, prior, symbol, "2026-09-23", "2026-09-24")


def test_previous_float_metadata_revision_preserves_account_history(tmp_path):
    import pandas as pd

    prior, today = tmp_path / "prior", tmp_path / "today"
    prior.mkdir()
    today.mkdir()
    symbol = "sz300054"
    old = pd.DataFrame({"date": ["2026-09-22", "2026-09-23"], "open": [70., 71.],
                        "high": [72., 73.], "low": [69., 70.], "close": [71., 72.],
                        "volume": [1000., 1100.], "amount": [71000., 79200.],
                        "outstanding_share": [100000., 100000.], "turnover": [.01, .011]})
    new = old.copy()
    new.loc[1, "outstanding_share"] = 101000.
    new.loc[1, "turnover"] = 1100 / 101000
    new = pd.concat([new, pd.DataFrame({"date": ["2026-09-24"], "open": [72.],
                      "high": [73.], "low": [71.], "close": [72.], "volume": [1200.],
                      "amount": [86400.], "outstanding_share": [101000.],
                      "turnover": [1200 / 101000]})], ignore_index=True)
    old.to_csv(prior / (symbol + ".csv"), index=False)
    new.to_csv(today / (symbol + ".csv"), index=False)
    old.tail(1).to_csv(prior / (symbol + ".raw.csv"), index=False)
    new.tail(2).to_csv(today / (symbol + ".raw.csv"), index=False)
    assert market._anchor_adjusted_history(today, prior, symbol, "2026-09-23", "2026-09-24") == 1
    anchored = pd.read_csv(today / (symbol + ".csv"))
    pd.testing.assert_frame_equal(anchored.iloc[:-1], old, check_dtype=False)
    assert anchored.iloc[-1]["outstanding_share"] == 101000
    new.loc[0, "volume"] += 1
    new.to_csv(today / (symbol + ".csv"), index=False)
    with pytest.raises(ValueError, match="nonprice"):
        market._anchor_adjusted_history(today, prior, symbol, "2026-09-23", "2026-09-24")


def test_provider_deadline_interrupts_stalled_call():
    import time
    import signal
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TimeoutError):
        with market.request_deadline(.01):
            time.sleep(.2)
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_refresh_reuses_persisted_sources_and_accepts_single_day_raw_quote(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    import pandas as pd

    frame = pd.DataFrame({'date': ['2026-09-22', '2026-09-23'], 'open': [10, 11],
                          'high': [12, 13], 'low': [9, 10], 'close': [11, 12],
                          'volume': [1000, 2000], 'amount': [11000, 24000]})
    eastmoney = Mock(side_effect=ConnectionError('must not request an unneeded source'))
    ak = SimpleNamespace(stock_zh_a_hist=eastmoney,
                         stock_zh_a_daily=lambda **kw: frame if kw['adjust'] else frame.iloc[-1:],
                         stock_zh_a_hist_tx=eastmoney)
    monkeypatch.setitem(sys.modules, 'akshare', ak)
    monkeypatch.setitem(sys.modules, 'uquant.engine', SimpleNamespace(INDEX_SYMBOLS=(), REFERENCE_UNIVERSE=()))
    monkeypatch.setattr(market, 'SYMBOLS', ('sz300308',))
    audit = market.refresh(tmp_path / 'inputs', '2026-09-23', '2026-09-22', prior_audit={
        'coverage': {'sz300308': {'provider': 'Sina'}}, 'quotes': {'sz300308': {'provider': 'Sina'}}})
    assert not audit['failures']
    assert audit['coverage']['sz300308']['provider'] == 'Sina'
    assert audit['quotes']['sz300308']['close'] == 12
    assert audit['quotes']['sz300308']['change_pct'] is None
    eastmoney.assert_not_called()

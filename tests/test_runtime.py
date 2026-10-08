import copy
import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

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
    from uquant.models import AccountState

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

    with patch("uquant.data.DataStore", return_value=SimpleNamespace(adjustment="raw")), patch(
            "uquant.engine.ProductionEngine",
            return_value=SimpleNamespace(data=SimpleNamespace(adjustment="raw"), decide=decide)):
        with pytest.raises(ReachedDecision):
            daily.compute(tmp_path, work, {"target_date": "2026-09-24"},
                          {"config_sha256": config_fingerprint(DEFAULT_CONFIG)})
    assert economic_state_sha256(load_account(work / "account_before.json")) == before


def test_observer_schema_upgrade_preserves_verified_previous_account(tmp_path):
    from types import SimpleNamespace
    from uquant.account import load_account, save_account
    from uquant.config import DEFAULT_CONFIG, config_fingerprint
    from uquant.engine import code_fingerprint
    from uquant.models import ACCOUNT_SCHEMA_VERSION, AccountState

    account = AccountState.empty(DEFAULT_CONFIG.initial_cash)
    account.code_hash = "previous-production-code"
    account.data_hash = "previous-market-data"
    source = tmp_path / "state/account.json"
    source.parent.mkdir()
    save_account(account, source)
    payload = json.loads(source.read_text())
    payload["schema_version"] = 8
    for name in ("account_revision", "broker_binding", "broker_snapshots",
                 "external_cash_flows", "corporate_actions", "receivables", "dividend_tax_lots"):
        payload.pop(name, None)
    source.write_text(json.dumps(payload))
    old_bytes = source.read_bytes()
    work = tmp_path / "work"
    work.mkdir()

    class ReachedDecision(Exception):
        pass

    def decide(*, symbols, as_of, account):
        assert account.schema_version == ACCOUNT_SCHEMA_VERSION
        assert account.cash == DEFAULT_CONFIG.initial_cash and not account.positions
        assert account.code_hash == code_fingerprint()
        assert account.account_migrations[-1]["migration_type"] == "schema_upgrade"
        raise ReachedDecision

    with patch("uquant.data.DataStore", return_value=SimpleNamespace(adjustment="raw")), patch(
            "uquant.engine.ProductionEngine",
            return_value=SimpleNamespace(data=SimpleNamespace(adjustment="raw"), decide=decide)):
        with pytest.raises(ReachedDecision):
            daily.compute(tmp_path, work, {"target_date": "2026-09-28"},
                          {"config_sha256": config_fingerprint(DEFAULT_CONFIG)})
    assert source.read_bytes() == old_bytes
    assert load_account(work / "account_before.json").schema_version == ACCOUNT_SCHEMA_VERSION

    decide_after_config_change = Mock(side_effect=AssertionError("unverified configuration reached decision"))

    with patch("uquant.data.DataStore", return_value=SimpleNamespace(adjustment="raw")), patch(
            "uquant.engine.ProductionEngine",
            return_value=SimpleNamespace(data=SimpleNamespace(adjustment="raw"),
                                         decide=decide_after_config_change)):
        with pytest.raises(RuntimeError, match="configuration requires explicit reconciliation"):
            daily.compute(tmp_path, work, {"target_date": "2026-09-28"},
                          {"config_sha256": "previous-config"})
    decide_after_config_change.assert_not_called()
    assert source.read_bytes() == old_bytes
    assert load_account(work / "account_before.json").cash == DEFAULT_CONFIG.initial_cash
    assert not (work / "account_before.json.lock").exists()


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

    # A missing quote or historical contract failure is not a network retry.
    # Only refresh's validated TARGET_BAR_UNAVAILABLE path may wait for a bar.
    for error in (ValueError("target raw close missing"),
                  market.MarketDataError("HISTORICAL_ANCHOR_MISSING")):
        operation = Mock(side_effect=error)
        with pytest.raises(type(error)):
            market._retry_request(operation)
        assert operation.call_count == 1

    calls = 0
    def delayed_login():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("baostock login failed")
        return "connected"
    assert market._retry_request(delayed_login) == "connected"
    assert calls == 3


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
    new.loc[0, "outstanding_share"] = 101000.
    new.loc[0, "turnover"] = 1000 / 101000
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


def test_refresh_extends_verified_raw_prefix_and_uses_exchange_preclose(tmp_path, monkeypatch):
    import json
    import uquant.data
    import uquant.data_update
    import uquant.engine
    import pandas as pd

    base = tmp_path / 'base'
    base.mkdir()
    old = pd.DataFrame({'date': ['2026-09-24'], 'open': [10.], 'high': [11.],
                        'low': [9.], 'close': [10.], 'preclose': [9.5], 'volume': [1000.],
                        'amount': [10000.], 'volume_unit': ['shares'], 'special_treatment': [0]})
    old.to_csv(base / 'sz300308.csv', index=False)
    for index in ('sh000300', 'sh000682'):
        old.to_csv(base / (index + '.csv'), index=False)
    (base / 'CORPORATE_ACTIONS.json').write_text('[]')
    (base / 'DATA_MANIFEST.json').write_text(json.dumps({'snapshot_id': 'base', 'price_basis': 'raw',
        'start': '2014-01-01', 'end': '2026-09-24', 'suspended_dates': {'sz300308': []}}))

    class FakeStore:
        _validate = staticmethod(uquant.data.DataStore._validate)
        def __init__(self, root):
            self.root = root
            self.snapshot_manifest = json.loads((root / 'DATA_MANIFEST.json').read_text())
        def verify_file(self, name):
            assert (self.root / name).exists()
        def load(self, symbol, as_of=None):
            frame = pd.read_csv(self.root / (symbol + '.csv')).set_index('date')
            frame.index = pd.to_datetime(frame.index)
            return frame.loc[:as_of] if as_of else frame

    class FakeProvider:
        def stock_daily(self, symbol, start, end):
            assert start == '2026-09-24' and end == '2026-09-28'
            return pd.concat([old, old.assign(date='2026-09-28', close=11., preclose=10.)]), []
        def dividends(self, symbol, start, end):
            return []

    def update_snapshot(*, output_root, base_dir, symbols, start, end, provider):
        assert base_dir == base and symbols == {'sz300308'}
        extended, _ = provider.stock_daily('sz300308', start, end)
        target = output_root / 'raw-test'
        target.mkdir(parents=True)
        extended.to_csv(target / 'sz300308.csv', index=False)
        (target / 'CORPORATE_ACTIONS.json').write_text('[]')
        (target / 'DATA_MANIFEST.json').write_text(json.dumps({'snapshot_id': 'raw-test', 'price_basis': 'raw'}))
        return target

    monkeypatch.setattr(uquant.data, 'DataStore', FakeStore)
    monkeypatch.setattr(uquant.data_update, 'BaostockProvider', FakeProvider)
    monkeypatch.setattr(uquant.data_update, 'update_snapshot', update_snapshot)
    monkeypatch.setattr(uquant.engine, 'INDEX_SYMBOLS', ())
    monkeypatch.setattr(uquant.engine, 'REFERENCE_UNIVERSE', ())
    monkeypatch.setattr(market, 'SYMBOLS', ('sz300308',))
    audit = market.refresh(tmp_path / 'inputs', '2026-09-28', '2026-09-24', prior_root=base)
    assert not audit['failures']
    assert audit['quotes']['sz300308']['provider'] == 'baostock'
    assert audit['quotes']['sz300308']['close'] == 11
    assert audit['quotes']['sz300308']['change_pct'] == pytest.approx(10)
    assert pd.read_csv(tmp_path / 'inputs/sz300308.csv').iloc[0]['close'] == 10


def raw_market_rows():
    import pandas as pd
    return pd.DataFrame({'date': ['2026-09-24'], 'open': [10.], 'high': [11.],
        'low': [9.], 'close': [10.], 'preclose': [9.5], 'volume': [1000.],
        'amount': [10000.], 'volume_unit': ['shares'], 'special_treatment': [0]})


@pytest.mark.parametrize('failure,reason', [
    ('missing_anchor', 'HISTORICAL_ANCHOR_MISSING'),
    ('duplicate_anchor', 'HISTORICAL_ANCHOR_DUPLICATED'),
    ('revised_anchor', 'HISTORICAL_ANCHOR_REVISED'),
    ('micro_revision', 'HISTORICAL_ANCHOR_REVISED'),
    ('bad_price', 'RESPONSE_FORMAT_INVALID'),
    ('bad_date', 'RESPONSE_FORMAT_INVALID'),
    ('incomplete_target', 'TARGET_BAR_UNAVAILABLE'),
])
def test_raw_extension_separates_unavailable_bar_from_integrity_failures(failure, reason):
    import pandas as pd
    old = raw_market_rows()
    today = old.assign(date='2026-09-28', close=11., preclose=10.)
    fresh = pd.concat([old, today], ignore_index=True)
    if failure == 'missing_anchor':
        fresh = today
    elif failure == 'duplicate_anchor':
        fresh = pd.concat([old, fresh], ignore_index=True)
    elif failure == 'revised_anchor':
        fresh.loc[0, 'close'] = 10.5
    elif failure == 'micro_revision':
        fresh.loc[0, 'close'] += .000001
    elif failure == 'bad_price':
        fresh.loc[1, 'close'] = float('nan')
    elif failure == 'bad_date':
        fresh.loc[0, 'date'] = 'not-a-date'
    else:
        fresh = old
    diagnostic = {}
    with pytest.raises(market.MarketDataError) as failed:
        market._validated_extension(fresh, [], old, 'sh600487', '2026-09-28', diagnostic)
    assert failed.value.reason_code == reason
    assert diagnostic['normalized_response_rows'] == len(fresh)
    assert len(diagnostic['normalized_response_sha256']) == 64


def test_market_recovery_rebuilds_full_snapshot_and_retains_failure_facts(tmp_path, monkeypatch):
    root = tmp_path / 'inputs'
    calls, waits = [], []

    def build(destination, day, previous, **kwargs):
        assert not destination.exists()
        destination.mkdir()
        calls.append((day, previous))
        if len(calls) < 3:
            detail = {'category': 'unavailable', 'reason_code': 'TARGET_BAR_UNAVAILABLE',
                      'operation': 'stock_daily', 'symbol': 'sh600487', 'anchor_rows': 1}
            put(destination, 'audit.json', {'failures': {'snapshot': detail}})
            (destination / 'unaccepted.csv').write_text('partial input')
            raise market.LiveInputError({'snapshot': detail})
        assert not (destination / 'unaccepted.csv').exists()
        audit = {'failures': {}, 'all_inputs_validated': True}
        put(destination, 'audit.json', audit)
        return audit

    monkeypatch.setattr(market, '_refresh_once', build)
    monkeypatch.setattr(market, 'pause', waits.append)
    audit = market.refresh(root, '2026-09-28', '2026-09-24')
    assert calls == [('2026-09-28', '2026-09-24')] * 3
    assert waits == [60, 120]
    assert audit['all_inputs_validated']
    assert [item['attempt'] for item in audit['recovery_attempts']] == [1, 2]
    assert json.loads((root / 'audit.json').read_text()) == audit


@pytest.mark.parametrize('reason', ['HISTORICAL_ANCHOR_MISSING', 'HISTORICAL_ANCHOR_DUPLICATED',
    'HISTORICAL_ANCHOR_REVISED', 'RESPONSE_FORMAT_INVALID', 'TARGET_BAR_UNAVAILABLE'])
def test_market_recovery_failures_remain_failed_and_preserved(tmp_path, monkeypatch, reason):
    from unittest.mock import Mock
    root = tmp_path / 'inputs'
    builds = []

    def build(destination, *args, **kwargs):
        destination.mkdir()
        builds.append(destination)
        detail = {'category': 'data_contract', 'reason_code': reason}
        put(destination, 'audit.json', {'failures': {'snapshot': detail}})
        raise market.LiveInputError({'snapshot': detail})

    wait = Mock()
    monkeypatch.setattr(market, '_refresh_once', build)
    monkeypatch.setattr(market, 'pause', wait)
    with pytest.raises(market.LiveInputError):
        market.refresh(root, '2026-09-28', '2026-09-24')
    assert len(builds) == (3 if reason == 'TARGET_BAR_UNAVAILABLE' else 1)
    assert wait.call_count == (2 if reason == 'TARGET_BAR_UNAVAILABLE' else 0)
    audit = json.loads((root / 'audit.json').read_text())
    assert audit['failures']['snapshot']['reason_code'] == reason
    assert len(audit['recovery_attempts']) == len(builds)


def raw_snapshot_fixture(tmp_path):
    import hashlib
    import pandas as pd
    root = tmp_path / 'base'
    root.mkdir()
    dates = pd.bdate_range(end='2026-09-24', periods=121).strftime('%Y-%m-%d')
    bars = pd.concat([raw_market_rows().assign(date=day, preclose=10.) for day in dates], ignore_index=True)
    for symbol in ('sh600487', 'sz300308', 'sh000300', 'sh000682'):
        bars.to_csv(root / (symbol + '.csv'), index=False)
    (root / 'CORPORATE_ACTIONS.json').write_text('[]')
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.iterdir()}
    put(root, 'DATA_MANIFEST.json', {'snapshot_id': 'fixture-only', 'price_basis': 'raw',
        'start': dates[0], 'end': dates[-1], 'files': files, 'suspended_dates': {}})
    return root, bars


def test_recovery_rechecks_risk_basket_with_native_snapshot_validator(tmp_path, monkeypatch):
    import uquant.data
    import uquant.data_update
    import uquant.engine
    import pandas as pd
    base, bars = raw_snapshot_fixture(tmp_path)
    stock_calls, waits = [], []

    class Provider:
        name = 'baostock'
        def stock_daily(self, symbol, start, end):
            stock_calls.append(symbol)
            anchor = bars.tail(1)
            if symbol == 'sz300308' and stock_calls.count(symbol) == 1:
                return anchor, []
            return pd.concat([anchor, anchor.assign(date=end)], ignore_index=True), []
        def dividends(self, symbol, start, end):
            return []
        def index_daily(self, symbol, start, end):
            anchor = bars.tail(1)
            return pd.concat([anchor, anchor.assign(date=end)], ignore_index=True)

    monkeypatch.setattr(uquant.data_update, 'BaostockProvider', Provider)
    monkeypatch.setattr(uquant.engine, 'REFERENCE_UNIVERSE', ('sh600487',))
    monkeypatch.setattr(market, 'SYMBOLS', ('sz300308',))
    monkeypatch.setattr(market, 'pause', waits.append)
    destination = tmp_path / 'inputs'
    audit = market.refresh(destination, '2026-09-28', '2026-09-24', prior_root=base)
    assert stock_calls == ['sh600487', 'sz300308'] * 2
    assert waits == [60]
    assert audit['recovery_attempts'][0]['failures']['snapshot']['reason_code'] == 'TARGET_BAR_UNAVAILABLE'
    report = uquant.data_update.check_snapshot(destination, as_of='2026-09-28', daily=True,
                                               required=['sh600487', 'sz300308'])
    assert report['ok'] and set(report['coverage']) == {'sh600487', 'sz300308', 'sh000300', 'sh000682'}
    pd.testing.assert_frame_equal(pd.read_csv(destination / 'sh600487.csv').iloc[:-1], bars)


@pytest.mark.parametrize('kind,reason', [('numeric', 'RESPONSE_FORMAT_INVALID'),
    ('unknown_status', 'RESPONSE_FORMAT_INVALID'), ('oversize', 'RESPONSE_TOO_LARGE')])
def test_failed_stock_response_is_preserved_before_numeric_conversion(tmp_path, monkeypatch, kind, reason):
    import uquant.data_update
    base, _ = raw_snapshot_fixture(tmp_path)
    fields = 'date,open,high,low,close,preclose,volume,amount,tradestatus,isST'.split(',')
    row = ['2026-09-24', 'broken-number', '11', '9', '10', '10', '1000', '10000', '1', '0']
    if kind == 'unknown_status':
        row[1], row[-2], row[-1] = '10', 'UNKNOWN', 'UNKNOWN'
    elif kind == 'oversize':
        row[1] = 'x' * 65
    rows = [row]
    if kind == 'unknown_status':
        row[0] = '2026-09-28'
        rows = [['2026-09-24', '10', '11', '9', '10', '10', '1000', '10000', '1', '0'], row]

    class Response:
        error_code = '0'
        def __init__(self):
            self.fields, self.index = fields, -1
        def next(self):
            self.index += 1
            return self.index < len(rows)
        def get_row_data(self):
            return rows[self.index]

    class Provider(uquant.data_update.BaostockProvider):
        def __init__(self):
            from types import SimpleNamespace
            self._bs = SimpleNamespace(query_history_k_data_plus=lambda *args, **kwargs: Response())

    monkeypatch.setattr(uquant.data_update, 'BaostockProvider', Provider)
    wait = Mock()
    monkeypatch.setattr(market, 'pause', wait)
    with pytest.raises(market.LiveInputError):
        market.refresh(tmp_path / 'inputs', '2026-09-28', '2026-09-24', prior_root=base)
    detail = json.loads((tmp_path / 'inputs/audit.json').read_text())['failures']['snapshot']
    assert detail['reason_code'] == reason
    if kind == 'oversize':
        assert 'response' not in detail
        assert detail['raw_response_bytes'] > 65 and len(detail['raw_response_sha256']) == 64
    else:
        assert detail['response'] == {'fields': fields, 'rows': rows}
        import hashlib
        body = json.dumps(detail['response'], ensure_ascii=False, separators=(',', ':')).encode()
        assert detail['raw_response_bytes'] == len(body)
        assert detail['raw_response_sha256'] == hashlib.sha256(body).hexdigest()
    wait.assert_not_called()


def action_identity_fixture(tmp_path):
    import hashlib
    import shutil
    from uquant.account import save_account
    from uquant.data import DataStore
    from uquant.models import AccountState

    base, _ = raw_snapshot_fixture(tmp_path)
    actions = [{'event_id': 'sh600487:2026-09-23:distribution', 'symbol': 'sh600487',
        'ex_date': '2026-09-23', 'cash_per_share': .1, 'share_ratio': 0.,
        'announce_date': '2026-09-21', 'register_date': '2026-09-22', 'pay_date': '2026-09-23',
        'source': 'fixture-only', 'description': 'fixture-only'}]
    put(base, 'CORPORATE_ACTIONS.json', actions)
    manifest = json.loads((base / 'DATA_MANIFEST.json').read_text())
    manifest['files']['CORPORATE_ACTIONS.json'] = hashlib.sha256((base / 'CORPORATE_ACTIONS.json').read_bytes()).hexdigest()
    put(base, 'DATA_MANIFEST.json', manifest)
    root = tmp_path / 'observer'
    shutil.copytree(base, root / 'inputs')
    current = tmp_path / 'current'
    shutil.copytree(base, current)
    data = DataStore(root / 'inputs')
    account = AccountState.empty(2_000_000)
    account.data_hash_as_of = '2026-09-24'
    account.data_hash_symbols = ['sh600487', 'sz300308', 'sh000300', 'sh000682']
    files = data.manifest(account.data_hash_symbols, as_of=account.data_hash_as_of).files
    files.pop('CORPORATE_ACTIONS.json')
    account.data_hash = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    account.risk_streaks = {'sh600487': 2}
    save_account(account, root / 'state/account.json')
    previous = {**result(), 'target_date': '2026-09-24', 'source_sha': daily.LEGACY_ACTION_MANIFEST_PRODUCER}
    name = 'reports/2026-09-24/result.json'
    put(root, name, previous)
    put(root, 'latest.json', {'result_path': name, 'files': {
        name: identity(root / name), 'state/account.json': identity(root / 'state/account.json'),
        **{path.relative_to(root).as_posix(): identity(path) for path in (root / 'inputs').iterdir()}}})
    return root, account, current, previous


def test_action_manifest_identity_changes_only_proven_binding_and_audit(tmp_path):
    from uquant.account import economic_state_sha256
    from uquant.data import DataStore
    root, account, current, previous = action_identity_fixture(tmp_path)
    before = account.to_dict()
    disk_bytes = (root / 'state/account.json').read_bytes()
    old_economic_hash = economic_state_sha256(account)
    daily.rebind_action_manifest_identity(root, account, DataStore(current), previous)
    after = account.to_dict()
    assert after['data_hash'] != before['data_hash']
    migration = account.account_migrations[-1]
    assert migration['economic_state_sha256_before'] == old_economic_hash
    assert migration['economic_state_sha256_after'] == economic_state_sha256(account) != old_economic_hash
    assert migration['previous_identity_economic_state_sha256_after'] == old_economic_hash
    after['data_hash'], after['account_migrations'] = before['data_hash'], before['account_migrations']
    assert after == before
    assert (root / 'state/account.json').read_bytes() == disk_bytes
    bound = account.to_dict()
    daily.rebind_action_manifest_identity(root, account, DataStore(current), previous)
    assert account.to_dict() == bound


@pytest.mark.parametrize('failure', ['unknown_source', 'pending', 'broker', 'unverified_old_input',
                                   'revised_price', 'revised_action', 'unknown_old_digest'])
def test_action_manifest_identity_refuses_economic_or_historical_reconciliation(tmp_path, failure):
    import hashlib
    from uquant.data import DataStore
    root, account, current, previous = action_identity_fixture(tmp_path)
    if failure == 'unknown_source':
        previous['source_sha'] = '0' * 40
    elif failure == 'pending':
        account.pending_orders.append({'unresolved': True})
    elif failure == 'broker':
        account.broker_as_of = '2026-09-24'
    elif failure == 'unverified_old_input':
        receipt = json.loads((root / 'latest.json').read_text())
        receipt['files'].pop('inputs/CORPORATE_ACTIONS.json')
        put(root, 'latest.json', receipt)
    elif failure in {'revised_price', 'revised_action'}:
        path = current / ('sh600487.csv' if failure == 'revised_price' else 'CORPORATE_ACTIONS.json')
        if failure == 'revised_price':
            import pandas as pd
            rows = pd.read_csv(path)
            rows.loc[0, 'volume'] += 1
            rows.to_csv(path, index=False)
        else:
            actions = json.loads(path.read_text())
            actions[0]['cash_per_share'] += .1
            path.write_text(json.dumps(actions))
        manifest = json.loads((current / 'DATA_MANIFEST.json').read_text())
        manifest['files'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        put(current, 'DATA_MANIFEST.json', manifest)
    else:
        account.data_hash = '0' * 64
    before = account.to_dict()
    with pytest.raises(RuntimeError):
        daily.rebind_action_manifest_identity(root, account, DataStore(current), previous)
    assert account.to_dict() == before


def config_label_fixture(tmp_path):
    from uquant.account import save_account
    root, account, current, previous = action_identity_fixture(tmp_path)
    old_hash = 'b2acb71bcf17245eaf7cbe0ac0ac59633f8996818e4c7ba8ae76b246b69f9f84'
    initial_hash = '4d9c3495556d78e24f629355bb2bedbd4f3e823a53cf30499c9f18e04d53fda6'
    account.account_migrations = [{'migration_type': 'configuration_binding',
        'effective_config_sha256': initial_hash}, {'migration_type': 'configuration_rebind',
        'from_config_sha256': initial_hash, 'to_config_sha256': old_hash}]
    previous['config_sha256'] = old_hash
    save_account(account, root / 'state/account.json')
    name = 'reports/2026-09-24/result.json'
    put(root, name, previous)
    receipt = json.loads((root / 'latest.json').read_text())
    for path in ('state/account.json', name):
        receipt['files'][path] = identity(root / path)
    put(root, 'latest.json', receipt)
    return root, account, previous


def test_only_fixed_config_labels_create_native_binding_preserving_original_audit(tmp_path):
    from uquant.account import economic_state_sha256
    from uquant.config import config_fingerprint
    root, account, previous = config_label_fixture(tmp_path)
    before, old_hash = account.to_dict(), economic_state_sha256(account)
    disk_before = (root / 'state/account.json').read_bytes()
    daily.rebind_config_labels(root, account, previous)
    assert account.account_migrations[:-1] == before['account_migrations']
    assert account.account_migrations[-1]['migration_type'] == 'configuration_binding'
    assert account.account_migrations[-1]['effective_config_sha256'] == config_fingerprint()
    assert account.account_migrations[-1]['metadata_labels_added'] == {
        'economic_core': 'legacy_lots', 'remove_relative_strength': True}
    assert economic_state_sha256(account) == old_hash
    after = account.to_dict()
    after['account_migrations'] = before['account_migrations']
    assert after == before
    assert (root / 'state/account.json').read_bytes() == disk_before


@pytest.mark.parametrize('failure', ['unknown_source', 'unknown_previous_hash', 'unrecorded_previous_hash',
                                   'economic_parameter_change', 'missing_receipt_original'])
def test_config_labels_do_not_authorize_unknown_or_semantic_policy_changes(tmp_path, monkeypatch, failure):
    import uquant.config
    root, account, previous = config_label_fixture(tmp_path)
    if failure == 'unknown_source':
        previous['source_sha'] = '0' * 40
    elif failure == 'unknown_previous_hash':
        previous['config_sha256'] = '0' * 64
    elif failure == 'unrecorded_previous_hash':
        account.account_migrations[-1]['to_config_sha256'] = '0' * 64
    elif failure == 'economic_parameter_change':
        monkeypatch.setattr(uquant.config, 'DEFAULT_CONFIG', uquant.config.DEFAULT_CONFIG.override(max_positions=5))
    else:
        receipt = json.loads((root / 'latest.json').read_text())
        receipt['files'].pop('state/account.json')
        put(root, 'latest.json', receipt)
    before = account.to_dict()
    with pytest.raises(RuntimeError):
        daily.rebind_config_labels(root, account, previous)
    assert account.to_dict() == before

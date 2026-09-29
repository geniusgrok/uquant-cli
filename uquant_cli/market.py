"""多源实时行情适配，所有来源均经过生产数据校验。"""
from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import signal
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, time
from time import sleep as pause
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .store import put

SHANGHAI = ZoneInfo("Asia/Shanghai")
WATCHLIST = (
    ("sz300308", "中际旭创"), ("sz300502", "新易盛"), ("sz300394", "天孚通信"),
    ("sh688256", "寒武纪"), ("sh603986", "兆易创新"), ("sh688072", "拓荆科技"),
    ("sh688300", "联瑞新材"), ("sz300054", "鼎龙股份"), ("sh688361", "中科飞测"),
    ("sz002409", "雅克科技"), ("sh688498", "源杰科技"), ("sh688120", "华海清科"),
    ("sz002384", "东山精密"),
)
SYMBOLS = tuple(symbol for symbol, _ in WATCHLIST)


class LiveInputError(ValueError):
    def __init__(self, failures: dict[str, dict[str, str]]) -> None:
        self.stage = "MARKET_DATA"
        counts = Counter(item["category"] for item in failures.values())
        labels = {"network": "网络连接失败", "data_contract": "行情质量校验失败",
                  "invalid_response": "接口返回不完整", "provider": "数据接口异常"}
        details = "、".join(f"{labels.get(key, '其他异常')}{count}项" for key, count in sorted(counts.items()))
        self.safe_summary = f"{len(failures)}项行情未通过校验（{details}）；未使用旧数据替代。"
        super().__init__(self.safe_summary)


def _retry_request(operation, attempts: int = 3):
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:
            name = type(exc).__name__
            retryable = (isinstance(exc, (TimeoutError, OSError))
                         or name in {"ConnectionError", "ConnectTimeout", "ReadTimeout",
                                     "Timeout", "DataContractError"}
                         or (isinstance(exc, ValueError)
                             and str(exc) in {"target close or history missing",
                                              "index target close or history missing",
                                              "raw quote missing or duplicated"}))
            if not retryable or index + 1 == attempts:
                raise
            pause(2**index)
    raise RuntimeError("market request retry exhausted")


def _failure_category(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, FileNotFoundError):
        return "data_contract"
    if isinstance(exc, (TimeoutError, OSError)) or name in {
            "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout"}:
        return "network"
    if name == "DataContractError":
        return "data_contract"
    if isinstance(exc, ValueError):
        return "invalid_response"
    return "provider"


def session_context(dates: list[str], now: datetime) -> dict:
    if now.tzinfo is None:
        raise ValueError("timezone-aware clock required")
    local = now.astimezone(SHANGHAI)
    day = local.date().isoformat()
    sessions = sorted(set(dates))
    if not sessions or sessions[0] >= day or sessions[-1] < day:
        raise ValueError("trading calendar coverage missing")
    for value in sessions:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.isoformat() != value or parsed.weekday() > 4:
            raise ValueError("invalid trading session")
    is_session = day in sessions
    if is_session and local.time() >= time(15, 30):
        target = day
        reason = "当日已过15:30收盘确认时点"
    else:
        target = max((value for value in sessions if value < day), default="")
        reason = ("15:30前使用前一交易日收盘数据" if is_session
                  else "非交易日使用最近一个已完成交易日")
    previous = max((value for value in sessions if value < target), default="")
    if not target or not previous:
        raise ValueError("trading calendar history missing")
    return {"requested_date": day, "target_date": target, "previous_session": previous,
            "status": "READY", "selection_reason": reason,
            "checked_at": local.isoformat(), "calendar_source": "akshare/Sina"}


def calendar_now(now: datetime) -> tuple[dict, list[str]]:
    ak = importlib.import_module("akshare")
    frame = ak.tool_trade_date_hist_sina()
    dates = pd.to_datetime(frame["trade_date"], errors="raise").dt.strftime("%Y-%m-%d").tolist()
    # Decisions only need recent sessions, not historical exchange weekend regimes.
    start = f"{now.astimezone(SHANGHAI).year - 1}-01-01"
    dates = [value for value in dates if value >= start]
    return session_context(dates, now), dates


@contextmanager
def request_deadline(seconds: float = 25):
    """限制整个适配器调用的耗时，包含依赖内部未设置超时的请求。"""
    def expired(signum, frame):
        raise TimeoutError("market provider deadline exceeded")

    old_handler = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)


def _normalized(raw, symbol: str, day: str, provider: str, *, index: bool = False, minimum_rows: int = 2):
    from uquant.data import DataStore

    mapping = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
               "收盘": "close", "成交量": "volume", "成交额": "amount"}
    frame = raw.rename(columns=mapping).copy()
    if provider == "Eastmoney":
        frame["volume"] = pd.to_numeric(frame["volume"], errors="raise") * 100
    elif provider == "Tencent" and index:
        # 锁定版本的指数接口将第六项成交量误命名为 amount，实际单位为股。
        frame = frame.rename(columns={"amount": "volume"})
    elif provider == "Tencent" and symbol.startswith("sz000"):
        # 锁定版本误将深市主板视为指数，其股票成交量仍需从手转换为股。
        frame["volume"] = pd.to_numeric(frame["volume"], errors="raise") * 100
    dates = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.loc[dates <= pd.Timestamp(day)].copy()
    estimated_amount = "amount" not in frame
    if not estimated_amount and pd.to_numeric(frame["amount"], errors="coerce").isna().any():
        raise ValueError("provider amount missing")
    frame = DataStore._validate(frame, symbol)
    if len(frame) < minimum_rows or str(frame.index[-1].date()) != day:
        raise ValueError("target close or history missing")
    return frame, estimated_amount


def _select_source(providers: dict, preferred: str | None, normalize, attempts: list):
    names = list(providers)
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    last = None
    for name in names:
        try:
            with request_deadline():
                frame, estimated = normalize(providers[name](), name)
            attempts.append({"provider": name, "status": "OK", "rows": len(frame)})
            return frame, name, estimated
        except Exception as exc:
            last = exc
            attempts.append({"provider": name, "status": "FAILED",
                             "type": type(exc).__name__, "category": _failure_category(exc)})
    raise last if last is not None else ValueError("no market provider")


def _anchor_adjusted_history(root: Path, prior_root: Path, symbol: str, previous: str, day: str) -> float | None:
    """Keep the account's verified price scale when a provider rebases qfq history."""
    old = pd.read_csv(prior_root / (symbol + ".csv"))
    current_path = root / (symbol + ".csv")
    current = pd.read_csv(current_path)
    prefix = current.loc[current["date"] <= previous].reset_index(drop=True)
    if not old["date"].equals(prefix["date"]) or len(current) != len(old) + 1 or current.iloc[-1]["date"] != day:
        raise ValueError("adjusted history dates differ from verified account input")
    price = ["open", "high", "low", "close"]
    float_metadata = ["outstanding_share", "turnover"]
    stable = [column for column in old if column not in (*price, *float_metadata)]
    if not old[stable].equals(prefix[stable]):
        raise ValueError("adjusted history nonprice fields changed")
    metadata_changed = not old[float_metadata].equals(prefix[float_metadata])
    if old.equals(prefix):
        return None
    # A real ex-date rebase changes the adjusted scale but not the preceding raw close.
    old_raw = pd.read_csv(prior_root / (symbol + ".raw.csv"))
    new_raw = pd.read_csv(root / (symbol + ".raw.csv"))
    prior_raw = old_raw.loc[old_raw["date"] == previous]
    current_raw = new_raw.loc[new_raw["date"] == previous]
    raw_columns = ["date", *price, "volume", "amount"] if metadata_changed else list(old_raw)
    if (len(prior_raw) != 1 or not prior_raw[raw_columns].reset_index(drop=True).equals(
            current_raw[raw_columns].reset_index(drop=True))):
        raise ValueError("raw history changed with adjusted history")
    factor = float(old.iloc[-1]["close"] / prefix.iloc[-1]["close"])
    if not math.isfinite(factor) or factor <= 0 or not ((old[price] - prefix[price] * factor).abs() <= 0.011).all().all():
        raise ValueError("adjusted history is not a uniform price rebase")
    if metadata_changed and not old[price].equals(prefix[price]):
        raise ValueError("price and float metadata both changed")
    today = current.tail(1).copy()
    today[price] = today[price] * factor
    pd.concat([old, today], ignore_index=True).to_csv(current_path, index=False)
    return factor


def refresh(root: Path, day: str, previous: str, *, prior_audit: dict | None = None,
            prior_root: Path | None = None) -> dict:
    """Extend the verified raw snapshot with the production data-update validator."""
    from uquant.data import DataStore
    from uquant.data_update import BaostockProvider, STOCK_COLUMNS, update_snapshot
    from uquant.engine import INDEX_SYMBOLS, REFERENCE_UNIVERSE

    del prior_audit
    candidate = DataStore(prior_root) if prior_root is not None and (prior_root / "DATA_MANIFEST.json").exists() else None
    base = candidate if candidate is not None and candidate.snapshot_manifest.get("price_basis") == "raw" else DataStore(
        Path(os.environ["UQUANT_SOURCE_DIR"]) / "data")
    if base.snapshot_manifest.get("price_basis") != "raw" or base.snapshot_manifest.get("end") != previous:
        raise RuntimeError("verified raw snapshot does not end at previous session")
    actions_path = base.root / "CORPORATE_ACTIONS.json"
    base.verify_file(actions_path.name)
    for index in ("sh000300", "sh000682"):
        base.verify_file(index + ".csv")
    actions = json.loads(actions_path.read_text())
    suspended = base.snapshot_manifest["suspended_dates"]
    live = BaostockProvider()

    class IncrementalProvider:
        name = "baostock"

        def stock_daily(self, symbol, start, end):
            if not (base.root / (symbol + ".csv")).exists():
                return live.stock_daily(symbol, start, end)
            base.verify_file(symbol + ".csv")
            old = pd.read_csv(base.root / (symbol + ".csv"), dtype={"date": str})
            anchor = old["date"].iloc[-1]
            fresh, halted = live.stock_daily(symbol, anchor, end)
            matching = fresh.loc[fresh["date"] == anchor, list(STOCK_COLUMNS)]
            if len(matching) != 1:
                raise ValueError("raw anchor missing or duplicated")
            pd.testing.assert_frame_equal(old.tail(1).reset_index(drop=True)[list(STOCK_COLUMNS)],
                                          matching.reset_index(drop=True), check_dtype=False)
            extension = fresh.loc[fresh["date"] > anchor, list(STOCK_COLUMNS)]
            if extension.empty and day not in halted:
                raise ValueError("target raw close missing")
            return pd.concat([old, extension], ignore_index=True), sorted(set(suspended.get(symbol, [])) | set(halted))

        def dividends(self, symbol, start, end):
            old = [item for item in actions if item["symbol"] == symbol]
            exists = (base.root / (symbol + ".csv")).exists()
            since = previous if exists else start
            recent = [item for item in live.dividends(symbol, since, end)
                      if item["ex_date"] > previous or not exists]
            return old + recent

        def index_daily(self, symbol, start, end):
            return live.index_daily(symbol, start, end)

    root.mkdir(parents=True, exist_ok=False)
    prepared = root.parent / "prepared-snapshot"
    try:
        published = update_snapshot(output_root=prepared, base_dir=base.root,
            symbols=set(SYMBOLS) | set(REFERENCE_UNIVERSE) | set(INDEX_SYMBOLS),
            start=base.snapshot_manifest["start"], end=day, provider=IncrementalProvider())
        for path in published.iterdir():
            shutil.move(str(path), root / path.name)
        data = DataStore(root)
        quotes = {}
        for symbol in SYMBOLS:
            frame = data.load(symbol, as_of=day)
            if frame.empty or str(frame.index[-1].date()) != day:
                raise ValueError("target raw quote missing")
            row = frame.iloc[-1]
            quotes[symbol] = {"date": day, "close": float(row["close"]),
                "change_pct": 100 * (float(row["close"]) / float(row["preclose"]) - 1),
                "adjustment": "raw", "provider": "baostock"}
        audit = {"provider": "baostock", "fetched_at": datetime.now(SHANGHAI).isoformat(),
                 "base_snapshot": base.snapshot_manifest["snapshot_id"],
                 "snapshot": data.snapshot_manifest["snapshot_id"], "quotes": quotes, "failures": {}}
        put(root, "audit.json", audit)
        return audit
    except Exception as exc:
        put(root, "audit.json", {"provider": "baostock", "target_date": day,
            "failures": {"snapshot": {"type": type(exc).__name__, "category": _failure_category(exc)}}})
        raise LiveInputError({"snapshot": {"type": type(exc).__name__, "category": _failure_category(exc)}}) from exc
    finally:
        shutil.rmtree(prepared, ignore_errors=True)

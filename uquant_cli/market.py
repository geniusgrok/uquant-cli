"""Live input adapters; reuse production validation and never change frozen data."""
from __future__ import annotations

import importlib
import math
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


def refresh(root: Path, day: str, previous: str) -> dict:
    from uquant.data import DataStore
    from uquant.engine import INDEX_SYMBOLS, REFERENCE_UNIVERSE

    ak = importlib.import_module("akshare")
    root.mkdir(parents=True, exist_ok=False)
    store = DataStore(root)
    coverage, failures, quotes = {}, {}, {}

    def record_failure(key: str, exc: Exception) -> None:
        failures[key] = {"type": type(exc).__name__, "category": _failure_category(exc)}

    for symbol in sorted(set(SYMBOLS) | set(REFERENCE_UNIVERSE)):
        try:
            def load_stock():
                store.refresh_akshare([symbol], end=day)
                frame = store.load(symbol)
                if str(frame.index[-1].date()) != day or len(frame) < 2:
                    raise ValueError("target close or history missing")
                return frame
            frame = _retry_request(load_stock)
            coverage[symbol] = {"date": str(frame.index[-1].date()), "rows": len(frame),
                                "adjustment": "qfq"}
        except Exception as exc:
            record_failure(symbol, exc)

    mapping = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
               "收盘": "close", "成交量": "volume", "成交额": "amount"}
    for symbol in INDEX_SYMBOLS:
        try:
            def load_index():
                raw = ak.index_zh_a_hist(symbol=symbol[2:], period="daily", start_date="20000101",
                                         end_date=day.replace("-", ""))
                frame = raw.rename(columns=mapping)[list(mapping.values())].copy()
                frame["volume"] = pd.to_numeric(frame["volume"], errors="raise") * 100
                frame = DataStore._validate(frame, symbol)
                if str(frame.index[-1].date()) != day or len(frame) < 2:
                    raise ValueError("index target close or history missing")
                return frame
            frame = _retry_request(load_index)
            coverage[symbol] = {"date": str(frame.index[-1].date()), "rows": len(frame),
                                "adjustment": "raw"}
            frame.reset_index().to_csv(root / (symbol + ".csv"), index=False)
        except Exception as exc:
            record_failure(symbol, exc)

    for symbol in SYMBOLS:
        try:
            def load_raw_quote():
                raw = ak.stock_zh_a_hist(symbol=symbol[2:], period="daily", adjust="",
                                         start_date=previous.replace("-", ""),
                                         end_date=day.replace("-", ""))
                dates = pd.to_datetime(raw["日期"], errors="raise").dt.strftime("%Y-%m-%d")
                selected = raw.loc[dates == day]
                if len(selected) != 1:
                    raise ValueError("raw quote missing or duplicated")
                row = selected.iloc[0]
                close, change = float(row["收盘"]), row.get("涨跌幅")
                if not math.isfinite(close) or close <= 0:
                    raise ValueError("invalid raw close")
                change = float(change) if pd.notna(change) else None
                if change is not None and not math.isfinite(change):
                    raise ValueError("invalid daily change")
                return raw, {"date": day, "close": close, "change_pct": change,
                             "adjustment": "raw"}
            raw, quote = _retry_request(load_raw_quote)
            raw.to_csv(root / (symbol + ".raw.csv"), index=False)
            quotes[symbol] = quote
        except Exception as exc:
            record_failure(symbol + ":raw", exc)

    audit = {"provider": "AkShare/Eastmoney", "fetched_at": datetime.now(SHANGHAI).isoformat(),
             "coverage": coverage, "quotes": quotes, "failures": failures}
    put(root, "audit.json", audit)
    if failures:
        raise LiveInputError(failures)
    return audit

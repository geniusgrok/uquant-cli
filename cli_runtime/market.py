"""Live input adapters; reuse production validation and never change frozen data."""
from __future__ import annotations

import importlib
import math
from datetime import datetime, time
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
    previous = max(value for value in sessions if value < day)
    status = "READY" if day in sessions else "MARKET_CLOSED"
    if status == "READY" and local.time() < time(15):
        status = "MARKET_NOT_CLOSED"
    return {"target_date": day, "previous_session": previous, "status": status,
            "checked_at": local.isoformat(), "calendar_source": "akshare/Sina"}


def calendar_now(now: datetime) -> tuple[dict, list[str]]:
    ak = importlib.import_module("akshare")
    frame = ak.tool_trade_date_hist_sina()
    dates = pd.to_datetime(frame["trade_date"], errors="raise").dt.strftime("%Y-%m-%d").tolist()
    return session_context(dates, now), dates


def refresh(root: Path, day: str, previous: str) -> dict:
    from uquant.data import DataStore
    from uquant.engine import INDEX_SYMBOLS, REFERENCE_UNIVERSE

    ak = importlib.import_module("akshare")
    root.mkdir(parents=True, exist_ok=False)
    store = DataStore(root)
    coverage, failures, quotes = {}, {}, {}
    for symbol in sorted(set(SYMBOLS) | set(REFERENCE_UNIVERSE)):
        try:
            store.refresh_akshare([symbol], end=day)
            frame = store.load(symbol)
            coverage[symbol] = {"date": str(frame.index[-1].date()), "rows": len(frame),
                                "adjustment": "qfq"}
            if coverage[symbol]["date"] != day or len(frame) < 2:
                raise ValueError("target close or history missing")
        except Exception as exc:
            failures[symbol] = type(exc).__name__
    mapping = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
               "收盘": "close", "成交量": "volume", "成交额": "amount"}
    for symbol in INDEX_SYMBOLS:
        try:
            raw = ak.index_zh_a_hist(symbol=symbol[2:], period="daily", start_date="20000101",
                                     end_date=day.replace("-", ""))
            frame = raw.rename(columns=mapping)[list(mapping.values())].copy()
            frame["volume"] = pd.to_numeric(frame["volume"], errors="raise") * 100
            frame = DataStore._validate(frame, symbol)
            coverage[symbol] = {"date": str(frame.index[-1].date()), "rows": len(frame),
                                "adjustment": "raw"}
            if coverage[symbol]["date"] != day or len(frame) < 2:
                raise ValueError("index target close or history missing")
            frame.reset_index().to_csv(root / (symbol + ".csv"), index=False)
        except Exception as exc:
            failures[symbol] = type(exc).__name__
    for symbol in SYMBOLS:
        try:
            raw = ak.stock_zh_a_hist(symbol=symbol[2:], period="daily", adjust="",
                                     start_date=previous.replace("-", ""),
                                     end_date=day.replace("-", ""))
            raw.to_csv(root / (symbol + ".raw.csv"), index=False)
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
            quotes[symbol] = {"date": day, "close": close, "change_pct": change,
                              "adjustment": "raw"}
        except Exception as exc:
            failures[symbol + ":raw"] = type(exc).__name__
    audit = {"provider": "AkShare/Eastmoney", "fetched_at": datetime.now(SHANGHAI).isoformat(),
             "coverage": coverage, "quotes": quotes, "failures": failures}
    put(root, "audit.json", audit)
    if failures:
        raise ValueError("live input validation failed")
    return audit

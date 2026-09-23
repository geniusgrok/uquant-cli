"""多源实时行情适配，所有来源均经过生产数据校验。"""
from __future__ import annotations

import importlib
import math
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


def refresh(root: Path, day: str, previous: str, *, prior_audit: dict | None = None) -> dict:
    from uquant.engine import INDEX_SYMBOLS, REFERENCE_UNIVERSE

    ak = importlib.import_module("akshare")
    root.mkdir(parents=True, exist_ok=False)
    coverage, failures, quotes, attempts = {}, {}, {}, {}
    preferred = {"stock": None, "index": None, "raw": None}

    def stock_sources(symbol, start, adjust):
        return {
            "Eastmoney": lambda: ak.stock_zh_a_hist(
                symbol=symbol[2:], period="daily", adjust=adjust,
                start_date=start.replace("-", ""), end_date=day.replace("-", ""), timeout=10),
            "Sina": lambda: ak.stock_zh_a_daily(
                symbol=symbol, start_date=start.replace("-", ""),
                end_date=day.replace("-", ""), adjust=adjust),
            "Tencent": lambda: ak.stock_zh_a_hist_tx(
                symbol=symbol, start_date=start, end_date=day, adjust=adjust, timeout=10),
        }

    def fetch(key, symbol, providers, kind):
        attempts[key] = []
        saved = (prior_audit or {}).get("quotes" if kind == "raw" else "coverage", {})
        first = saved.get(symbol, {}).get("provider") or preferred[kind]
        frame, provider, estimated = _select_source(
            providers, first,
            lambda raw, name: _normalized(raw, symbol, day, name, index=kind == "index",
                                          minimum_rows=1 if kind == "raw" else 2),
            attempts[key])
        preferred[kind] = provider
        return frame, provider, estimated

    def failed(key, exc):
        failures[key] = {"type": type(exc).__name__, "category": _failure_category(exc)}

    for symbol in sorted((set(SYMBOLS) | set(REFERENCE_UNIVERSE)) - set(INDEX_SYMBOLS)):
        try:
            frame, provider, estimated = fetch(
                symbol, symbol, stock_sources(symbol, "2000-01-01", "qfq"), "stock")
            frame.reset_index().to_csv(root / (symbol + ".csv"), index=False)
            coverage[symbol] = {"date": day, "rows": len(frame), "adjustment": "qfq",
                                "provider": provider, "amount_estimated": estimated}
        except Exception as exc:
            failed(symbol, exc)

    for symbol in INDEX_SYMBOLS:
        try:
            providers = {
                "Eastmoney": lambda: ak.stock_zh_index_daily_em(
                    symbol="csi" + symbol[2:], start_date="20000101", end_date=day.replace("-", "")),
                "Sina": lambda: ak.stock_zh_index_daily(symbol=symbol),
                "Tencent": lambda: ak.stock_zh_index_daily_tx(
                    symbol=symbol, start_date="20000101", end_date=day.replace("-", "")),
            }
            frame, provider, estimated = fetch(symbol, symbol, providers, "index")
            frame.reset_index().to_csv(root / (symbol + ".csv"), index=False)
            coverage[symbol] = {"date": day, "rows": len(frame), "adjustment": "raw",
                                "provider": provider, "amount_estimated": estimated}
        except Exception as exc:
            failed(symbol, exc)

    for symbol in SYMBOLS:
        key = symbol + ":raw"
        try:
            frame, provider, estimated = fetch(
                key, symbol, stock_sources(symbol, previous, ""), "raw")
            row = frame.iloc[-1]
            change = row.get("涨跌幅")
            change = float(change) if pd.notna(change) else None
            if change is not None and not math.isfinite(change):
                raise ValueError("invalid daily change")
            # 备用源未给出交易所涨跌幅时保留缺失，避免除权日按原始前收错误推算。
            quotes[symbol] = {"date": day, "close": float(row["close"]), "change_pct": change,
                              "adjustment": "raw", "provider": provider}
            frame.reset_index().to_csv(root / (symbol + ".raw.csv"), index=False)
        except Exception as exc:
            failed(key, exc)

    audit = {"provider": "AkShare/multi-source", "fetched_at": datetime.now(SHANGHAI).isoformat(),
             "coverage": coverage, "quotes": quotes, "failures": failures, "attempts": attempts}
    put(root, "audit.json", audit)
    if failures:
        raise LiveInputError(failures)
    return audit

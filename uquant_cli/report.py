"""Projection of actual production output; unavailable is never an inferred signal."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .market import WATCHLIST

RISK_FIELDS = ("freeze_new_risk", "base_freeze_new_risk", "sentinel_freeze_new_risk",
               "target_gross_cap", "system_gross_cap", "severity", "reduction_level",
               "sentinel_causal_coverage_status", "sentinel_causal_active_families",
               "sector_guard_active")
ENTRY_FIELDS = ("entry", "pending_entry", "pullback_entry", "repair_entry", "entry_gate")
LIMIT_FIELDS = ("increase_block", "restore_block", "allocation_reason", "order_planning")


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "value"):
        return json_safe(value.value)
    if hasattr(value, "item"):
        return json_safe(value.item())
    return value


def signals(decision: dict, quotes: dict) -> dict:
    summary = decision["risk_summary"]
    trace = summary.get("core_allocation", {})
    rows = trace.get("symbols", {}) if trace.get("scope") == "FINAL_DECISION" else {}
    leaders = {row["symbol"]: row for row in summary.get("leader_ranking", [])}
    targets = {row["symbol"]: row for row in decision["targets"]}
    stocks = []
    for symbol, name in WATCHLIST:
        row = rows.get(symbol, {})
        stocks.append({"symbol": symbol, "name": name, "quote": quotes.get(symbol),
                       "trend_evidence": leaders.get(symbol),
                       "qualification": {key: row[key] for key in ENTRY_FIELDS if key in row} or None,
                       "risk_limits": {key: row[key] for key in LIMIT_FIELDS if key in row} or None,
                       "allocation": row or None, "target": targets.get(symbol),
                       "orders": [item for item in decision["pending_orders"] if item["symbol"] == symbol]})
    return json_safe({"market": {"opportunity": decision["opportunity"], "risk": decision["risk"],
                                 **{key: summary.get(key) for key in RISK_FIELDS}}, "stocks": stocks})


def compare(current: dict, previous: dict | None) -> dict:
    if previous is None or previous.get("status") not in {"COMPLETE", "PARTIAL"}:
        return {"status": "INCOMPARABLE", "reason": "没有可核验的前一交易日结果", "changes": []}
    for key in ("observer_id", "config_sha256", "economic_code_hash"):
        if not current.get(key) or previous.get(key) != current[key]:
            return {"status": "INCOMPARABLE", "reason": "比较口径变化：" + key, "changes": []}
    if previous.get("target_date") != current["previous_session"]:
        return {"status": "INCOMPARABLE", "reason": "基线不是前一交易日", "changes": []}
    old = {row["symbol"]: row for row in previous["signals"]["stocks"]}
    if set(old) != {symbol for symbol, _ in WATCHLIST}:
        return {"status": "INCOMPARABLE", "reason": "前一交易日股票池不完整", "changes": []}
    changes, unavailable = [], []

    def record(subject: str, field: str, before: Any, after: Any) -> None:
        if before is None or after is None:
            unavailable.append(subject + "/" + field)
        elif before != after:
            changes.append({"subject": subject, "field": field, "before": before, "after": after})

    for key, value in current["signals"]["market"].items():
        record("市场", key, previous["signals"]["market"].get(key), value)
    for row in current["signals"]["stocks"]:
        former = old[row["symbol"]]
        for field in ("qualification", "risk_limits", "trend_evidence"):
            record(row["name"], field, former.get(field), row.get(field))
        record(row["name"], "target_weight", (former["target"] or {}).get("weight"),
               (row["target"] or {}).get("weight"))
        action = lambda item: sorted((str(o["side"]), o.get("target_weight")) for o in item["orders"])
        record(row["name"], "action", action(former), action(row))
    return {"status": "COMPARABLE", "changes": changes, "unavailable": unavailable,
            "source_changed": previous["source_sha"] != current["source_sha"]}


def display(value: Any) -> str:
    text = "未提供" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
    for symbol, name in WATCHLIST:
        code = symbol[2:]
        pattern = rf"(?<![A-Za-z0-9])(?:sh|sz)?{code}(?!\d)(?!\s*{re.escape(name)})"
        text = re.sub(pattern, f"{code} {name}", text)
    return text.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def render(result: dict) -> str:
    status_names = {
        "COMPLETE": "成功", "PARTIAL": "部分结果", "REUSED": "已复用",
        "MARKET_CLOSED": "休市", "MARKET_NOT_CLOSED": "尚未收盘",
        "FAILED": "失败", "BOOTSTRAP_OR_PROCESS_FAILED": "启动或运行失败",
    }
    market_names = {
        "opportunity": "市场机会", "risk": "市场风险",
        "freeze_new_risk": "禁止增加风险", "base_freeze_new_risk": "基础风控禁止增加风险",
        "sentinel_freeze_new_risk": "风险哨兵禁止增加风险",
        "target_gross_cap": "建议总仓位上限", "system_gross_cap": "系统总仓位上限",
        "severity": "风险严重程度", "reduction_level": "减仓级别",
        "sentinel_causal_coverage_status": "风险覆盖状态",
        "sentinel_causal_active_families": "已触发风险类别", "sector_guard_active": "行业保护状态",
    }
    status = result["status"]
    target_date = result["target_date"]
    lines = [
        "# Uquant 13只标的盘后日报", "",
        f"目标交易日：{target_date}；行情截止日：{result.get('actual_market_date') or '未取得'}。",
        f"策略结果状态：**{status_names.get(status, status)}（{status}）**。",
        f"实际生产源码版本：{result.get('source_sha', '未取得')}。",
        f"[查看本次计算的运行记录]({result['run_url']})。",
        "执行口径：连续、不执行的观察账户，不代表真实账户持仓或成交。",
        f"运行时间：{result.get('started_at', '未取得')} → {result.get('finished_at', '未完成')}。",
        "", "## 重点变化", "",
    ]
    comparison = result.get("comparison") or {}
    previous = result.get("previous_session") or "未确认"
    lines.append(f"比较日期：{previous} → {target_date}。")
    if "signals" not in result:
        lines += ["不可比较：本次未产生完整生产信号。",
                  "本次未产生新的生产信号。", "", "## 结果限制", "",
                  "本次没有生成可展示的市场和逐只标的信号。未收盘、休市或行情校验失败时，不使用旧信号代替当日结果。"]
        if result.get("failure"):
            lines.append("失败阶段/类型：" + display(result["failure"]))
        return "\n".join(lines) + "\n"

    lines.insert(7, f"观察起点：{result.get('observer_start', '未取得')}；初始模拟现金：{result.get('initial_cash', '未提供')}元。")
    if comparison.get("status") != "COMPARABLE":
        lines.append("不可比较：" + display(comparison.get("reason")))
    elif not comparison.get("changes"):
        lines.append("已核验的可比较字段无变化；缺失字段不视为无变化。")
    else:
        for item in comparison["changes"]:
            lines.append(f"- **{display(item['subject'])} · {display(item['field'])}："
                         f"{display(item['before'])} → {display(item['after'])}**")
    if comparison.get("unavailable"):
        lines.append("未能比较的字段：" + "、".join(display(value) for value in comparison["unavailable"]))
    if comparison.get("source_changed"):
        lines.append("生产源码版本已变化，不能将全部信号变化归因于行情。")

    lines += ["", "## 市场状态与风险信号", "",
              "| 项目 | 生产输出 |", "|---|---|"]
    for key, value in result["signals"]["market"].items():
        lines.append(f"| {market_names.get(key, display(key))} | {display(value)} |")

    lines += ["", "## 全部13只标的信号总览", "",
              "| 代码及名称 | 收盘价 / 涨跌幅（%） | 机会与趋势证据 | 资格与入场 | 风险限制 | 行动与订单意图 | 目标仓位 |",
              "|---|---|---|---|---|---|---|"]
    for row in result["signals"]["stocks"]:
        quote = row.get("quote") or {}
        orders = [{key: order.get(key) for key in ("side", "target_weight", "signal_date")}
                  for order in row["orders"]]
        lines.append("| " + " | ".join([
            display(row["symbol"][2:] + " " + row["name"]),
            display(quote.get("close")) + " / " + display(quote.get("change_pct")),
            display(row.get("trend_evidence")), display(row.get("qualification")),
            display(row.get("risk_limits")),
            display(orders) if orders else "未生成订单意图",
            display((row.get("target") or {}).get("weight")),
        ]) + " |")

    lines += ["", "## 逐只标的信号与风险明细", ""]
    details = [
        ("收盘行情", lambda row: row.get("quote")),
        ("机会与趋势证据", lambda row: row.get("trend_evidence")),
        ("资格与入场条件", lambda row: row.get("qualification")),
        ("风险限制", lambda row: row.get("risk_limits")),
        ("生产分配输出", lambda row: row.get("allocation")),
        ("目标信号", lambda row: row.get("target")),
        ("订单意图", lambda row: [
            {key: order.get(key) for key in ("side", "target_weight", "signal_date")}
            for order in row["orders"]
        ]),
    ]
    for row in result["signals"]["stocks"]:
        lines += [f"### {display(row['symbol'][2:] + ' ' + row['name'])}", "",
                  "| 字段 | 生产输出 |", "|---|---|"]
        for label, get_value in details:
            lines.append(f"| {label} | {display(get_value(row))} |")

    lines += ["", "## 阅读说明", "",
              "未提供的字段不解释为零、无风险或无变化。风险等级、风险限制和仓位上限须结合阅读。",
              "零数量、受阻信号或订单意图不代表可执行买入或已成交。",
              "盘后信号仅供下一可交易日人工核对；本仓库不连接券商、不自动下单。"]
    return "\n".join(lines) + "\n"

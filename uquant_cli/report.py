"""Projection of actual production output; unavailable is never an inferred signal."""
from __future__ import annotations

import json
import math
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
        # IDs and dates naturally change; compare the actual action and weight instead.
        action = lambda item: sorted((str(o["side"]), o.get("target_weight")) for o in item["orders"])
        record(row["name"], "action", action(former), action(row))
    return {"status": "COMPARABLE", "changes": changes, "unavailable": unavailable,
            "source_changed": previous["source_sha"] != current["source_sha"]}


def display(value: Any) -> str:
    text = "未提供" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def render(result: dict, production_report: str = "") -> str:
    lines = [f"# Uquant 13标的盘后日报 — {result['target_date']}", "",
             f"状态：{result['status']}；实际行情日：{result.get('actual_market_date') or '未取得'}", 
             f"前一交易日：{result.get('previous_session') or '未确认'}",
             f"生产源码：`{result.get('source_sha', '未取得')}`；[Actions Run]({result['run_url']})", "",
             f"运行启动：{result['started_at']}；本次处理完成：{result.get('finished_at', '未完成')}"]
    if "signals" not in result:
        lines += ["", "本次未产生新的生产信号。原因：" + result["status"],
                  "昨日 → 今日：不可比较；不得将旧数据或盘中数据标为今日收盘。"]
        if result.get("failure"):
            lines.append("失败阶段/类型：" + display(result["failure"]))
        return "\n".join(lines) + "\n"
    lines += ["", "口径：连续、不执行的观察账户；没有使用真实账户，也没有模拟成交。",
              f"观察起点：{result['observer_start']}；初始模拟现金：{result['initial_cash']}元。",
              "## 重点变化", ""]
    comp = result["comparison"]
    if comp["status"] != "COMPARABLE":
        lines.append("**不可比较：" + comp["reason"] + "**")
    elif not comp["changes"]:
        lines.append("已核验的可比较字段无变化；不涵盖缺失字段。")
    else:
        for item in comp["changes"]:
            lines.append(f"- **{item['subject']} / {item['field']}：{display(item['before'])} → {display(item['after'])}**")
    if comp.get("unavailable"):
        lines.append("不可比较的字段：" + "、".join(comp["unavailable"]))
    if comp.get("source_changed"):
        lines.append("源码提交已变化；当前经济代码和配置指纹相同，原始源码身份分别保留。")
    lines += ["", "## 市场与风险", "", "字段 | 实际生产输出", "--- | ---"]
    lines += [key + " | " + display(value) for key, value in result["signals"]["market"].items()]
    lines += ["", "## 全部13只股票", "",
              "代码及名称 | 收盘价 / 涨跌幅(%) | 机会/趋势证据 | 资格 | 风险限制 | 行动/订单意图 | 目标仓位",
              "--- | --- | --- | --- | --- | --- | ---"]
    for row in result["signals"]["stocks"]:
        quote = row["quote"] or {}
        orders = [{key: o.get(key) for key in ("side", "target_weight", "signal_date")} for o in row["orders"]]
        lines.append(" | ".join([row["symbol"][2:] + " " + row["name"],
            display(quote.get("close")) + " / " + display(quote.get("change_pct")),
            display(row["trend_evidence"]), display(row["qualification"]), display(row["risk_limits"]),
            display(orders) if orders else "未生成订单意图（不等于持有或允许买入）",
            display((row["target"] or {}).get("weight"))]))
    lines += ["", "目标仓位以0—1表示。保留意图的signal_date，区分历史未执行意图与今日信号。",
              "盘后决策仅供下一可交易日人工核对，不代表成交。", "",
              "## 生产系统完整日报", "", production_report]
    return "\n".join(lines) + "\n"

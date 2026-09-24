"""One production decision per trading day; all generated files belong to uquant-cli."""
from __future__ import annotations

import argparse
import contextlib
import math
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .market import LiveInputError, SHANGHAI, SYMBOLS, calendar_now, refresh, session_context
from .report import compare, json_safe, render, signals
from .store import GitStore, identity, open_store, put, read, verify

OBSERVER = "uquant-13-continuous-no-execution-v1"


def calendar_context(root: Path, now: datetime) -> tuple[dict, list[str]]:
    try:
        return calendar_now(now)
    except Exception:
        # Reuse only a calendar whose bytes are bound to the last successful receipt.
        if not (root / "latest.json").exists():
            raise
        receipt = read(root, "latest.json")
        verify(root, receipt["files"])
        path = str(Path(receipt["result_path"]).parent / "calendar.json")
        if path not in receipt["files"]:
            raise
        dates = read(root, path)
        context = session_context(dates, now)
        context["calendar_source"] = "已核验的前次交易日历"
        return context, dates


def next_unfinished_session(root: Path, context: dict, dates: list[str]) -> dict:
    """Resume the oldest missing session before today's decision."""
    if not (root / "latest.json").exists():
        claims = sorted((root / "claims").glob("*.json"))
        if len(claims) == 1:
            claim = read(root, claims[0].relative_to(root).as_posix())
            day = claim.get("target_date")
            earlier = [value for value in dates if day and value < day]
            if (claim.get("status") == "STARTED" and day in dates
                    and day <= context["target_date"] and earlier
                    and claim.get("previous_session") == max(earlier)):
                return {**context, "target_date": day, "previous_session": max(earlier),
                        "calendar_target_date": context["target_date"],
                        "selection_reason": "优先补做未完成的首次交易日"}
        return context
    receipt = read(root, "latest.json")
    verify(root, receipt["files"])
    previous = read(root, receipt["result_path"])["target_date"]
    if not dates or previous < min(dates):
        raise RuntimeError("trading calendar no longer covers the account gap")
    pending = [day for day in sorted(set(dates)) if previous < day <= context["target_date"]]
    if len(pending) <= 1:
        return context
    return {**context, "target_date": pending[0], "previous_session": previous,
            "calendar_target_date": context["target_date"],
            "selection_reason": "优先补做缺失的最早交易日"}


def prior(root: Path, context: dict) -> dict | None:
    if not (root / "latest.json").exists():
        claims = sorted((root / "claims").glob("*.json"))
        if claims:
            if len(claims) != 1:
                raise RuntimeError("unreconciled production claim")
            claim = read(root, claims[0].relative_to(root).as_posix())
            if not (claims[0].name == str(context.get("target_date", "")) + ".json"
                    and claim.get("status") == "STARTED"
                    and claim.get("target_date") == context.get("target_date")
                    and claim.get("previous_session") == context.get("previous_session")
                    and str(claim.get("run_url", "")).startswith(
                        "https://github.com/geniusgrok/uquant-cli/actions/runs/")):
                raise RuntimeError("unreconciled production claim")
        previous_state = [root / "state/account.json", root / "account.json"]
        if (any(path.exists() for path in previous_state)
                or list((root / "reports").glob("*/result.json"))
                or list((root / ".state").glob("account*"))
                or list((root / ".state").glob("latest*"))):
            raise RuntimeError("missing receipt; refusing account reset")
        return None
    receipt = read(root, "latest.json")
    verify(root, receipt["files"])
    result = read(root, receipt["result_path"])
    if result.get("observer_id") != OBSERVER:
        raise RuntimeError("observer identity changed")
    if result["target_date"] not in {context["target_date"], context["previous_session"]}:
        raise RuntimeError("observer trading-session gap")
    for path in (root / "claims").glob("*.json"):
        claim = read(root, path.relative_to(root).as_posix())
        if claim.get("status") == "COMPLETE":
            continue
        recoverable = (path.name == context["target_date"] + ".json"
            and claim.get("status") == "STARTED"
            and claim.get("target_date") == context["target_date"]
            and claim.get("previous_session") == context["previous_session"]
            and result["target_date"] == context["previous_session"]
            and str(claim.get("run_url", "")).startswith(
                "https://github.com/geniusgrok/uquant-cli/actions/runs/")
            and not (root / "reports" / context["target_date"] / "result.json").exists())
        if not recoverable:
            raise RuntimeError("unreconciled production claim")
    return result


def compute(root: Path, work: Path, metadata: dict, previous: dict | None) -> dict:
    from uquant.account import load_account, migrate_code_identity, save_account
    from uquant.config import DEFAULT_CONFIG, config_fingerprint
    from uquant.engine import ProductionEngine, code_fingerprint
    from uquant.types import AccountState

    day = metadata["target_date"]
    engine = ProductionEngine(work / "inputs")
    if previous is None:
        account = AccountState.empty(DEFAULT_CONFIG.initial_cash)
        account.account_migrations.append({"migration_type": "configuration_binding",
            "effective_config_sha256": config_fingerprint(DEFAULT_CONFIG)})
    else:
        account = load_account(root / "state/account.json")
        if previous["config_sha256"] != config_fingerprint(DEFAULT_CONFIG):
            raise RuntimeError("observer configuration changed")
    # Only this explicitly non-executing observer state may ever be published publicly.
    if account.positions or getattr(account, "broker_as_of", ""):
        raise RuntimeError("real or executed account input is not authorized for public publication")
    current_code_hash = code_fingerprint() if previous is not None else None
    if previous is not None and account.code_hash != current_code_hash:
        # This observer follows reviewed production main. Preserve every economic state field.
        account = migrate_code_identity(root / "state/account.json", work / "account_before.json",
            new_code_hash=current_code_hash, acknowledge_code_change=True)
    else:
        save_account(account, work / "account_before.json")
    decision = engine.decide(symbols=SYMBOLS, as_of=day, account=account)
    account.pending_orders = list(decision.pending_orders)
    raw = asdict(decision)
    for item in (*raw["targets"], *raw["pending_orders"]):
        for field in ("weight", "target_weight"):
            if field in item and not math.isfinite(float(item[field])):
                raise ValueError("non-finite production control value")
    save_account(account, work / "account_after.json")
    raw = json_safe(raw)
    coverage = raw["risk_summary"].get("sentinel_causal_coverage_status")
    result = {**metadata, "schema": "uquant-cli.daily.v1", "observer_id": OBSERVER,
        "status": "COMPLETE" if coverage == "READY" else "PARTIAL", "actual_market_date": day,
        "observer_start": previous["observer_start"] if previous else day,
        "initial_cash": previous["initial_cash"] if previous else DEFAULT_CONFIG.initial_cash,
        "economic_code_hash": account.code_hash, "config_sha256": config_fingerprint(DEFAULT_CONFIG),
        "data_manifest": engine.data.manifest(account.data_hash_symbols, source="live-akshare", as_of=day).to_dict(),
        "finished_at": datetime.now(SHANGHAI).isoformat(),
        "signals": signals(raw, read(work, "inputs/audit.json")["quotes"])}
    result["comparison"] = compare(result, previous)
    put(work, "decision.json", raw)
    put(work, "result.json", result)
    (work / "report.md").write_text(render(result), encoding="utf-8")
    return result


def run_once(store: GitStore, work: Path, metadata: dict) -> dict:
    day = metadata["target_date"]
    if metadata["status"] != "READY":
        result = {**metadata, "actual_market_date": None, "signals_generated": False,
                  "finished_at": datetime.now(SHANGHAI).isoformat()}
        put(work, "status.json", result)
        report_name = f"reports/{day}.md"
        report_path = store.root / report_name
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render(result), encoding="utf-8")
        store.publish([report_name], "Publish daily status report " + day)
        return result
    previous = prior(store.root, metadata)
    if previous is not None and previous["target_date"] == day:
        result = {**metadata, "status": "REUSED", "actual_market_date": day,
                  "source_sha": previous["source_sha"], "checked_source_sha": metadata["source_sha"],
                  "original_run_url": previous["run_url"],
                  "finished_at": datetime.now(SHANGHAI).isoformat()}
        put(work, "status.json", result)
        return result
    saved_sources = read(store.root, "inputs/audit.json") if previous is not None else None
    refresh(work / "inputs", day, metadata["previous_session"], prior_audit=saved_sources,
            prior_root=store.root / "inputs" if previous is not None else None)
    claim_path = f"claims/{day}.json"
    claim = {**metadata, "status": "STARTED"}
    if (store.root / claim_path).exists():
        claim["recovered_from_run_url"] = read(store.root, claim_path)["run_url"]
    put(store.root, claim_path, claim)
    # A conflicting or ambiguous claim write raises before the engine is called.
    store.publish([claim_path], "Claim production decision " + day)
    result = compute(store.root, work, metadata, previous)
    paths = []
    for original in sorted(work.rglob("*")):
        if original.is_file():
            rel = original.relative_to(work).as_posix()
            destination = rel if rel.startswith("inputs/") else (f"reports/{day}.md" if rel == "report.md" else f"reports/{day}/{rel}")
            target = store.root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            paths.append(destination)
    (store.root / "state").mkdir(exist_ok=True)
    shutil.copyfile(work / "account_after.json", store.root / "state/account.json")
    paths.append("state/account.json")
    put(store.root, claim_path, {**claim, "status": "COMPLETE",
                                "decision_digest": read(work, "decision.json")["decision_digest"]})
    paths.append(claim_path)
    put(store.root, "latest.json", {"result_path": f"reports/{day}/result.json",
        "files": {name: identity(store.root / name) for name in paths}})
    store.publish([*paths, "latest.json"], "Publish production report " + day)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    work = args.root / "publishable"
    work.mkdir(parents=True, exist_ok=False)
    metadata = {"target_date": datetime.now(SHANGHAI).date().isoformat(),
        "run_url": f"https://github.com/geniusgrok/uquant-cli/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        "source_sha": os.environ["UQUANT_SOURCE_SHA"], "runner_sha": os.environ["GITHUB_SHA"],
        "started_at": os.environ["UQUANT_STARTED_AT"]}
    stage = "STATE"
    try:
        with open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
            store = open_store(args.root / "state-checkout")
            stage = "CALENDAR"
            context, dates = calendar_context(store.root, datetime.now(SHANGHAI))
            put(work, "calendar.json", dates)
            metadata.update(context)
            stage = "STATE_AND_DECISION"
            metadata.update(next_unfinished_session(store.root, context, dates))
            result = run_once(store, work, metadata)
        put(work, "status.json", {key: result[key] for key in
            ("status", "target_date", "source_sha", "run_url", "started_at", "finished_at")})
        return 0
    except Exception as exc:
        # Do not publish traceback source lines, arbitrary exception text, or process environments.
        known_failures = {"historical data prefix differs from account state": "DATA_PREFIX_CHANGED",
                          "production code hash differs from account state": "CODE_IDENTITY_CHANGED",
                          "unreconciled production claim": "CLAIM_UNRECONCILED"}
        result = {**metadata, "status": "FAILED", "actual_market_date": None,
                  "failure": {"stage": getattr(exc, "stage", stage), "type": type(exc).__name__,
                              "reason": ("MARKET_INPUT_UNAVAILABLE" if isinstance(exc, LiveInputError)
                                         else known_failures.get(str(exc), "UNCLASSIFIED"))},
                  "finished_at": datetime.now(SHANGHAI).isoformat()}
        if getattr(exc, "safe_summary", None):
            result["failure_summary"] = exc.safe_summary
        put(work, "status.json", result)
        if not (work / "report.md").exists():
            (work / "report.md").write_text(render(result), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

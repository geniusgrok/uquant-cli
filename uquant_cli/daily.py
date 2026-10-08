"""One production decision per trading day; all generated files belong to uquant-cli."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .market import LiveInputError, SHANGHAI, SYMBOLS, calendar_now, refresh, session_context
from .report import compare, json_safe, render, signals
from .store import GitStore, identity, open_store, put, read, verify

OBSERVER = "uquant-13-continuous-no-execution-v1"
LEGACY_ACTION_MANIFEST_PRODUCER = "964954df5373c3e17a1fe041f2339f2342609125"
CURRENT_LABEL_CONFIG_SHA256 = "0b58e22be7bd9f25bd78ccfff9252b0a64ca329ebfeb2eb2b85095373908247d"


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


def rebind_action_manifest_identity(root: Path, account, current_data, previous: dict) -> None:
    """Migrate the one verified empty observer's pre-action manifest contract.

    Historical price/action facts must be identical. This does not reconcile
    a changed historical input or authorize an executed account migration.
    """
    from uquant.account import economic_state_sha256
    from uquant.data import DataStore, RAW_ADJUSTMENT

    new = current_data.manifest(account.data_hash_symbols, as_of=account.data_hash_as_of)
    if new.digest == account.data_hash:
        return
    if (previous.get("observer_id") != OBSERVER
            or previous.get("source_sha") != LEGACY_ACTION_MANIFEST_PRODUCER
            or previous.get("target_date") != account.data_hash_as_of
            or account.positions or account.pending_orders or account.fills or account.order_ledger
            or account.broker_as_of or account.broker_binding or account.broker_snapshots
            or account.external_cash_flows or account.receivables or account.dividend_tax_lots
            or account.cash != account.initial_cash):
        raise RuntimeError("observer action manifest requires explicit reconciliation")
    receipt = read(root, "latest.json")
    required_receipt_files = {"state/account.json", receipt["result_path"], "inputs/DATA_MANIFEST.json",
        "inputs/CORPORATE_ACTIONS.json", *(f"inputs/{symbol}.csv" for symbol in account.data_hash_symbols)}
    if not required_receipt_files <= set(receipt["files"]):
        raise RuntimeError("observer action manifest receipt lacks originals")
    verify(root, receipt["files"])
    if read(root, receipt["result_path"]) != previous:
        raise RuntimeError("observer action manifest receipt differs")
    old_data = DataStore(root / "inputs")
    old = old_data.manifest(account.data_hash_symbols, as_of=account.data_hash_as_of)
    price_files = dict(old.files)
    action_hash = price_files.pop("CORPORATE_ACTIONS.json", None)
    legacy_digest = hashlib.sha256(json.dumps(price_files, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
    if (old_data.adjustment != RAW_ADJUSTMENT or current_data.adjustment != RAW_ADJUSTMENT
            or not action_hash or legacy_digest != account.data_hash or old.files != new.files):
        raise RuntimeError("observer action manifest facts differ")
    # Compare the complete recorded old action inventory with the new snapshot's
    # preceding prefix, including symbols beyond an accidentally narrower binding.
    old_actions = read(old_data.root, "CORPORATE_ACTIONS.json")
    new_actions = [event for event in read(current_data.root, "CORPORATE_ACTIONS.json")
                   if event["ex_date"] <= account.data_hash_as_of]
    if (any(event["ex_date"] > account.data_hash_as_of for event in old_actions)
            or sorted(old_actions, key=lambda event: event["event_id"]) != sorted(
                new_actions, key=lambda event: event["event_id"])):
        raise RuntimeError("observer action manifest facts differ")
    before_payload = account.to_dict()
    before, old_hash = economic_state_sha256(account), account.data_hash
    account.data_hash = new.digest
    after_payload = account.to_dict()
    after_payload["data_hash"] = old_hash
    neutral_after = economic_state_sha256(replace(account, data_hash=old_hash))
    if after_payload != before_payload or neutral_after != before:
        account.data_hash = old_hash
        raise RuntimeError("observer action manifest migration changed economic state")
    account.account_migrations.append({"migration_type": "visible_action_manifest_identity",
        "as_of": account.data_hash_as_of, "from_data_hash": old_hash, "to_data_hash": new.digest,
        "verified_previous_source_sha": previous["source_sha"], "visible_action_sha256": action_hash,
        "economic_state_sha256_before": before, "economic_state_sha256_after": economic_state_sha256(account),
        "previous_identity_economic_state_sha256_after": neutral_after,
        "allowed_identity_field_change": "data_hash"})


def rebind_config_labels(root: Path, account, previous: dict) -> None:
    """Bind fixed metadata labels added to the unchanged verified policy."""
    from uquant.account import economic_state_sha256
    from uquant.config import DEFAULT_CONFIG, config_fingerprint

    current_hash = config_fingerprint(DEFAULT_CONFIG)
    labels = {"economic_core": "legacy_lots", "remove_relative_strength": True}
    legacy_payload = DEFAULT_CONFIG.to_dict()
    if (current_hash != CURRENT_LABEL_CONFIG_SHA256
            or any(legacy_payload.pop(name, None) != value for name, value in labels.items())):
        raise RuntimeError("observer configuration requires explicit reconciliation")
    if legacy_payload.get("risk_sentinel_causal_confirmation_enabled") is False:
        legacy_payload.pop("risk_sentinel_causal_confirmation_enabled")
    legacy_hash = hashlib.sha256(json.dumps(legacy_payload, allow_nan=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    config_events = [event for event in account.account_migrations
                     if event.get("migration_type") in {"configuration_binding", "configuration_rebind"}]
    recorded_hash = (config_events[-1].get("effective_config_sha256")
                     or config_events[-1].get("to_config_sha256")) if config_events else None
    if (previous.get("observer_id") != OBSERVER
            or previous.get("source_sha") != LEGACY_ACTION_MANIFEST_PRODUCER
            or previous.get("config_sha256") != legacy_hash or recorded_hash != legacy_hash
            or previous.get("target_date") != account.data_hash_as_of
            or account.positions or account.pending_orders or account.fills or account.order_ledger
            or account.broker_as_of or account.broker_binding or account.broker_snapshots
            or account.external_cash_flows or account.receivables or account.dividend_tax_lots
            or account.cash != account.initial_cash):
        raise RuntimeError("observer configuration requires explicit reconciliation")
    receipt = read(root, "latest.json")
    if not {"state/account.json", receipt["result_path"]} <= set(receipt["files"]):
        raise RuntimeError("observer configuration receipt lacks originals")
    verify(root, receipt["files"])
    if read(root, receipt["result_path"]) != previous:
        raise RuntimeError("observer configuration receipt differs")
    before, before_payload = economic_state_sha256(account), account.to_dict()
    account.account_migrations.append({"migration_type": "configuration_binding",
        "effective_config_sha256": current_hash, "from_config_sha256": legacy_hash,
        "verified_previous_source_sha": previous["source_sha"], "metadata_labels_added": labels,
        "economic_state_sha256_before": before, "economic_state_sha256_after": before})
    after_payload = account.to_dict()
    after_payload["account_migrations"] = before_payload["account_migrations"]
    if after_payload != before_payload or economic_state_sha256(account) != before:
        account.account_migrations.pop()
        raise RuntimeError("observer configuration migration changed economic state")


def compute(root: Path, work: Path, metadata: dict, previous: dict | None) -> dict:
    from uquant.account import (UnsupportedAccountSchemaError, economic_state_sha256, load_account,
                                migrate_account_schema, migrate_code_identity, save_account)
    from uquant.config import DEFAULT_CONFIG, config_fingerprint
    from uquant.data import DataStore, LEGACY_ADJUSTMENT, RAW_ADJUSTMENT
    from uquant.engine import ProductionEngine, code_fingerprint
    from uquant.models import AccountState

    day = metadata["target_date"]
    engine = ProductionEngine(work / "inputs")
    account_source = root / "state/account.json"
    if previous is None:
        account = AccountState.empty(DEFAULT_CONFIG.initial_cash)
        account.account_migrations.append({"migration_type": "configuration_binding",
            "effective_config_sha256": config_fingerprint(DEFAULT_CONFIG)})
    else:
        try:
            account = load_account(account_source)
        except UnsupportedAccountSchemaError:
            # Migrate a copy: the verified previous receipt remains untouched until publication.
            migrated = work / "account_before.json"
            shutil.copyfile(account_source, migrated)
            try:
                account = migrate_account_schema(migrated, code_hash=code_fingerprint())
            finally:
                migrated.with_name(migrated.name + ".lock").unlink(missing_ok=True)
            account_source = migrated
    # Only this explicitly non-executing observer state may ever be published publicly.
    if account.positions or getattr(account, "broker_as_of", ""):
        raise RuntimeError("real or executed account input is not authorized for public publication")
    current_code_hash = code_fingerprint() if previous is not None else None
    if previous is not None and account.code_hash != current_code_hash:
        # This observer follows reviewed production main. Preserve every economic state field.
        account = migrate_code_identity(account_source, work / "account_before.json",
            new_code_hash=current_code_hash, acknowledge_code_change=True)
    if previous is not None and previous["config_sha256"] != config_fingerprint(DEFAULT_CONFIG):
        rebind_config_labels(root, account, previous)
    if previous is not None and account.data_hash:
        old_basis = DataStore(root / "inputs").adjustment
        if old_basis != engine.data.adjustment:
            if (old_basis != LEGACY_ADJUSTMENT or engine.data.adjustment != RAW_ADJUSTMENT
                    or account.positions or account.pending_orders or account.fills or account.order_ledger
                    or account.data_hash_as_of != previous["target_date"]):
                raise RuntimeError("observer data basis requires explicit reconciliation")
            old_digest = account.data_hash
            account.data_hash = engine.data.manifest(
                account.data_hash_symbols, as_of=account.data_hash_as_of).digest
            account.account_migrations.append({"migration_type": "raw_data_basis_rebind",
                "as_of": account.data_hash_as_of, "from_data_hash": old_digest,
                "to_data_hash": account.data_hash,
                "source_snapshot": engine.data.snapshot_manifest["snapshot_id"]})
        elif old_basis == RAW_ADJUSTMENT and account.data_hash_as_of and account.data_hash_symbols:
            rebind_action_manifest_identity(root, account, engine.data, previous)
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
        "data_manifest": engine.data.manifest(account.data_hash_symbols, source="live-baostock", as_of=day).to_dict(),
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
        public_location = None
        trace = exc.__traceback__
        while trace:
            source = Path(trace.tb_frame.f_code.co_filename).resolve()
            if source.parent == Path(__file__).resolve().parent:
                public_location = f"{source.name}:{trace.tb_lineno}"
            trace = trace.tb_next
        known_failures = {"historical data prefix differs from account state": "DATA_PREFIX_CHANGED",
                          "production code hash differs from account state": "CODE_IDENTITY_CHANGED",
                          "account configuration identity differs from the selected economic core": "CONFIG_IDENTITY_CHANGED",
                          "observer configuration requires explicit reconciliation": "CONFIG_RECONCILIATION_REQUIRED",
                          "observer action manifest requires explicit reconciliation": "MANIFEST_IDENTITY_RECONCILIATION_REQUIRED",
                          "observer action manifest facts differ": "HISTORICAL_ACTION_OR_PRICE_FACTS_CHANGED",
                          "unreconciled production claim": "CLAIM_UNRECONCILED"}
        result = {**metadata, "status": "FAILED", "actual_market_date": None,
                  "failure": {"stage": getattr(exc, "stage", stage), "type": type(exc).__name__,
                              "reason": ("MARKET_INPUT_UNAVAILABLE" if isinstance(exc, LiveInputError)
                                         else known_failures.get(str(exc), "UNCLASSIFIED")),
                              "reason_code": (exc.reason_code if isinstance(exc, LiveInputError)
                                              else known_failures.get(str(exc), "UNCLASSIFIED")),
                              "public_location": public_location},
                  "finished_at": datetime.now(SHANGHAI).isoformat()}
        if getattr(exc, "safe_summary", None):
            result["failure_summary"] = exc.safe_summary
        put(work, "status.json", result)
        if not (work / "report.md").exists():
            (work / "report.md").write_text(render(result), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

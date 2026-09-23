"""Actual unchanged production engine checks; fixture results are not live signals."""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from . import daily
from .market import SYMBOLS
from .store import GitStore, identity, put, read, verify


def check(source: Path, root: Path) -> dict:
    from uquant.data import DataStore
    from uquant.engine import INDEX_SYMBOLS, REFERENCE_UNIVERSE

    # The upstream frozen research pool omits 002384. Keep all thirteen test
    # subjects: add a disclosed synthetic fixture only in this isolated test.
    # Never fetch or overwrite a production input through this fixture path.
    fixture = root / "fixture"
    shutil.copytree(source / "data/frozen", fixture)
    members = set(SYMBOLS) | set(REFERENCE_UNIVERSE) | set(INDEX_SYMBOLS)
    missing = {symbol for symbol in members if not (fixture / (symbol + ".csv")).exists()
               and not (fixture / (symbol[2:] + ".csv")).exists()}
    if missing - {"sz002384"}:
        raise ValueError("unexpected frozen fixture coverage gap")
    if missing:
        frame = DataStore(fixture).load("sh000300")
        frame.loc[:, ["open", "high", "low", "close"]] = 20.0
        frame.loc[:, "volume"] = 1_000_000.0
        frame.loc[:, "amount"] = 20_000_000.0
        frame.reset_index().to_csv(fixture / "sz002384.csv", index=False)
    data = DataStore(fixture)
    days = [str(value.date()) for value in data.common_sessions(members, "2026-01-01", "2026-08-05")[-3:]]
    remote = root / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    store = GitStore(root / "state", str(remote))
    calls = []

    def fixture_refresh(destination: Path, day: str, previous: str) -> dict:
        calls.append(day)
        destination.mkdir()
        for symbol in members:
            data.load(symbol, as_of=day).reset_index().to_csv(destination / (symbol + ".csv"), index=False)
        audit = {"fixture_only": True, "quotes": {symbol: {"date": day,
            "close": float(data.load(symbol, as_of=day).iloc[-1]["close"])} for symbol in SYMBOLS}}
        put(destination, "audit.json", audit)
        return audit

    outcomes = []
    with patch.object(daily, "refresh", fixture_refresh):
        for previous, day in zip(days[:-1], days[1:], strict=True):
            work = root / day
            work.mkdir()
            metadata = {"target_date": day, "previous_session": previous, "status": "READY",
                        "source_sha": os.environ["UQUANT_SOURCE_SHA"], "runner_sha": os.environ["GITHUB_SHA"],
                        "run_url": "fixture-validation-only", "started_at": day + "T17:01:00+08:00"}
            outcome = daily.run_once(store, work, metadata)
            assert outcome["status"] in {"COMPLETE", "PARTIAL"}
            assert tuple(row["symbol"] for row in outcome["signals"]["stocks"]) == SYMBOLS
            assert daily.prior(store.root, metadata)["target_date"] == day
            verify(store.root, read(store.root, "latest.json")["files"])
            outcomes.append({"day": day, "status": outcome["status"],
                             "comparison": outcome["comparison"]["status"]})
        assert daily.run_once(store, work, metadata)["status"] == "REUSED"
    assert calls == days[1:]
    assert outcomes[1]["comparison"] == "COMPARABLE"
    loaded = GitStore(root / "readback", str(remote))
    verify(loaded.root, read(loaded.root, "latest.json")["files"])
    assert (loaded.root / "state/account.json").read_bytes() == (store.root / "state/account.json").read_bytes()
    return {"status": "PASS", "fixture_only": True, "synthetic_fixture_symbols": sorted(missing),
            "production_decisions": 2,
            "duplicate_decisions": 0, "sessions": outcomes,
            "account_identity": identity(loaded.root / "state/account.json"),
            "source_sha": os.environ["UQUANT_SOURCE_SHA"], "runner_sha": os.environ["GITHUB_SHA"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory() as directory, open(os.devnull, "w") as silent:
            with contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
                result = check(args.source, Path(directory))
        code = 0
    except Exception as exc:
        # Frame identities help diagnosis without publishing any private source line or message.
        frames, trace = [], exc.__traceback__
        while trace:
            frames.append({"module": Path(trace.tb_frame.f_code.co_filename).name,
                           "function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        result = {"status": "FAIL", "type": type(exc).__name__, "frames": frames, "fixture_only": True,
                  "source_sha": os.environ["UQUANT_SOURCE_SHA"]}
        code = 1
    put(args.output.parent, args.output.name, result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

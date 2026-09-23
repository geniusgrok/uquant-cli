"""Preserve allowlisted runtime outputs, never the private source checkout or raw logs."""
from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

from .store import identity, open_store, path_in, put, read


def preserve(root: Path) -> str:
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("invalid run identity")
    store = open_store(root / "archive-checkout")
    prefix = f"runs/{run_id}-{attempt}"
    candidate = root / "operation/publishable"
    status = read(candidate, "status.json") if (candidate / "status.json").exists() else {
        "status": "BOOTSTRAP_OR_PROCESS_FAILED", "run_url": f"https://github.com/geniusgrok/uquant-cli/actions/runs/{run_id}"}
    files = [(root / "events.json", "events.json")]
    allowed_files = {"status.json", "calendar.json", "report.md", "result.json",
                     "decision.json", "account_before.json", "account_after.json"}
    if candidate.exists():
        for path in sorted(candidate.rglob("*")):
            if path.is_file():
                relative = path.relative_to(candidate).as_posix()
                if status["status"] in {"COMPLETE", "PARTIAL", "REUSED"} and relative != "status.json":
                    continue  # Already preserved atomically under reports/ and inputs/.
                is_input = (relative == "inputs/audit.json" or
                            re.fullmatch(r"inputs/(?:sh|sz)[0-9]{6}(?:\.raw)?\.csv", relative))
                if relative not in allowed_files and not is_input:
                    raise ValueError("unapproved runtime output path")
                files.append((path, relative))
    names = []
    for original, relative in files:
        if not original.is_file():
            continue
        path_in(original.parent, original.name)
        name = prefix + "/" + relative
        target = path_in(store.root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = identity(original)
        shutil.copyfile(original, target)
        if identity(original) != before or identity(target) != before:
            raise RuntimeError("runtime output changed during preservation")
        names.append(name)
    if prefix + "/status.json" not in names:
        put(store.root, prefix + "/status.json", status)
        names.append(prefix + "/status.json")
    put(store.root, prefix + "/manifest.json", {name: identity(store.root / name) for name in names})
    put(store.root, "latest_run.json", {**status, "record_path": prefix + "/status.json",
                                        "events_path": prefix + "/events.json"})
    return store.publish([*names, prefix + "/manifest.json", "latest_run.json"],
                         "Preserve daily run " + run_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print("PRESERVATION_COMMIT=" + preserve(args.root))

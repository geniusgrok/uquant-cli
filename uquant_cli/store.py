"""Atomic publication to uquant-cli only, with remote byte and Git-object readback."""
from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY = "geniusgrok/uquant-cli"
BRANCH = "uquant-daily-reports"
REMOTE = "https://github.com/" + REPOSITORY + ".git"


def path_in(root: Path, relative: str) -> Path:
    path = root / relative
    if (not relative or Path(relative).is_absolute() or ".." in Path(relative).parts
            or ".git" in Path(relative).parts
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("unsafe publication path")
    if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError("linked publication path")
    return path


def put(root: Path, relative: str, value: Any) -> None:
    path = path_in(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def read(root: Path, relative: str) -> Any:
    return json.loads(path_in(root, relative).read_text(encoding="utf-8"))


def identity(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"bytes": path.stat().st_size, "sha256": digest}


def verify(root: Path, manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("missing result manifest")
    for name, expected in manifest.items():
        if identity(path_in(root, name)) != expected:
            raise ValueError("result manifest mismatch")


class GitStore:
    def __init__(self, root: Path, remote: str = REMOTE,
                 env: dict[str, str] | None = None) -> None:
        # The only non-network alternative is a local repository for targeted tests.
        if remote != REMOTE and not Path(remote).is_absolute():
            raise ValueError("unapproved write destination")
        self.root, self.remote, self.env = root, remote, env
        root.mkdir(parents=True, exist_ok=False)
        self.git("init", "--quiet")
        self.git("config", "user.name", "uquant-cli observer")
        self.git("config", "user.email", "uquant-cli@users.noreply.github.com")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("remote", "add", "origin", remote)
        listing = self.git("ls-remote", "--exit-code", "origin", "refs/heads/" + BRANCH,
                           check=False)
        if listing.returncode == 0:
            self.git("fetch", "--quiet", "--depth=1", "origin", "refs/heads/" + BRANCH)
            self.git("checkout", "--quiet", "-b", BRANCH, "FETCH_HEAD")
        elif listing.returncode == 2:
            self.git("checkout", "--quiet", "--orphan", BRANCH)
        else:
            raise RuntimeError("cannot establish report branch state")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(["git", *args], cwd=self.root, env=self.env,
                                capture_output=True, timeout=120, check=False)
        if check and result.returncode:
            raise RuntimeError("report Git operation failed: " + args[0])
        return result

    def publish(self, paths: list[str], message: str) -> str:
        if self.git("remote", "get-url", "--push", "origin").stdout.decode().strip() != self.remote:
            raise RuntimeError("publication remote changed")
        for name in paths:
            path_in(self.root, name)
        self.git("add", "--", *paths)
        self.git("commit", "--quiet", "-m", message)
        expected = self.git("rev-parse", "HEAD").stdout.decode().strip()
        # Never retry an ambiguous write. Resolve it by fetching the actual target ref.
        with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
            self.git("push", "--quiet", "origin", "HEAD:refs/heads/" + BRANCH)
        self.git("fetch", "--quiet", "origin", "refs/heads/" + BRANCH)
        actual = self.git("rev-parse", "FETCH_HEAD").stdout.decode().strip()
        if actual != expected:
            raise RuntimeError("publication conflict or unconfirmed write")
        for name in paths:
            original = path_in(self.root, name)
            with tempfile.TemporaryFile() as fetched:
                subprocess.run(["git", "show", actual + ":" + name], cwd=self.root,
                               env=self.env, stdout=fetched, stderr=subprocess.PIPE,
                               timeout=120, check=True)
                fetched.seek(0)
                with original.open("rb") as local:
                    while True:
                        block = local.read(1024 * 1024)
                        if block != fetched.read(len(block)):
                            raise RuntimeError("remote byte mismatch")
                        if not block:
                            if fetched.read(1):
                                raise RuntimeError("remote length mismatch")
                            break
                fetched.seek(0)
                if hashlib.file_digest(fetched, "sha256").hexdigest() != identity(original)["sha256"]:
                    raise RuntimeError("remote SHA-256 mismatch")
            if self.git("hash-object", "--", name).stdout.strip() != self.git(
                    "rev-parse", actual + ":" + name).stdout.strip():
                raise RuntimeError("remote Git blob mismatch")
        return actual


def open_store(root: Path) -> GitStore:
    """Use the official checkout's Git authentication; never handle a credential value."""
    import os

    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("unapproved runner")
    checkout = Path(os.environ["UQUANT_REPORT_STATE"])
    if not checkout.resolve().is_relative_to(Path(os.environ["GITHUB_WORKSPACE"]).resolve()):
        raise ValueError("report checkout must belong to the CLI workspace")
    del root  # The same authenticated checkout is reused for the entire run.
    store = object.__new__(GitStore)
    store.root, store.env = checkout, None
    remote = store.git("remote", "get-url", "--push", "origin").stdout.decode().strip()
    if remote not in {REMOTE, REMOTE.removesuffix(".git")}:
        raise RuntimeError("unapproved write destination")
    store.remote = remote
    if store.git("branch", "--show-current").stdout.decode().strip() != BRANCH:
        raise RuntimeError("not the approved report branch")
    store.git("config", "user.name", "uquant-cli observer")
    store.git("config", "user.email", "uquant-cli@users.noreply.github.com")
    store.git("config", "core.hooksPath", "/dev/null")
    store.git("fetch", "--quiet", "origin", "refs/heads/" + BRANCH)
    store.git("merge", "--ff-only", "FETCH_HEAD")
    if store.git("rev-parse", "HEAD").stdout != store.git("rev-parse", "FETCH_HEAD").stdout:
        raise RuntimeError("unconfirmed prior write; refusing another publication")
    return store

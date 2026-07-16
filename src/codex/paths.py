"""Resolve per-process Codex homes without assuming a single global environment."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from models import CodexPaths, ProcessIdentity


@dataclass(frozen=True)
class ResolvedInstance:
    instance_id: str
    paths: CodexPaths
    method: str


class ProcReader:
    def __init__(self, root: Path = Path("/proc")) -> None:
        self.root = root

    def cwd(self, pid: int) -> Path | None:
        try:
            return Path(os.readlink(self.root / str(pid) / "cwd"))
        except OSError:
            return None

    def environ(self, pid: int) -> dict[str, str] | None:
        try:
            payload = (self.root / str(pid) / "environ").read_bytes()
        except OSError:
            return None
        result: dict[str, str] = {}
        for entry in payload.split(b"\0"):
            if b"=" not in entry:
                continue
            key, value = entry.split(b"=", 1)
            result[key.decode(errors="replace")] = value.decode(errors="replace")
        return result

    def fd_targets(self, pid: int) -> list[Path]:
        directory = self.root / str(pid) / "fd"
        try:
            descriptors = list(directory.iterdir())
        except OSError:
            return []
        targets: list[Path] = []
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            targets.append(Path(target.removesuffix(" (deleted)")))
        return targets

    def identity(self, pid: int) -> ProcessIdentity:
        try:
            raw = (self.root / str(pid) / "stat").read_text(errors="replace")
            suffix = raw.rsplit(")", 1)[1].split()
            start_time = int(suffix[19])
        except (OSError, IndexError, ValueError):
            start_time = 0
        return ProcessIdentity(pid, start_time)


def canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _infer_codex_home(targets: list[Path]) -> Path | None:
    for target in targets:
        parts = target.parts
        try:
            index = parts.index("sessions")
        except ValueError:
            continue
        if target.suffix == ".jsonl" and index > 0:
            return Path(*parts[:index])
    return None


def _infer_sqlite_home(targets: list[Path]) -> Path | None:
    for target in targets:
        if target.name.startswith(("state_", "logs_")) and target.suffix == ".sqlite":
            return target.parent
    return None


def instance_id(codex_home: Path, sqlite_home: Path) -> str:
    payload = f"{codex_home}\0{sqlite_home}".encode()
    return hashlib.blake2s(payload, digest_size=8).hexdigest()


def open_rollout_paths(pid: int, sessions_dir: Path, proc: ProcReader) -> list[Path]:
    paths: set[Path] = set()
    for target in proc.fd_targets(pid):
        if target.suffix != ".jsonl":
            continue
        try:
            canonical(target).relative_to(canonical(sessions_dir))
        except ValueError:
            continue
        paths.add(target)
    return sorted(paths)


def resolve_instance(pid: int, proc: ProcReader) -> ResolvedInstance:
    environment_value = proc.environ(pid)
    environment = environment_value or {}
    environment_readable = environment_value is not None
    cwd = proc.cwd(pid) or Path.cwd()
    targets = proc.fd_targets(pid)

    raw_codex_home = environment.get("CODEX_HOME", "").strip()
    inferred_codex_home = _infer_codex_home(targets)
    if raw_codex_home:
        raw_path = Path(raw_codex_home)
        codex_home = canonical(raw_path if raw_path.is_absolute() else cwd / raw_path)
        method = "environment"
    elif inferred_codex_home:
        codex_home = canonical(inferred_codex_home)
        method = "file-descriptor"
    else:
        codex_home = canonical(Path.home() / ".codex")
        method = "default" if environment_readable else "unresolved"

    raw_sqlite_home = environment.get("CODEX_SQLITE_HOME", "").strip()
    inferred_sqlite_home = _infer_sqlite_home(targets)
    if raw_sqlite_home:
        sqlite_path = Path(raw_sqlite_home)
        sqlite_home = canonical(sqlite_path if sqlite_path.is_absolute() else cwd / sqlite_path)
        method = "environment"
    elif inferred_sqlite_home:
        sqlite_home = canonical(inferred_sqlite_home)
        if method == "default":
            method = "file-descriptor"
    else:
        sqlite_home = codex_home

    paths = CodexPaths(
        codex_home=codex_home,
        sqlite_home=sqlite_home,
        state_db=sqlite_home / "state_5.sqlite",
        log_db=sqlite_home / "logs_2.sqlite",
        session_index=codex_home / "session_index.jsonl",
        sessions_dir=codex_home / "sessions",
    )
    return ResolvedInstance(instance_id(codex_home, sqlite_home), paths, method)

"""Read-only regular-file tail collector for child stdout and stderr."""

from __future__ import annotations

import codecs
import os
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from models import SessionIdentity, TerminalCapability

from .terminal import (
    MAX_TERMINAL_BYTES,
    TerminalStore,
    TerminalUpdate,
    sanitize_terminal_text,
)

MAX_FILE_TAIL_DIAGNOSTICS = 64

@dataclass
class _FileCursor:
    device: int
    inode: int
    offset: int = 0
    partial: bytes = b""
    process_id: str = ""
    command: str = ""
    cwd: str = ""


@dataclass(frozen=True)
class RegularFileTailDiagnostic:
    session_key: str | SessionIdentity
    observed_at: float
    pid: int
    start_time: int
    fds: tuple[int, ...]
    reason: str


class RegularFileTailCollector:
    """Tail child stdout/stderr only when they resolve to allowed regular files."""

    def __init__(self, root: Path = Path("/proc"), max_read_bytes: int = 512 * 1024) -> None:
        self.root = root
        self.max_read_bytes = max_read_bytes
        self.cursors: dict[tuple[str | SessionIdentity, int, int, int, int], _FileCursor] = {}
        self.diagnostics: deque[RegularFileTailDiagnostic] = deque(maxlen=MAX_FILE_TAIL_DIAGNOSTICS)

    def _diagnose(
        self,
        session_key: str | SessionIdentity,
        observed_at: float,
        pid: int,
        start_time: int,
        fds: set[int],
        reason: str,
    ) -> None:
        diagnostic = RegularFileTailDiagnostic(
            session_key,
            observed_at,
            pid,
            start_time,
            tuple(sorted(fds)),
            reason,
        )
        if not self.diagnostics or self.diagnostics[-1] != diagnostic:
            self.diagnostics.append(diagnostic)

    def pop_diagnostics(
        self, session_key: str | SessionIdentity
    ) -> tuple[RegularFileTailDiagnostic, ...]:
        matched = tuple(item for item in self.diagnostics if item.session_key == session_key)
        self.diagnostics = deque(
            (item for item in self.diagnostics if item.session_key != session_key),
            maxlen=MAX_FILE_TAIL_DIAGNOSTICS,
        )
        return matched

    def active_scopes(self) -> set[str]:
        return {
            f"file:{pid}:{start_time}:{device}:{inode}"
            for _, pid, start_time, device, inode in self.cursors
        }

    @staticmethod
    def _allowed(target: Path, workspace: Path) -> bool:
        target = target.resolve(strict=False)
        roots = [workspace.resolve(strict=False), Path("/tmp")]
        for root in roots:
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @classmethod
    def _opened_file_matches(
        cls,
        handle: BinaryIO,
        descriptor: Path,
        *,
        device: int,
        inode: int,
        workspace: Path,
    ) -> os.stat_result | None:
        try:
            fileno = handle.fileno()
            opened_stat = os.fstat(fileno)
            current_stat = descriptor.stat()
            raw_target = os.readlink(Path("/proc/self/fd") / str(fileno))
        except (AttributeError, OSError):
            return None
        if not stat.S_ISREG(opened_stat.st_mode):
            return None
        expected = (device, inode)
        if (opened_stat.st_dev, opened_stat.st_ino) != expected:
            return None
        if (current_stat.st_dev, current_stat.st_ino) != expected:
            return None
        opened_target = Path(raw_target.removesuffix(" (deleted)"))
        if not opened_target.is_absolute() or not cls._allowed(opened_target, workspace):
            return None
        return opened_stat

    def read(
        self,
        session_key: str | SessionIdentity,
        workspace: str,
        children: tuple[object, ...],
        observed_at: float,
    ) -> tuple[TerminalUpdate, ...]:
        workspace_path = Path(workspace or ".")
        updates: list[TerminalUpdate] = []
        active_keys: set[tuple[str | SessionIdentity, int, int, int, int]] = set()
        observer_process_ids = TerminalStore._observer_process_ids(children)
        child_by_pid = {
            getattr(getattr(child, "identity", None), "pid", None): child for child in children
        }

        def depth(child: object) -> int:
            value = 0
            parent_pid = getattr(child, "parent_pid", None)
            seen: set[int] = set()
            while isinstance(parent_pid, int) and parent_pid in child_by_pid:
                if parent_pid in seen:
                    break
                seen.add(parent_pid)
                value += 1
                parent_pid = getattr(child_by_pid[parent_pid], "parent_pid", None)
            return value

        claimed_files: set[tuple[int, int]] = set()
        for child in sorted(children, key=depth):
            identity = getattr(child, "identity", None)
            pid = getattr(identity, "pid", None)
            start_time = getattr(identity, "start_time", 0)
            if not isinstance(pid, int) or pid in observer_process_ids:
                continue
            targets: dict[tuple[int, int], tuple[Path, set[int]]] = {}
            for fd in (1, 2):
                descriptor = self.root / str(pid) / "fd" / str(fd)
                try:
                    raw_target = os.readlink(descriptor)
                    target = Path(raw_target.removesuffix(" (deleted)"))
                    descriptor_stat = descriptor.stat()
                except OSError:
                    process_cursor_keys = {
                        key
                        for key in self.cursors
                        if key[0] == session_key and key[1:3] == (pid, start_time)
                    }
                    if process_cursor_keys:
                        active_keys.update(process_cursor_keys)
                        self._diagnose(
                            session_key,
                            observed_at,
                            pid,
                            start_time,
                            {fd},
                            "descriptor_unavailable",
                        )
                    continue
                if not target.is_absolute() or not stat.S_ISREG(descriptor_stat.st_mode):
                    continue
                if not self._allowed(target, workspace_path):
                    continue
                key = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                if key in targets:
                    targets[key][1].add(fd)
                else:
                    targets[key] = (descriptor, {fd})
            for (device, inode), (descriptor, fds) in targets.items():
                if (device, inode) in claimed_files:
                    continue
                claimed_files.add((device, inode))
                cursor_key = (session_key, pid, start_time, device, inode)
                cursor = self.cursors.get(cursor_key)
                try:
                    handle = descriptor.open("rb")
                except OSError:
                    self._diagnose(
                        session_key,
                        observed_at,
                        pid,
                        start_time,
                        fds,
                        "descriptor_open_failed",
                    )
                    if cursor is not None:
                        active_keys.add(cursor_key)
                    continue
                with handle:
                    opened_stat = self._opened_file_matches(
                        handle,
                        descriptor,
                        device=device,
                        inode=inode,
                        workspace=workspace_path,
                    )
                    if opened_stat is None:
                        self._diagnose(
                            session_key,
                            observed_at,
                            pid,
                            start_time,
                            fds,
                            "opened_identity_mismatch",
                        )
                        if cursor is not None:
                            active_keys.add(cursor_key)
                        continue
                    active_keys.add(cursor_key)
                    scope = f"file:{pid}:{start_time}:{device}:{inode}"
                    size = opened_stat.st_size
                    upstream_truncated = False
                    if cursor is None:
                        offset = max(0, size - MAX_TERMINAL_BYTES)
                        upstream_truncated = offset > 0
                        cursor = _FileCursor(
                            device,
                            inode,
                            offset,
                            process_id=f"os:{pid}:{start_time}",
                            command=str(getattr(child, "command", "") or "child process"),
                            cwd=workspace,
                        )
                        self.cursors[cursor_key] = cursor
                    elif size < cursor.offset:
                        cursor.offset = 0
                        cursor.partial = b""
                    updates.append(
                        TerminalUpdate(
                            source_id=(
                                f"file-active:{pid}:{start_time}:{device}:{inode}:{observed_at}"
                            ),
                            observed_at=observed_at,
                            process_id=cursor.process_id,
                            command=cursor.command,
                            cwd=cursor.cwd,
                            status="running",
                            capability=TerminalCapability.FILE_TAIL,
                            terminal_candidate=True,
                            upstream_truncated=upstream_truncated,
                            source="file-tail",
                            scope=scope,
                        )
                    )
                    if size <= cursor.offset:
                        continue
                    try:
                        handle.seek(cursor.offset)
                        start = cursor.offset
                        payload = handle.read(self.max_read_bytes)
                        cursor.offset = handle.tell()
                    except OSError:
                        self._diagnose(
                            session_key,
                            observed_at,
                            pid,
                            start_time,
                            fds,
                            "read_failed",
                        )
                        continue
                if not payload:
                    continue
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                decoded = decoder.decode(cursor.partial + payload, final=False)
                cursor.partial = decoder.getstate()[0]
                streams = {1: "stdout", 2: "stderr"}
                stream = streams[next(iter(fds))] if len(fds) == 1 else "combined"
                updates.append(
                    TerminalUpdate(
                        source_id=f"file:{pid}:{start_time}:{device}:{inode}:{start}",
                        observed_at=observed_at,
                        process_id=cursor.process_id,
                        command=cursor.command,
                        cwd=cursor.cwd,
                        status="running",
                        stream=stream,
                        output=sanitize_terminal_text(decoded),
                        capability=TerminalCapability.FILE_TAIL,
                        terminal_candidate=True,
                        upstream_truncated=upstream_truncated,
                        source="file-tail",
                        scope=scope,
                    )
                )
        closed_keys = {
            key for key in self.cursors if key[0] == session_key and key not in active_keys
        }
        for closed_key in closed_keys:
            cursor = self.cursors[closed_key]
            updates.append(
                TerminalUpdate(
                    source_id=f"file-closed:{cursor.device}:{cursor.inode}:{observed_at}",
                    observed_at=observed_at,
                    process_id=cursor.process_id,
                    command=cursor.command,
                    cwd=cursor.cwd,
                    status="completed",
                    capability=TerminalCapability.FILE_TAIL,
                    terminal_candidate=True,
                    source="file-tail",
                    scope=(f"file:{closed_key[1]}:{closed_key[2]}:{closed_key[3]}:{closed_key[4]}"),
                )
            )
        self.cursors = {
            key: cursor
            for key, cursor in self.cursors.items()
            if key[0] != session_key or key in active_keys
        }
        return tuple(updates)

    def prune(self, retained_session_keys: set[str | SessionIdentity]) -> None:
        self.cursors = {
            key: cursor for key, cursor in self.cursors.items() if key[0] in retained_session_keys
        }

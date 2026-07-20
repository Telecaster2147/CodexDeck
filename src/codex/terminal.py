"""Build bounded, read-only terminal transcripts from Codex evidence."""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import stat
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from models import TerminalCapability, TerminalChunk, TerminalSessionSummary
from utils import redact_sensitive


MAX_TERMINAL_BYTES = 2 * 1024 * 1024
MAX_TERMINAL_CHUNKS = 4_000
MAX_TERMINALS_PER_SESSION = 16
MAX_GLOBAL_TERMINAL_BYTES = 16 * 1024 * 1024

_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_STRING_ESCAPE = re.compile(r"\x1b[PX^_].*?\x1b\\", re.DOTALL)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
_BACKGROUND_RUNNING = re.compile(
    r"(?:Script running with cell ID|Process running with session ID) "
    r"(?P<process>\S+).*?(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_SCRIPT_RESULT = re.compile(
    r"^Script (?:completed|failed).*?\n"
    r"Wall time[^\n]*\n(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_PROCESS_EXITED = re.compile(
    r"^Process exited with code (?P<code>-?\d+).*?\n"
    r"(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_EXIT_CODE = re.compile(r"(?:Process exited with code|exit(?: code)?)\s*(?P<code>-?\d+)", re.I)
_TRUNCATED = re.compile(r"(?:tokens?|bytes?|chars?)\s+(?:truncated|omitted)|truncated after", re.I)
_TOOL_OUTPUT_TYPES = {
    "custom_tool_call_output",
    "function_call_output",
    "local_shell_call_output",
}
_TERMINAL_TOOLS = {"exec", "exec_command", "local_shell_call", "write_stdin", "wait"}


def sanitize_terminal_text(value: str) -> str:
    """Remove terminal control sequences without replaying them to CodexNet's TTY."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC.sub("", text)
    text = _STRING_ESCAPE.sub("", text)
    text = _CSI.sub("", text)
    return redact_sensitive(_CONTROL.sub("", text))


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_terminal_call(value: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, str):
        return "", {}
    for match in re.finditer(
        r"tools\.(?P<tool>exec_command|write_stdin|wait)\(\s*",
        value,
    ):
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value[match.end() :])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return match.group("tool"), parsed
    return "", {}


def _argument_value(payload: dict[str, Any], item: dict[str, Any]) -> object:
    return (
        payload.get("arguments")
        or payload.get("input")
        or item.get("arguments")
        or item.get("input")
    )


def _arguments(payload: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    raw = _argument_value(payload, item)
    return _mapping(raw) or _nested_terminal_call(raw)[1]


def _tool_name(payload: dict[str, Any], item: dict[str, Any], item_type: str) -> str:
    explicit = str(
        payload.get("name")
        or payload.get("tool")
        or item.get("name")
        or item.get("tool")
        or ""
    ).strip()
    if explicit:
        return explicit
    if item_type in {"exec_command_begin", "exec_command_end", "command_execution"}:
        return "exec_command"
    return ""


def _command(value: object) -> str:
    if isinstance(value, list):
        return shlex.join(str(part) for part in value)
    return str(value or "")


def _content_text(value: object) -> str:
    """Flatten Codex content parts without serializing their container syntax."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_text(part) for part in value)
    if isinstance(value, dict):
        for key in ("text", "output", "content", "value"):
            if key in value:
                return _content_text(value[key])
        return ""
    return str(value)


def _output_fields(value: object) -> tuple[str, str, str, int | None, bool]:
    """Return text, stream, process id, exit code, and upstream truncation."""

    if isinstance(value, dict):
        process_id = str(
            value.get("process_id")
            or value.get("session_id")
            or value.get("cell_id")
            or ""
        )
        exit_code = value.get("exit_code")
        text = _content_text(
            value.get("output")
            or value.get("aggregated_output")
            or value.get("stdout")
            or value.get("stderr")
            or ""
        )
        stream = "stderr" if value.get("stderr") and not value.get("stdout") else "combined"
        return (
            sanitize_terminal_text(text),
            stream,
            process_id,
            int(exit_code) if isinstance(exit_code, (int, float)) else None,
            bool(value.get("truncated") or value.get("omitted_bytes")) or bool(_TRUNCATED.search(text)),
        )
    text = sanitize_terminal_text(_content_text(value))
    running = _BACKGROUND_RUNNING.search(text)
    if running:
        return (
            running.group("output"),
            "combined",
            running.group("process"),
            None,
            bool(_TRUNCATED.search(text)),
        )
    script_result = _SCRIPT_RESULT.search(text)
    if script_result:
        text = script_result.group("output")
    exited_result = _PROCESS_EXITED.search(text)
    if exited_result:
        return (
            exited_result.group("output"),
            "combined",
            "",
            int(exited_result.group("code")),
            bool(_TRUNCATED.search(text)),
        )
    exit_match = _EXIT_CODE.search(text)
    return (
        text,
        "combined",
        "",
        int(exit_match.group("code")) if exit_match else None,
        bool(_TRUNCATED.search(text)),
    )


@dataclass(frozen=True)
class TerminalUpdate:
    source_id: str
    observed_at: float
    call_id: str = ""
    process_id: str = ""
    turn_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = "unknown"
    exit_code: int | None = None
    stream: str = "combined"
    output: str = ""
    capability: TerminalCapability = TerminalCapability.METADATA_ONLY
    terminal_candidate: bool = False
    cumulative: bool = False
    upstream_truncated: bool = False
    source: str = "rollout"
    continuation: bool = False


def extract_terminal_updates(
    record: dict[str, object], source_id: str, observed_at: float
) -> tuple[TerminalUpdate, ...]:
    """Extract terminal evidence without changing lifecycle normalization."""

    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    item = payload.get("item")
    item = item if isinstance(item, dict) else {}
    item_type = str(payload.get("type") or item.get("type") or "")
    if record_type == "event_msg" and item_type in {"item_started", "item_completed"} and item:
        payload = {**payload, **item, "item": item}
        item_type = str(item.get("type") or "")

    call_id = str(
        payload.get("call_id")
        or payload.get("id")
        or item.get("call_id")
        or item.get("id")
        or ""
    )
    turn_id = str(payload.get("turn_id") or item.get("turn_id") or "")
    process_id = str(payload.get("process_id") or item.get("process_id") or "")
    tool_name = _tool_name(payload, item, item_type).lower()
    nested_tool, _ = _nested_terminal_call(_argument_value(payload, item))
    if nested_tool and tool_name in {"exec", "functions.exec"}:
        tool_name = nested_tool
    arguments = _arguments(payload, item)
    if not process_id:
        process_id = str(
            arguments.get("process_id")
            or arguments.get("session_id")
            or arguments.get("cell_id")
            or ""
        )
    command = _command(
        payload.get("command")
        or item.get("command")
        or arguments.get("cmd")
        or arguments.get("command")
    )
    cwd = str(
        payload.get("cwd")
        or item.get("cwd")
        or arguments.get("workdir")
        or arguments.get("cwd")
        or ""
    )

    output_value = (
        payload.get("output")
        or payload.get("result")
        or item.get("output")
        or item.get("result")
    )
    direct_end = item_type in {"exec_command_end", "command_execution"}
    if direct_end:
        stdout = payload.get("stdout") or item.get("stdout")
        stderr = payload.get("stderr") or item.get("stderr")
        if stdout is not None or stderr is not None:
            declared_exit = payload.get("exit_code")
            if declared_exit is None:
                declared_exit = item.get("exit_code")
            exit_code = int(declared_exit) if isinstance(declared_exit, (int, float)) else None
            status = str(payload.get("status") or item.get("status") or "completed").lower()
            truncated = bool(
                payload.get("truncated")
                or item.get("truncated")
                or payload.get("omitted_bytes")
                or item.get("omitted_bytes")
            )
            updates: list[TerminalUpdate] = []
            for stream, value in (("stdout", stdout), ("stderr", stderr)):
                if value in (None, ""):
                    continue
                text = sanitize_terminal_text(str(value))
                updates.append(
                    TerminalUpdate(
                        source_id=f"{source_id}:{stream}",
                        observed_at=observed_at,
                        call_id=call_id,
                        process_id=process_id,
                        turn_id=turn_id,
                        command=command,
                        cwd=cwd,
                        status=status,
                        exit_code=exit_code,
                        stream=stream,
                        output=text,
                        capability=TerminalCapability.FINAL_TRANSCRIPT,
                        terminal_candidate=True,
                        upstream_truncated=truncated or bool(_TRUNCATED.search(text)),
                    )
                )
            if updates:
                return tuple(updates)
    if direct_end:
        output_value = (
            payload.get("aggregated_output")
            or item.get("aggregated_output")
            or payload.get("stdout")
            or item.get("stdout")
            or output_value
        )
    if record_type == "response_item" and item_type in _TOOL_OUTPUT_TYPES:
        output_value = payload.get("output") or payload.get("result")

    if output_value is not None:
        text, stream, output_process, exit_code, truncated = _output_fields(output_value)
        declared_exit = payload.get("exit_code")
        if declared_exit is None:
            declared_exit = item.get("exit_code")
        if exit_code is None and isinstance(declared_exit, (int, float)):
            exit_code = int(declared_exit)
        process_id = process_id or output_process
        running = bool(output_process and exit_code is None)
        return (
            TerminalUpdate(
                source_id=source_id,
                observed_at=observed_at,
                call_id=call_id,
                process_id=process_id,
                turn_id=turn_id,
                command=command,
                cwd=cwd,
                status=(
                    "running"
                    if running
                    else str(payload.get("status") or item.get("status") or "completed").lower()
                ),
                exit_code=exit_code,
                stream=stream,
                output=text,
                capability=(
                    TerminalCapability.POLL_TRANSCRIPT
                    if running or process_id
                    else TerminalCapability.FINAL_TRANSCRIPT
                ),
                terminal_candidate=bool(
                    tool_name in _TERMINAL_TOOLS
                    or process_id
                    or command
                    or direct_end
                ),
                cumulative=direct_end,
                upstream_truncated=truncated,
            ),
        )

    is_terminal = bool(
        tool_name in _TERMINAL_TOOLS
        or item_type in {"exec_command_begin", "command_execution"}
        or command
        or process_id
    )
    if not is_terminal:
        return ()
    status = str(payload.get("status") or item.get("status") or "running").lower()
    exit_code = payload.get("exit_code") or item.get("exit_code")
    return (
        TerminalUpdate(
            source_id=source_id,
            observed_at=observed_at,
            call_id=call_id,
            process_id=process_id,
            turn_id=turn_id,
            command=command,
            cwd=cwd,
            status=status,
            exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            capability=(
                TerminalCapability.POLL_TRANSCRIPT
                if process_id or tool_name in {"write_stdin", "wait"}
                else TerminalCapability.METADATA_ONLY
            ),
            terminal_candidate=True,
            continuation=tool_name in {"write_stdin", "wait"},
        ),
    )


@dataclass
class _TerminalSession:
    terminal_id: str
    root_call_id: str = ""
    process_id: str = ""
    turn_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = "unknown"
    exit_code: int | None = None
    capability: TerminalCapability = TerminalCapability.METADATA_ONLY
    started_at: float | None = None
    completed_at: float | None = None
    last_output_at: float | None = None
    dropped_bytes: int = 0
    upstream_truncated: bool = False
    stale: bool = False
    source: str = "rollout"
    last_state_at: float = 0.0
    chunks: deque[TerminalChunk] = field(default_factory=deque)
    retained_bytes: int = 0

    def append(self, update: TerminalUpdate, sequence: int) -> None:
        text = update.output
        if update.cumulative and text and self.chunks:
            current = "".join(chunk.text for chunk in self.chunks if chunk.stream != "system")
            if text == current or current.endswith(text):
                text = ""
            elif text.startswith(current):
                text = text[len(current) :]
            elif current and current in text:
                text = text.split(current, 1)[1]
            elif current:
                text = "\n[final aggregate]\n" + text
        if not text:
            return
        encoded = text.encode("utf-8", errors="replace")
        self.chunks.append(
            TerminalChunk(
                source_id=update.source_id,
                observed_at=update.observed_at,
                stream=update.stream,
                text=text,
                sequence=sequence,
            )
        )
        self.retained_bytes += len(encoded)
        self.last_output_at = update.observed_at
        while self.chunks and (
            len(self.chunks) > MAX_TERMINAL_CHUNKS
            or self.retained_bytes > MAX_TERMINAL_BYTES
        ):
            removed = self.chunks.popleft()
            size = len(removed.text.encode("utf-8", errors="replace"))
            self.retained_bytes = max(0, self.retained_bytes - size)
            self.dropped_bytes += size

    def summary(self) -> TerminalSessionSummary:
        chunks = tuple(self.chunks)
        if self.dropped_bytes:
            marker = TerminalChunk(
                source_id=f"trim:{self.terminal_id}",
                observed_at=self.last_output_at or self.started_at or 0.0,
                stream="system",
                text=f"[CodexNet dropped {self.dropped_bytes} earlier bytes]\n",
                sequence=-1,
            )
            chunks = (marker, *chunks)
        return TerminalSessionSummary(
            terminal_id=self.terminal_id,
            root_call_id=self.root_call_id,
            process_id=self.process_id,
            turn_id=self.turn_id,
            command=self.command,
            cwd=self.cwd,
            status=self.status,
            exit_code=self.exit_code,
            capability=self.capability,
            started_at=self.started_at,
            completed_at=self.completed_at,
            last_output_at=self.last_output_at,
            retained_bytes=self.retained_bytes,
            dropped_bytes=self.dropped_bytes,
            upstream_truncated=self.upstream_truncated,
            stale=self.stale,
            source=self.source,
            chunks=chunks,
        )


class TerminalStore:
    """Correlate terminal updates while keeping output memory bounded."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, _TerminalSession]] = defaultdict(dict)
        self.call_ids: dict[str, dict[str, str]] = defaultdict(dict)
        self.process_ids: dict[str, dict[str, str]] = defaultdict(dict)
        self.continuation_call_ids: dict[str, set[str]] = defaultdict(set)
        self.seen_sources: dict[str, set[str]] = defaultdict(set)
        self.sequence = 0

    @staticmethod
    def _rank(capability: TerminalCapability) -> int:
        return {
            TerminalCapability.METADATA_ONLY: 0,
            TerminalCapability.FINAL_TRANSCRIPT: 1,
            TerminalCapability.POLL_TRANSCRIPT: 2,
            TerminalCapability.FILE_TAIL: 3,
            TerminalCapability.STREAMING: 4,
        }[capability]

    def apply(self, session_key: str, updates: tuple[TerminalUpdate, ...]) -> bool:
        changed = False
        for update in updates:
            if update.source_id in self.seen_sources[session_key]:
                continue
            if update.continuation and update.call_id:
                self.continuation_call_ids[session_key].add(update.call_id)
            terminal_id = ""
            if update.process_id:
                terminal_id = self.process_ids[session_key].get(update.process_id, "")
            if not terminal_id and update.call_id:
                terminal_id = self.call_ids[session_key].get(update.call_id, "")
            if not terminal_id and not update.terminal_candidate:
                continue
            if not terminal_id:
                terminal_id = update.process_id or update.call_id or update.source_id
                self.sessions[session_key][terminal_id] = _TerminalSession(
                    terminal_id=terminal_id,
                    root_call_id=update.call_id,
                    process_id=update.process_id,
                    turn_id=update.turn_id,
                    command=update.command,
                    cwd=update.cwd,
                    status=update.status,
                    capability=update.capability,
                    started_at=update.observed_at,
                    source=update.source,
                    last_state_at=update.observed_at,
                )
            terminal = self.sessions[session_key][terminal_id]
            if update.call_id:
                self.call_ids[session_key][update.call_id] = terminal_id
            if update.process_id:
                self.process_ids[session_key][update.process_id] = terminal_id
                terminal.process_id = terminal.process_id or update.process_id
            terminal.root_call_id = terminal.root_call_id or update.call_id
            terminal.turn_id = terminal.turn_id or update.turn_id
            terminal.command = terminal.command or update.command
            terminal.cwd = terminal.cwd or update.cwd
            terminal.upstream_truncated |= update.upstream_truncated
            if self._rank(update.capability) > self._rank(terminal.capability):
                terminal.capability = update.capability
            if update.observed_at >= terminal.last_state_at:
                terminal.last_state_at = update.observed_at
                status = update.status
                if (
                    update.call_id in self.continuation_call_ids[session_key]
                    and update.exit_code is None
                    and status in {"completed", "complete", "success"}
                ):
                    status = "running"
                if status and status != "unknown":
                    terminal.status = status
                if update.exit_code is not None:
                    terminal.exit_code = update.exit_code
                if terminal.status in {"completed", "failed", "declined", "error", "errored"}:
                    terminal.completed_at = update.observed_at
            self.sequence += 1
            terminal.append(update, self.sequence)
            self.seen_sources[session_key].add(update.source_id)
            changed = True
        self._trim_sessions(session_key)
        self._trim_global()
        return changed

    def _trim_sessions(self, session_key: str) -> None:
        values = self.sessions.get(session_key, {})
        if len(values) <= MAX_TERMINALS_PER_SESSION:
            return
        ordered = sorted(
            values.values(),
            key=lambda item: (
                item.status == "running",
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        for terminal in ordered[: len(values) - MAX_TERMINALS_PER_SESSION]:
            values.pop(terminal.terminal_id, None)
            for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
                for key, value in list(mapping.items()):
                    if value == terminal.terminal_id:
                        mapping.pop(key, None)

    def summaries(self, session_key: str) -> list[TerminalSessionSummary]:
        return [
            terminal.summary()
            for terminal in sorted(
                self.sessions.get(session_key, {}).values(),
                key=lambda item: item.started_at or 0.0,
            )
        ]

    def _trim_global(self) -> None:
        terminals = [
            terminal
            for values in self.sessions.values()
            for terminal in values.values()
        ]
        total = sum(terminal.retained_bytes for terminal in terminals)
        if total <= MAX_GLOBAL_TERMINAL_BYTES:
            return
        ordered = sorted(
            terminals,
            key=lambda item: (
                item.status == "running",
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        while total > MAX_GLOBAL_TERMINAL_BYTES and ordered:
            progressed = False
            for terminal in ordered:
                if not terminal.chunks:
                    continue
                removed = terminal.chunks.popleft()
                size = len(removed.text.encode("utf-8", errors="replace"))
                terminal.retained_bytes = max(0, terminal.retained_bytes - size)
                terminal.dropped_bytes += size
                total -= size
                progressed = True
                if total <= MAX_GLOBAL_TERMINAL_BYTES:
                    break
            if not progressed:
                break

    def mark_stale(self, session_key: str) -> None:
        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.status == "running":
                terminal.stale = True

    def prune(self, retained_session_keys: set[str]) -> None:
        for session_key in set(self.sessions) - retained_session_keys:
            self.sessions.pop(session_key, None)
            self.call_ids.pop(session_key, None)
            self.process_ids.pop(session_key, None)
            self.continuation_call_ids.pop(session_key, None)
            self.seen_sources.pop(session_key, None)


@dataclass
class _FileCursor:
    device: int
    inode: int
    offset: int = 0
    partial: bytes = b""


class RegularFileTailCollector:
    """Tail child stdout/stderr only when they resolve to allowed regular files."""

    def __init__(self, root: Path = Path("/proc"), max_read_bytes: int = 512 * 1024) -> None:
        self.root = root
        self.max_read_bytes = max_read_bytes
        self.cursors: dict[tuple[str, int, int], _FileCursor] = {}

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

    def read(
        self,
        session_key: str,
        workspace: str,
        children: tuple[object, ...],
        observed_at: float,
    ) -> tuple[TerminalUpdate, ...]:
        workspace_path = Path(workspace or ".")
        updates: list[TerminalUpdate] = []
        active_keys: set[tuple[str, int, int]] = set()
        for child in children:
            identity = getattr(child, "identity", None)
            pid = getattr(identity, "pid", None)
            start_time = getattr(identity, "start_time", 0)
            if not isinstance(pid, int):
                continue
            targets: dict[tuple[int, int], tuple[Path, set[int]]] = {}
            for fd in (1, 2):
                descriptor = self.root / str(pid) / "fd" / str(fd)
                try:
                    raw_target = os.readlink(descriptor)
                    target = Path(raw_target.removesuffix(" (deleted)"))
                    descriptor_stat = descriptor.stat()
                except OSError:
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
                cursor_key = (session_key, pid, inode)
                active_keys.add(cursor_key)
                cursor = self.cursors.get(cursor_key)
                try:
                    size = descriptor.stat().st_size
                except OSError:
                    continue
                upstream_truncated = False
                if cursor is None or (cursor.device, cursor.inode) != (device, inode):
                    offset = max(0, size - MAX_TERMINAL_BYTES)
                    upstream_truncated = offset > 0
                    cursor = _FileCursor(device, inode, offset)
                    self.cursors[cursor_key] = cursor
                elif size < cursor.offset:
                    cursor.offset = 0
                    cursor.partial = b""
                if size <= cursor.offset:
                    continue
                try:
                    with descriptor.open("rb") as handle:
                        handle.seek(cursor.offset)
                        start = cursor.offset
                        payload = handle.read(self.max_read_bytes)
                        cursor.offset = handle.tell()
                except OSError:
                    continue
                if not payload:
                    continue
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                decoded = decoder.decode(cursor.partial + payload, final=False)
                cursor.partial = decoder.getstate()[0]
                streams = {1: "stdout", 2: "stderr"}
                stream = (
                    streams[next(iter(fds))]
                    if len(fds) == 1
                    else "combined"
                )
                updates.append(
                    TerminalUpdate(
                        source_id=f"file:{device}:{inode}:{start}",
                        observed_at=observed_at,
                        process_id=f"os:{pid}:{start_time}",
                        command=str(getattr(child, "command", "") or "child process"),
                        cwd=workspace,
                        status="running",
                        stream=stream,
                        output=sanitize_terminal_text(decoded),
                        capability=TerminalCapability.FILE_TAIL,
                        terminal_candidate=True,
                        upstream_truncated=upstream_truncated,
                        source="file-tail",
                    )
                )
        self.cursors = {
            key: cursor
            for key, cursor in self.cursors.items()
            if key[0] != session_key or key in active_keys
        }
        return tuple(updates)

    def prune(self, retained_session_keys: set[str]) -> None:
        self.cursors = {
            key: cursor for key, cursor in self.cursors.items() if key[0] in retained_session_keys
        }

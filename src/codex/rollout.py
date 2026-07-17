"""Incrementally read actively written Codex JSONL rollout files."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from config import MAX_SESSION_TAIL
from models import Confidence, NormalizedEvent
from utils import message_text
from .events import is_compact_command, normalize_rollout_record, parse_timestamp


KNOWN_IGNORED_TYPES = {
    "event_msg:thread_goal_updated",
    "event_msg:thread_rolled_back",
    "event_msg:thread_settings_applied",
    "event_msg:user_message",
    "response_item:message",
}


@dataclass
class RolloutCursor:
    device: int
    inode: int
    offset: int
    partial: bytes = b""
    anchor: bytes = b""
    saw_turn_boundary: bool = False
    saw_user_input: bool = False
    context_tokens: int | None = None
    context_window: int | None = None
    manual_compact_in_flight: bool = False
    pending_empty_task_at: float | None = None
    pending_empty_task_source_id: str = ""
    pending_empty_task_turn_id: str = ""
    pending_context_tokens: int | None = None
    pending_context_window: int | None = None
    stat_size: int = 0
    mtime_ns: int = 0
    last_growth_at: float | None = None


@dataclass(frozen=True)
class RolloutActivity:
    path: str
    observed_at: float
    available: bool = False
    stat_size: int = 0
    mtime_ns: int = 0
    bytes_read: int = 0
    complete_record_count: int = 0
    ignored_record_count: int = 0
    normalized_count: int = 0
    partial_bytes: int = 0
    last_growth_at: float | None = None
    replaced: bool = False
    truncated: bool = False
    copy_truncated: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.bytes_read
            or self.replaced
            or self.truncated
            or self.copy_truncated
        )


@dataclass(frozen=True)
class RolloutReadResult:
    events: tuple[NormalizedEvent, ...]
    activity: RolloutActivity


def _record_shape(record: dict[str, object]) -> tuple[str, str, dict[str, object]]:
    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return record_type, str(payload.get("type") or ""), payload


def _token_context(payload: dict[str, object]) -> tuple[int | None, int | None]:
    info = payload.get("info")
    info = info if isinstance(info, dict) else payload
    last = info.get("last_token_usage") or info.get("last_usage") or {}
    last = last if isinstance(last, dict) else {}
    tokens = info.get("context_tokens") or last.get("input_tokens")
    window = info.get("model_context_window") or info.get("context_window")
    return (
        int(tokens) if isinstance(tokens, (int, float)) else None,
        int(window) if isinstance(window, (int, float)) else None,
    )


class RolloutReader:
    def __init__(self) -> None:
        self.cursors: dict[str, RolloutCursor] = {}
        self.unknown_types: dict[str, Counter[str]] = {}
        self.bootstrap_truncated: set[str] = set()

    def read(self, path: Path) -> list[NormalizedEvent]:
        return list(self.read_with_activity(path).events)

    def read_with_activity(self, path: Path) -> RolloutReadResult:
        observed_at = time.time()
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return RolloutReadResult((), RolloutActivity(key, observed_at))
        cursor = self.cursors.get(key)
        replaced = cursor is not None and (cursor.device, cursor.inode) != (
            stat.st_dev,
            stat.st_ino,
        )
        truncated = cursor is not None and stat.st_size < cursor.offset
        if cursor is None or replaced or truncated:
            if replaced or truncated:
                self.unknown_types.pop(key, None)
            start = max(0, stat.st_size - MAX_SESSION_TAIL)
            if start:
                self.bootstrap_truncated.add(key)
            else:
                self.bootstrap_truncated.discard(key)
            cursor = RolloutCursor(
                stat.st_dev,
                stat.st_ino,
                start,
                stat_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            self.cursors[key] = cursor
        copy_truncated = False
        bytes_read = 0
        try:
            with path.open("rb") as handle:
                if cursor.offset and cursor.anchor:
                    anchor_start = max(0, cursor.offset - len(cursor.anchor))
                    handle.seek(anchor_start)
                    if handle.read(len(cursor.anchor)) != cursor.anchor:
                        # Detect copy-truncate even when the writer has already
                        # grown the new file beyond the previous byte offset.
                        self.unknown_types.pop(key, None)
                        start = max(0, stat.st_size - MAX_SESSION_TAIL)
                        if start:
                            self.bootstrap_truncated.add(key)
                        else:
                            self.bootstrap_truncated.discard(key)
                        cursor = RolloutCursor(
                            stat.st_dev,
                            stat.st_ino,
                            start,
                            stat_size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                        )
                        self.cursors[key] = cursor
                        copy_truncated = True
                handle.seek(cursor.offset)
                if (
                    cursor.offset
                    and not cursor.partial
                    and cursor.offset == max(0, stat.st_size - MAX_SESSION_TAIL)
                ):
                    handle.readline()
                    cursor.offset = handle.tell()
                read_start = cursor.offset
                previous_partial = cursor.partial
                fresh = handle.read()
                bytes_read = len(fresh)
                payload = previous_partial + fresh
                cursor.offset = handle.tell()
                anchor_start = max(0, cursor.offset - 64)
                handle.seek(anchor_start)
                cursor.anchor = handle.read(cursor.offset - anchor_start)
        except OSError:
            return RolloutReadResult(
                (),
                RolloutActivity(
                    key,
                    observed_at,
                    available=True,
                    stat_size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    partial_bytes=len(cursor.partial),
                    last_growth_at=cursor.last_growth_at,
                    replaced=replaced,
                    truncated=truncated,
                    copy_truncated=copy_truncated,
                ),
            )
        if bytes_read or len(payload) != len(previous_partial):
            cursor.last_growth_at = observed_at
        cursor.stat_size = stat.st_size
        cursor.mtime_ns = stat.st_mtime_ns
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            cursor.partial = payload
            return RolloutReadResult(
                (),
                RolloutActivity(
                    key,
                    observed_at,
                    available=True,
                    stat_size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    bytes_read=bytes_read,
                    partial_bytes=len(cursor.partial),
                    last_growth_at=cursor.last_growth_at,
                    replaced=replaced,
                    truncated=truncated,
                    copy_truncated=copy_truncated,
                ),
            )
        complete = payload[: last_newline + 1]
        cursor.partial = payload[last_newline + 1 :]
        events: list[NormalizedEvent] = []
        complete_record_count = 0
        ignored_record_count = 0
        base_offset = read_start - len(previous_partial)
        position = base_offset
        for raw_line in complete.splitlines(keepends=True):
            line_offset = position
            position += len(raw_line)
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                ignored_record_count += 1
                continue
            if isinstance(record, dict):
                complete_record_count += 1
                source_id = f"rollout:{stat.st_ino}:{line_offset}"
                record_type, item_type, item = _record_shape(record)
                explicit_compact = (
                    (record_type == "event_msg" and item_type == "user_message")
                    or (
                        record_type == "response_item"
                        and item_type == "message"
                        and item.get("role") == "user"
                    )
                ) and is_compact_command(item)
                task_started = record_type == "event_msg" and item_type in {
                    "task_started",
                    "turn_started",
                }
                compact_completed = (
                    record_type in {"compacted", "context_compacted"}
                    or (record_type == "event_msg" and item_type == "context_compacted")
                )
                inferred_manual_compact = bool(
                    compact_completed and cursor.pending_empty_task_at is not None
                )
                normalized = normalize_rollout_record(
                    record,
                    source_id,
                    inferred_manual_compact=(
                        inferred_manual_compact
                        or (compact_completed and cursor.manual_compact_in_flight)
                    ),
                    context_tokens=(
                        cursor.pending_context_tokens
                        if inferred_manual_compact
                        else cursor.context_tokens
                    ),
                    context_window=(
                        cursor.pending_context_window
                        if inferred_manual_compact
                        else cursor.context_window
                    ),
                    compact_started_at=cursor.pending_empty_task_at,
                    compact_started_source_id=cursor.pending_empty_task_source_id,
                    compact_started_turn_id=cursor.pending_empty_task_turn_id,
                )
                compact_open = bool(
                    cursor.manual_compact_in_flight
                    or cursor.pending_empty_task_at is not None
                )
                terminal_compact_events = []
                if compact_open:
                    for event in normalized:
                        if event.kind == "TURN_FAILED":
                            terminal_compact_events.append(
                                replace(
                                    event,
                                    kind="COMPACT_FAILED",
                                    summary="compact 失败",
                                    source_id=f"{event.source_id}:compact-failed",
                                    metadata={
                                        **event.metadata,
                                        "trigger": (
                                            "manual"
                                            if cursor.manual_compact_in_flight
                                            else "unknown"
                                        ),
                                    },
                                )
                            )
                        elif event.kind == "TURN_ABORTED":
                            terminal_compact_events.append(
                                replace(
                                    event,
                                    kind="COMPACT_ABORTED",
                                    summary="compact 已中止",
                                    source_id=f"{event.source_id}:compact-aborted",
                                    metadata={
                                        **event.metadata,
                                        "trigger": (
                                            "manual"
                                            if cursor.manual_compact_in_flight
                                            else "unknown"
                                        ),
                                    },
                                )
                            )
                normalized.extend(terminal_compact_events)
                normalized = [replace(event, observed_at=observed_at) for event in normalized]
                events.extend(normalized)

                if record_type == "event_msg" and item_type == "token_count":
                    tokens, window = _token_context(item)
                    cursor.context_tokens = tokens
                    cursor.context_window = window
                if explicit_compact:
                    cursor.manual_compact_in_flight = True
                    cursor.saw_user_input = True
                elif (
                    (record_type == "event_msg" and item_type == "user_message")
                    or (
                        record_type == "response_item"
                        and item_type == "message"
                        and item.get("role") == "user"
                    )
                ):
                    cursor.saw_user_input = True
                if task_started:
                    if (
                        cursor.saw_turn_boundary
                        and not cursor.saw_user_input
                        and cursor.context_tokens is not None
                    ):
                        cursor.pending_empty_task_at = parse_timestamp(record.get("timestamp"))
                        cursor.pending_empty_task_source_id = (
                            f"{source_id}:manual-compact"
                        )
                        cursor.pending_empty_task_turn_id = str(item.get("turn_id") or "")
                        cursor.pending_context_tokens = cursor.context_tokens
                        cursor.pending_context_window = cursor.context_window
                        candidate = NormalizedEvent(
                            timestamp=cursor.pending_empty_task_at,
                            kind="COMPACT_CANDIDATE",
                            summary="检测到可能的 standalone compact turn",
                            source="rollout",
                            confidence=Confidence.MEDIUM,
                            turn_id=cursor.pending_empty_task_turn_id,
                            source_id=f"{source_id}:compact-candidate",
                            derived=True,
                            complete=False,
                            observed_at=observed_at,
                            metadata={
                                "trigger": "unknown",
                                "context_tokens": cursor.context_tokens,
                                "context_window": cursor.context_window,
                            },
                        )
                        events.append(candidate)
                    cursor.saw_turn_boundary = False
                elif cursor.pending_empty_task_at is not None and not compact_completed:
                    allowed_while_pending = record_type == "event_msg" and item_type in {
                        "token_count",
                        "thread_settings_applied",
                    }
                    if not allowed_while_pending:
                        cursor.pending_empty_task_at = None
                        cursor.pending_empty_task_source_id = ""
                        cursor.pending_empty_task_turn_id = ""
                        cursor.pending_context_tokens = None
                        cursor.pending_context_window = None
                if record_type == "event_msg" and item_type in {
                    "task_complete",
                    "turn_complete",
                    "task_aborted",
                    "turn_aborted",
                }:
                    cursor.saw_turn_boundary = True
                    cursor.saw_user_input = False
                    cursor.manual_compact_in_flight = False
                if compact_completed:
                    cursor.saw_turn_boundary = True
                    cursor.saw_user_input = False
                    cursor.manual_compact_in_flight = False
                    cursor.pending_empty_task_at = None
                    cursor.pending_empty_task_source_id = ""
                    cursor.pending_empty_task_turn_id = ""
                    cursor.pending_context_tokens = None
                    cursor.pending_context_window = None
                unparsed = [event for event in normalized if event.kind == "UNPARSED_PAYLOAD"]
                if unparsed:
                    event_type = str(unparsed[-1].unparsed.source_type)
                    self.unknown_types.setdefault(key, Counter())[event_type] += 1
                elif not normalized and record.get("type") in {
                    "event_msg",
                    "response_item",
                }:
                    item = record.get("payload")
                    item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
                    if item_type:
                        event_type = f"{record['type']}:{item_type}"
                        if event_type not in KNOWN_IGNORED_TYPES:
                            self.unknown_types.setdefault(key, Counter())[event_type] += 1
                if not normalized:
                    ignored_record_count += 1
            else:
                ignored_record_count += 1
        return RolloutReadResult(
            tuple(events),
            RolloutActivity(
                key,
                observed_at,
                available=True,
                stat_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                bytes_read=bytes_read,
                complete_record_count=complete_record_count,
                ignored_record_count=ignored_record_count,
                normalized_count=len(events),
                partial_bytes=len(cursor.partial),
                last_growth_at=cursor.last_growth_at,
                replaced=replaced,
                truncated=truncated,
                copy_truncated=copy_truncated,
            ),
        )

    def unknown_counts(self, paths: set[str]) -> dict[str, int]:
        total: Counter[str] = Counter()
        for path in paths:
            total.update(self.unknown_types.get(path, {}))
        return dict(sorted(total.items()))

    def has_truncated_context(self, paths: set[str]) -> bool:
        return bool(paths & self.bootstrap_truncated)

    def prune(self, active_paths: set[str]) -> None:
        self.cursors = {
            path: cursor
            for path, cursor in self.cursors.items()
            if path in active_paths
        }
        self.unknown_types = {
            path: counts
            for path, counts in self.unknown_types.items()
            if path in active_paths
        }
        self.bootstrap_truncated.intersection_update(active_paths)


def rollout_identity(path: Path) -> tuple[str, bool]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return "", True
    if record.get("type") != "session_meta":
        return "", True
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return "", True
    session_id = str(payload.get("session_id") or payload.get("id") or "")
    source = payload.get("source")
    is_subagent = isinstance(source, dict) and "subagent" in source
    return session_id, is_subagent


def latest_user_task(path: Path) -> str:
    """Read a bounded tail and return the latest meaningful user message."""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_SESSION_TAIL:
                handle.seek(size - MAX_SESSION_TAIL)
                handle.readline()
            payload = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    latest = ""
    for line in payload.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        item = record.get("payload")
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") != "user"
        ):
            continue
        text = message_text(item).strip()
        if text and not text.startswith(("<environment_context>", "<turn_aborted>", "# AGENTS.md")):
            latest = " ".join(text.split())
    return latest

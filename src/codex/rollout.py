"""Incrementally read actively written Codex JSONL rollout files."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from config import MAX_SESSION_TAIL
from models import NormalizedEvent
from utils import message_text
from .events import is_compact_command, normalize_rollout_record, parse_timestamp
from .terminal import (
    TerminalProtocolParser,
    TerminalStore,
    TerminalUpdate,
    extract_terminal_updates,
)


KNOWN_IGNORED_TYPES = {
    "event_msg:image_generation_end",
    "event_msg:thread_goal_updated",
    "event_msg:thread_rolled_back",
    "event_msg:thread_settings_applied",
    "event_msg:user_message",
    "event_msg:web_search_end",
    "response_item:compaction",
    "response_item:context_compaction",
    "response_item:message",
}

MAX_TERMINAL_METADATA_BACKFILL = 16 * 1024 * 1024
MAX_TERMINAL_METADATA_BACKFILL_CHUNK = 512 * 1024
TERMINAL_METADATA_LINE_OVERLAP = 64 * 1024
_TERMINAL_METADATA_MARKERS = (
    b"exec_command",
    b"write_stdin",
    b"tools.wait",
    b"Script running with cell ID",
    b"Process running with session ID",
    b"Process exited with code",
    b"Script completed",
    b'"process_id"',
    b'"session_id"',
    b'"cell_id"',
)


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
    context_observed_at: float | None = None
    context_source_id: str = ""
    context_turn_id: str = ""
    manual_compact_in_flight: bool = False
    pending_empty_task_at: float | None = None
    pending_empty_task_source_id: str = ""
    pending_empty_task_turn_id: str = ""
    pending_context_tokens: int | None = None
    pending_context_window: int | None = None
    stat_size: int = 0
    mtime_ns: int = 0
    last_growth_at: float | None = None
    last_compact_completion_at: float | None = None
    last_compact_completion_type: str = ""


@dataclass
class TerminalMetadataBackfillCursor:
    inode: int
    next_end: int
    floor: int
    process_ids: set[str]
    call_ids: set[str] = field(default_factory=set)
    process_call_ids: dict[str, set[str]] = field(default_factory=dict)
    resolved_process_ids: set[str] = field(default_factory=set)
    pending_updates: dict[str, list[TerminalUpdate]] = field(default_factory=dict)


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
    terminal_updates: tuple[TerminalUpdate, ...] = ()


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
        self.terminal_metadata_attempted: dict[str, set[str]] = {}
        self.terminal_metadata_backfills: dict[str, TerminalMetadataBackfillCursor] = {}
        self.terminal_parser = TerminalProtocolParser()

    def read(self, path: Path) -> list[NormalizedEvent]:
        return list(self.read_with_activity(path).events)

    def read_with_activity(
        self,
        path: Path,
        *,
        allow_terminal_metadata_backfill: bool = True,
    ) -> RolloutReadResult:
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
                self.terminal_metadata_attempted.pop(key, None)
                self.terminal_metadata_backfills.pop(key, None)
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
                        self.terminal_metadata_attempted.pop(key, None)
                        self.terminal_metadata_backfills.pop(key, None)
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
            backfill = self._advance_terminal_metadata_backfill(
                path,
                read_start,
                (),
                inode=stat.st_ino,
                allow=allow_terminal_metadata_backfill,
            )
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
                backfill,
            )
        complete = payload[: last_newline + 1]
        cursor.partial = payload[last_newline + 1 :]
        events: list[NormalizedEvent] = []
        terminal_updates: list[TerminalUpdate] = []
        complete_record_count = 0
        ignored_record_count = 0
        base_offset = read_start - len(previous_partial)
        position = base_offset
        for raw_line in complete.splitlines(keepends=True):
            line_offset = position
            position += len(raw_line)
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                ignored_record_count += 1
                continue
            if isinstance(record, dict):
                complete_record_count += 1
                source_id = f"rollout:{stat.st_ino}:{line_offset}"
                terminal_updates.extend(
                    extract_terminal_updates(
                        record,
                        source_id,
                        parse_timestamp(record.get("timestamp")) or observed_at,
                        parser=self.terminal_parser,
                        scope=f"{key}:{stat.st_ino}",
                    )
                )
                record_type, item_type, item = _record_shape(record)
                item_value = item.get("item")
                item_value = item_value if isinstance(item_value, dict) else {}
                compact_item_type = str(item_value.get("type") or "").lower()
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
                    or (
                        record_type == "event_msg"
                        and item_type == "item_completed"
                        and "compact" in compact_item_type
                    )
                )
                inferred_manual_compact = bool(
                    compact_completed and cursor.pending_empty_task_at is not None
                )
                inferred_auto_compact = bool(
                    compact_completed
                    and not inferred_manual_compact
                    and not cursor.manual_compact_in_flight
                    and cursor.saw_user_input
                )
                record_timestamp = parse_timestamp(record.get("timestamp"))
                inferred_auto_started_at = (
                    cursor.context_observed_at
                    if inferred_auto_compact
                    and cursor.context_observed_at is not None
                    and cursor.context_observed_at <= record_timestamp
                    else None
                )
                completion_type = (
                    f"{record_type}:{item_type}:{compact_item_type}"
                    if compact_completed
                    else ""
                )
                duplicate_completion = bool(
                    compact_completed
                    and cursor.last_compact_completion_at is not None
                    and abs(record_timestamp - cursor.last_compact_completion_at) <= 1.0
                    and completion_type != cursor.last_compact_completion_type
                )
                normalized = (
                    []
                    if duplicate_completion
                    else normalize_rollout_record(
                        record,
                        source_id,
                        inferred_manual_compact=(
                            inferred_manual_compact
                            or (compact_completed and cursor.manual_compact_in_flight)
                        ),
                        inferred_auto_compact=inferred_auto_compact,
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
                        compact_started_at=(
                            cursor.pending_empty_task_at
                            if inferred_manual_compact
                            else inferred_auto_started_at
                        ),
                        compact_started_source_id=(
                            cursor.pending_empty_task_source_id
                            if inferred_manual_compact
                            else cursor.context_source_id
                        ),
                        compact_started_turn_id=(
                            cursor.pending_empty_task_turn_id
                            if inferred_manual_compact
                            else cursor.context_turn_id
                        ),
                    )
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
                    cursor.context_observed_at = record_timestamp
                    cursor.context_source_id = source_id
                    cursor.context_turn_id = str(item.get("turn_id") or "")
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
                    ):
                        cursor.pending_empty_task_at = parse_timestamp(record.get("timestamp"))
                        cursor.pending_empty_task_source_id = (
                            f"{source_id}:manual-compact"
                        )
                        cursor.pending_empty_task_turn_id = str(item.get("turn_id") or "")
                        cursor.pending_context_tokens = cursor.context_tokens
                        cursor.pending_context_window = cursor.context_window
                    cursor.saw_turn_boundary = False
                elif cursor.pending_empty_task_at is not None and not compact_completed:
                    allowed_while_pending = record_type == "turn_context" or (
                        record_type == "event_msg"
                        and item_type in {"token_count", "thread_settings_applied"}
                    )
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
                    cursor.manual_compact_in_flight = False
                    cursor.pending_empty_task_at = None
                    cursor.pending_empty_task_source_id = ""
                    cursor.pending_empty_task_turn_id = ""
                    cursor.pending_context_tokens = None
                    cursor.pending_context_window = None
                    cursor.context_observed_at = None
                    cursor.context_source_id = ""
                    cursor.context_turn_id = ""
                if compact_completed:
                    cursor.last_compact_completion_at = record_timestamp
                    cursor.last_compact_completion_type = completion_type
                unparsed = [event for event in normalized if event.kind == "UNPARSED_PAYLOAD"]
                if unparsed:
                    event_type = str(unparsed[-1].unparsed.source_type)
                    self.unknown_types.setdefault(key, Counter())[event_type] += 1
                elif not normalized and not duplicate_completion and record.get("type") in {
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
        backfill = self._advance_terminal_metadata_backfill(
            path,
            read_start,
            tuple(terminal_updates),
            inode=stat.st_ino,
            allow=allow_terminal_metadata_backfill,
        )
        terminal_updates = [*backfill, *terminal_updates]
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
            tuple(terminal_updates),
        )

    @staticmethod
    def _missing_terminal_process_ids(
        current_updates: tuple[TerminalUpdate, ...],
    ) -> set[str]:
        store = TerminalStore()
        store.apply("bootstrap", current_updates)
        return {
            terminal.process_id
            for terminal in store.summaries("bootstrap")
            if terminal.process_id
            and not terminal.command
            and terminal.status in {"running", "pending", "in_progress", "unknown"}
        }

    def _advance_terminal_metadata_backfill(
        self,
        path: Path,
        end_offset: int,
        current_updates: tuple[TerminalUpdate, ...],
        *,
        inode: int,
        allow: bool,
    ) -> tuple[TerminalUpdate, ...]:
        key = str(path)
        if key not in self.bootstrap_truncated:
            return ()
        state = self.terminal_metadata_backfills.get(key)
        missing_process_ids = (
            self._missing_terminal_process_ids(current_updates)
            if current_updates
            else set()
        )
        attempted = self.terminal_metadata_attempted.setdefault(key, set())
        new_process_ids = missing_process_ids - attempted
        if state is not None:
            new_process_ids -= state.process_ids
        if new_process_ids:
            process_ids = new_process_ids | (state.process_ids if state is not None else set())
            process_call_ids = {process_id: set() for process_id in process_ids}
            call_ids: set[str] = set()
            for update in current_updates:
                if update.process_id in process_ids and update.call_id:
                    process_call_ids[update.process_id].add(update.call_id)
                    call_ids.add(update.call_id)
            state = TerminalMetadataBackfillCursor(
                inode=inode,
                next_end=end_offset,
                floor=max(0, end_offset - MAX_TERMINAL_METADATA_BACKFILL),
                process_ids=process_ids,
                call_ids=call_ids,
                process_call_ids=process_call_ids,
            )
            self.terminal_metadata_backfills[key] = state
        if state is None or not allow:
            return ()
        updates, finished = self._terminal_metadata_backfill_step(path, state)
        if finished:
            attempted.update(state.process_ids)
            self.terminal_metadata_backfills.pop(key, None)
        return updates

    @staticmethod
    def _terminal_metadata_backfill_step(
        path: Path,
        state: TerminalMetadataBackfillCursor,
    ) -> tuple[tuple[TerminalUpdate, ...], bool]:
        if state.next_end <= state.floor:
            return (), True
        raw_start = max(
            state.floor,
            state.next_end - MAX_TERMINAL_METADATA_BACKFILL_CHUNK,
        )
        scan_start = max(state.floor, raw_start - TERMINAL_METADATA_LINE_OVERLAP)
        try:
            stat = path.stat()
            if stat.st_ino != state.inode:
                return (), True
            with path.open("rb") as handle:
                handle.seek(scan_start)
                data = handle.read(state.next_end - scan_start)
        except OSError:
            return (), False
        lines: list[tuple[int, bytes]] = []
        position = scan_start
        for index, raw_line in enumerate(data.splitlines(keepends=True)):
            line_offset = position
            position += len(raw_line)
            if index == 0 and scan_start > state.floor:
                continue
            lines.append((line_offset, raw_line))
        relevant: list[TerminalUpdate] = []
        for line_offset, raw_line in reversed(lines):
            if not any(marker in raw_line for marker in _TERMINAL_METADATA_MARKERS):
                continue
            identifiers = state.process_ids | state.call_ids
            identifier_match = not identifiers or any(
                identifier.encode("utf-8") in raw_line for identifier in identifiers
            )
            possible_completion = b"Script completed" in raw_line or b"Process exited" in raw_line
            if not identifier_match and not possible_completion:
                continue
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            source_id = f"rollout:{stat.st_ino}:{line_offset}:metadata"
            observed_at = parse_timestamp(record.get("timestamp")) or stat.st_mtime
            for update in extract_terminal_updates(record, source_id, observed_at):
                matched_processes = (
                    {update.process_id}
                    if update.process_id in state.process_ids
                    else set()
                )
                if matched_processes and update.call_id:
                    state.call_ids.add(update.call_id)
                    for process_id in matched_processes:
                        state.process_call_ids[process_id].add(update.call_id)
                linked_processes = {
                    process_id
                    for process_id, call_ids in state.process_call_ids.items()
                    if update.call_id and update.call_id in call_ids
                }
                if not matched_processes and not linked_processes:
                    if update.call_id and update.status in {
                        "completed",
                        "complete",
                        "success",
                        "failed",
                        "error",
                        "errored",
                    }:
                        state.pending_updates.setdefault(update.call_id, []).append(
                            replace(update, output="", cumulative=False)
                        )
                    continue
                if update.call_id:
                    relevant.extend(state.pending_updates.pop(update.call_id, ()))
                if update.command:
                    state.resolved_process_ids.update(
                        matched_processes | linked_processes
                    )
                relevant.append(replace(update, output="", cumulative=False))
        state.next_end = raw_start
        finished = bool(
            state.resolved_process_ids >= state.process_ids
            or state.next_end <= state.floor
        )
        return tuple(sorted(relevant, key=lambda update: update.observed_at)), finished

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
        self.terminal_metadata_attempted = {
            path: process_ids
            for path, process_ids in self.terminal_metadata_attempted.items()
            if path in active_paths
        }
        self.terminal_metadata_backfills = {
            path: cursor
            for path, cursor in self.terminal_metadata_backfills.items()
            if path in active_paths
        }


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

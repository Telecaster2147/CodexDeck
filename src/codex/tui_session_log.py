"""Incrementally extract only outbound Compact operations from Codex TUI logs."""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codex.events import parse_timestamp
from codex.ingress import (
    MAX_INGRESS_BYTES_PER_TICK,
    MAX_INGRESS_PARSE_SECONDS,
    MAX_INGRESS_RECORDS_PER_TICK,
    MAX_JSONL_RECORD_BYTES,
)
from codex.mutable_stream import (
    anchor_matches,
    anchor_sha256,
    refresh_anchor,
    stream_metadata,
    stream_source_id,
)
from models import Confidence, NormalizedEvent


@dataclass
class SessionLogCursor:
    device: int
    inode: int
    offset: int = 0
    generation: int = 0
    partial: bytes = b""
    anchor: bytes = b""
    stat_size: int = 0
    mtime_ns: int = 0
    stream_uncertain: bool = False
    stream_uncertainty_count: int = 0
    stream_uncertainty_reason: str = ""
    skipping_oversize: bool = False
    skipped_bytes: int = 0
    oversize_records: int = 0
    gap_count: int = 0
    gap_reason: str = ""
    gap_hash: str = ""
    backlog_since: float | None = None


@dataclass(frozen=True)
class SessionLogReadResult:
    events: tuple[tuple[str, NormalizedEvent], ...] = ()
    configured: bool = False
    readable: bool = False
    observed_at: float | None = None
    last_event_at: float | None = None
    error: str = ""
    bytes_read: int = 0
    consumed_bytes: int = 0
    record_count: int = 0
    backlog_bytes: int = 0
    backlog_records_lower_bound: int = 0
    backlog_age_seconds: float | None = None
    budget_exceeded: bool = False
    oversize_record_count: int = 0
    skipped_bytes: int = 0
    gap_count: int = 0
    gap_reason: str = ""
    gap_hash: str = ""
    parse_duration_seconds: float = 0.0
    device: int = 0
    inode: int = 0
    generation: int = 0
    anchor_hash: str = ""
    stream_uncertain: bool = False
    stream_uncertainty_count: int = 0
    stream_uncertainty_reason: str = ""


def configured_session_log_path(
    environment: dict[str, str] | None,
    cwd: str,
) -> Path | None:
    environment = environment or {}
    enabled = environment.get("CODEX_TUI_RECORD_SESSION", "").strip().lower()
    raw_path = environment.get("CODEX_TUI_SESSION_LOG_PATH", "").strip()
    if enabled not in {"1", "true", "yes", "on"} or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(cwd or ".") / path
    return path.resolve(strict=False)


def _values_for_keys(value: object, keys: set[str]) -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in keys and isinstance(item, (str, int, float)):
                results.append(str(item))
            if isinstance(item, (dict, list)):
                results.extend(_values_for_keys(item, keys))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                results.extend(_values_for_keys(item, keys))
    return results


def _first(record: dict[str, Any], keys: set[str]) -> str:
    return next((value for value in _values_for_keys(record, keys) if value), "")


def parse_session_log_record(
    record: dict[str, Any],
    *,
    source_id: str,
    observed_at: float,
    default_session_id: str = "",
) -> tuple[str, NormalizedEvent] | None:
    directions = {
        value.strip().lower().replace("-", "_")
        for value in _values_for_keys(record, {"direction", "dir", "source"})
    }
    outbound = bool(
        directions & {"from_tui", "outbound", "tui_to_core", "client_to_core", "request"}
    )
    if not outbound:
        return None
    typed_values = {
        value.strip().lower().replace("-", "_")
        for value in _values_for_keys(record, {"type", "kind", "op", "name", "variant"})
    }
    if not any(value == "compact" or value.endswith("::compact") for value in typed_values):
        return None
    session_id = _first(record, {"session_id", "thread_id", "conversation_id"})
    session_id = session_id or default_session_id
    turn_id = _first(record, {"turn_id"})
    raw_timestamp: object = next(
        (record[key] for key in ("timestamp", "ts", "time") if key in record),
        observed_at,
    )
    timestamp = parse_timestamp(raw_timestamp) or observed_at
    return session_id, NormalizedEvent(
        timestamp=timestamp,
        kind="COMPACT_REQUESTED",
        summary="用户已发送 /compact",
        source="tui_session_log",
        confidence=Confidence.HIGH,
        turn_id=turn_id,
        source_id=source_id,
        observed_at=observed_at,
        metadata={"trigger": "manual", "typed_op": "Compact"},
    )


class TuiSessionLogReader:
    def __init__(self) -> None:
        self.cursors: dict[str, SessionLogCursor] = {}

    @staticmethod
    def _record_gap(cursor: SessionLogCursor, payload: bytes, reason: str) -> None:
        cursor.skipped_bytes += len(payload)
        cursor.gap_reason = reason
        cursor.gap_hash = hashlib.sha256(
            cursor.gap_hash.encode("ascii") + payload[:MAX_JSONL_RECORD_BYTES]
        ).hexdigest()

    @staticmethod
    def _backlog(
        cursor: SessionLogCursor,
        stat_size: int,
        observed_at: float,
    ) -> tuple[int, float | None]:
        backlog_bytes = max(0, stat_size - cursor.offset)
        if backlog_bytes:
            cursor.backlog_since = cursor.backlog_since or observed_at
        else:
            cursor.backlog_since = None
        return (
            backlog_bytes,
            max(0.0, observed_at - cursor.backlog_since)
            if cursor.backlog_since is not None
            else None,
        )

    def read(
        self,
        path: Path | None,
        *,
        default_session_id: str = "",
    ) -> SessionLogReadResult:
        observed_at = time.time()
        if path is None:
            return SessionLogReadResult(observed_at=observed_at)
        key = str(path)
        try:
            stat = path.stat()
        except OSError as exc:
            return SessionLogReadResult(
                configured=True,
                observed_at=observed_at,
                error=str(exc),
            )
        cursor = self.cursors.get(key)
        replaced = cursor is not None and (cursor.device, cursor.inode) != (
            stat.st_dev,
            stat.st_ino,
        )
        truncated = cursor is not None and stat.st_size < cursor.offset
        if cursor is None or replaced or truncated:
            generation = cursor.generation + 1 if cursor is not None else 0
            uncertainty_count = cursor.stream_uncertainty_count if cursor is not None else 0
            cursor = SessionLogCursor(
                stat.st_dev,
                stat.st_ino,
                generation=generation,
                stat_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                stream_uncertain=bool(truncated),
                stream_uncertainty_count=uncertainty_count + int(truncated),
                stream_uncertainty_reason=("truncated" if truncated else ""),
            )
            self.cursors[key] = cursor
        try:
            with path.open("rb") as handle:
                if not anchor_matches(handle, cursor):
                    cursor = SessionLogCursor(
                        stat.st_dev,
                        stat.st_ino,
                        generation=cursor.generation + 1,
                        stat_size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        stream_uncertain=True,
                        stream_uncertainty_count=cursor.stream_uncertainty_count + 1,
                        stream_uncertainty_reason="content_anchor_mismatch",
                    )
                    self.cursors[key] = cursor
                elif (
                    cursor.offset
                    and stat.st_size == cursor.stat_size
                    and stat.st_mtime_ns != cursor.mtime_ns
                ):
                    cursor.stream_uncertain = True
                    cursor.stream_uncertainty_count += 1
                    cursor.stream_uncertainty_reason = "same_size_mtime_change_anchor_unchanged"
                handle.seek(cursor.offset)
                read_start = cursor.offset
                previous_partial = cursor.partial
                fresh = handle.read(MAX_INGRESS_BYTES_PER_TICK)
        except OSError as exc:
            return SessionLogReadResult(
                configured=True,
                observed_at=observed_at,
                error=str(exc),
            )
        bytes_read = len(fresh)
        if cursor.skipping_oversize:
            newline = fresh.find(b"\n")
            if newline < 0:
                self._record_gap(cursor, fresh, cursor.gap_reason or "oversize_jsonl_record")
                cursor.offset = read_start + len(fresh)
                cursor.stat_size = stat.st_size
                cursor.mtime_ns = stat.st_mtime_ns
                refresh_anchor(path, cursor)
                backlog_bytes, backlog_age = self._backlog(cursor, stat.st_size, observed_at)
                return SessionLogReadResult(
                    configured=True,
                    readable=True,
                    observed_at=observed_at,
                    bytes_read=bytes_read,
                    consumed_bytes=len(fresh),
                    backlog_bytes=backlog_bytes,
                    backlog_records_lower_bound=int(bool(backlog_bytes)),
                    backlog_age_seconds=backlog_age,
                    budget_exceeded=bool(backlog_bytes),
                    oversize_record_count=cursor.oversize_records,
                    skipped_bytes=cursor.skipped_bytes,
                    gap_count=cursor.gap_count,
                    gap_reason=cursor.gap_reason,
                    gap_hash=cursor.gap_hash,
                    device=cursor.device,
                    inode=cursor.inode,
                    generation=cursor.generation,
                    anchor_hash=anchor_sha256(cursor.anchor),
                    stream_uncertain=cursor.stream_uncertain,
                    stream_uncertainty_count=cursor.stream_uncertainty_count,
                    stream_uncertainty_reason=cursor.stream_uncertainty_reason,
                )
            self._record_gap(
                cursor,
                fresh[: newline + 1],
                cursor.gap_reason or "oversize_jsonl_record",
            )
            cursor.skipping_oversize = False
            read_start += newline + 1
            fresh = fresh[newline + 1 :]
            previous_partial = b""
        payload = previous_partial + fresh
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            if len(payload) > MAX_JSONL_RECORD_BYTES:
                cursor.partial = b""
                cursor.skipping_oversize = True
                cursor.oversize_records += 1
                cursor.gap_count += 1
                self._record_gap(cursor, payload, "oversize_jsonl_record")
            else:
                cursor.partial = payload
            cursor.offset = read_start + len(fresh)
            cursor.stat_size = stat.st_size
            cursor.mtime_ns = stat.st_mtime_ns
            refresh_anchor(path, cursor)
            backlog_bytes, backlog_age = self._backlog(cursor, stat.st_size, observed_at)
            return SessionLogReadResult(
                configured=True,
                readable=True,
                observed_at=observed_at,
                bytes_read=bytes_read,
                consumed_bytes=len(fresh),
                backlog_bytes=backlog_bytes,
                backlog_records_lower_bound=int(bool(backlog_bytes)),
                backlog_age_seconds=backlog_age,
                budget_exceeded=bool(backlog_bytes),
                oversize_record_count=cursor.oversize_records,
                skipped_bytes=cursor.skipped_bytes,
                gap_count=cursor.gap_count,
                gap_reason=cursor.gap_reason,
                gap_hash=cursor.gap_hash,
                device=cursor.device,
                inode=cursor.inode,
                generation=cursor.generation,
                anchor_hash=anchor_sha256(cursor.anchor),
                stream_uncertain=cursor.stream_uncertain,
                stream_uncertainty_count=cursor.stream_uncertainty_count,
                stream_uncertainty_reason=cursor.stream_uncertainty_reason,
            )
        complete = payload[: last_newline + 1]
        trailing_partial = payload[last_newline + 1 :]
        events: list[tuple[str, NormalizedEvent]] = []
        offset = read_start - len(previous_partial)
        parse_started = time.monotonic()
        record_count = 0
        budget_exceeded = False
        for raw_line in complete.splitlines(keepends=True):
            if (
                record_count >= MAX_INGRESS_RECORDS_PER_TICK
                or time.monotonic() - parse_started >= MAX_INGRESS_PARSE_SECONDS
            ):
                budget_exceeded = True
                break
            source_id = stream_source_id(
                "tui-session-log",
                cursor.device,
                cursor.inode,
                cursor.generation,
                offset,
            )
            offset += len(raw_line)
            record_count += 1
            if len(raw_line) > MAX_JSONL_RECORD_BYTES:
                cursor.oversize_records += 1
                cursor.gap_count += 1
                self._record_gap(cursor, raw_line, "oversize_jsonl_record")
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            parsed = parse_session_log_record(
                record,
                source_id=source_id,
                observed_at=observed_at,
                default_session_id=default_session_id,
            )
            if parsed:
                session_id, event = parsed
                events.append(
                    (
                        session_id,
                        replace(
                            event,
                            metadata={
                                **event.metadata,
                                **stream_metadata(
                                    path,
                                    cursor.device,
                                    cursor.inode,
                                    cursor.generation,
                                    offset - len(raw_line),
                                    uncertain=cursor.stream_uncertain,
                                    uncertainty_reason=(cursor.stream_uncertainty_reason),
                                ),
                            },
                        ),
                    )
                )
        if budget_exceeded:
            cursor.offset = offset
            cursor.partial = b""
        else:
            cursor.offset = read_start + len(fresh)
            if len(trailing_partial) > MAX_JSONL_RECORD_BYTES:
                cursor.partial = b""
                cursor.skipping_oversize = True
                cursor.oversize_records += 1
                cursor.gap_count += 1
                self._record_gap(cursor, trailing_partial, "oversize_jsonl_record")
            else:
                cursor.partial = trailing_partial
        backlog_bytes, backlog_age = self._backlog(cursor, stat.st_size, observed_at)
        cursor.stat_size = stat.st_size
        cursor.mtime_ns = stat.st_mtime_ns
        refresh_anchor(path, cursor)
        budget_exceeded = budget_exceeded or bool(backlog_bytes)
        return SessionLogReadResult(
            tuple(events),
            configured=True,
            readable=True,
            observed_at=observed_at,
            last_event_at=max((event.timestamp for _, event in events), default=None),
            bytes_read=bytes_read,
            consumed_bytes=max(0, cursor.offset - read_start),
            record_count=record_count,
            backlog_bytes=backlog_bytes,
            backlog_records_lower_bound=(
                max(0, complete.count(b"\n") - record_count) + int(bool(trailing_partial))
                if backlog_bytes
                else 0
            ),
            backlog_age_seconds=backlog_age,
            budget_exceeded=budget_exceeded,
            oversize_record_count=cursor.oversize_records,
            skipped_bytes=cursor.skipped_bytes,
            gap_count=cursor.gap_count,
            gap_reason=cursor.gap_reason,
            gap_hash=cursor.gap_hash,
            parse_duration_seconds=time.monotonic() - parse_started,
            device=cursor.device,
            inode=cursor.inode,
            generation=cursor.generation,
            anchor_hash=anchor_sha256(cursor.anchor),
            stream_uncertain=cursor.stream_uncertain,
            stream_uncertainty_count=cursor.stream_uncertainty_count,
            stream_uncertainty_reason=cursor.stream_uncertainty_reason,
        )

    def prune(self, active_paths: set[str]) -> None:
        self.cursors = {
            path: cursor for path, cursor in self.cursors.items() if path in active_paths
        }

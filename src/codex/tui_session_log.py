"""Incrementally extract only outbound Compact operations from Codex TUI logs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex.events import parse_timestamp
from models import Confidence, NormalizedEvent


@dataclass
class SessionLogCursor:
    device: int
    inode: int
    offset: int = 0
    partial: bytes = b""


@dataclass(frozen=True)
class SessionLogReadResult:
    events: tuple[tuple[str, NormalizedEvent], ...] = ()
    configured: bool = False
    readable: bool = False
    observed_at: float | None = None
    last_event_at: float | None = None
    error: str = ""


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
        directions
        & {"from_tui", "outbound", "tui_to_core", "client_to_core", "request"}
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
        (
            record[key]
            for key in ("timestamp", "ts", "time")
            if key in record
        ),
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
        if (
            cursor is None
            or (cursor.device, cursor.inode) != (stat.st_dev, stat.st_ino)
            or stat.st_size < cursor.offset
        ):
            cursor = SessionLogCursor(stat.st_dev, stat.st_ino)
            self.cursors[key] = cursor
        try:
            with path.open("rb") as handle:
                handle.seek(cursor.offset)
                read_start = cursor.offset
                payload = cursor.partial + handle.read()
                cursor.offset = handle.tell()
        except OSError as exc:
            return SessionLogReadResult(
                configured=True,
                observed_at=observed_at,
                error=str(exc),
            )
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            cursor.partial = payload[-65536:]
            return SessionLogReadResult(
                configured=True,
                readable=True,
                observed_at=observed_at,
            )
        complete = payload[: last_newline + 1]
        cursor.partial = payload[last_newline + 1 :][-65536:]
        events: list[tuple[str, NormalizedEvent]] = []
        offset = read_start - (len(payload) - (cursor.offset - read_start))
        for raw_line in complete.splitlines(keepends=True):
            source_id = f"tui-session-log:{stat.st_ino}:{offset}"
            offset += len(raw_line)
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
                events.append(parsed)
        return SessionLogReadResult(
            tuple(events),
            configured=True,
            readable=True,
            observed_at=observed_at,
            last_event_at=max((event.timestamp for _, event in events), default=None),
        )

    def prune(self, active_paths: set[str]) -> None:
        self.cursors = {
            path: cursor for path, cursor in self.cursors.items() if path in active_paths
        }

"""Incrementally read actively written Codex JSONL rollout files."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from config import MAX_SESSION_TAIL
from models import NormalizedEvent
from utils import message_text
from .events import normalize_rollout_record


KNOWN_IGNORED_TYPES = {
    "event_msg:patch_apply_end",
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


class RolloutReader:
    def __init__(self) -> None:
        self.cursors: dict[str, RolloutCursor] = {}
        self.unknown_types: dict[str, Counter[str]] = {}
        self.bootstrap_truncated: set[str] = set()

    def read(self, path: Path) -> list[NormalizedEvent]:
        try:
            stat = path.stat()
        except OSError:
            return []
        key = str(path)
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
            cursor = RolloutCursor(stat.st_dev, stat.st_ino, start)
            self.cursors[key] = cursor
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
                        cursor = RolloutCursor(stat.st_dev, stat.st_ino, start)
                        self.cursors[key] = cursor
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
                payload = previous_partial + handle.read()
                cursor.offset = handle.tell()
                anchor_start = max(0, cursor.offset - 64)
                handle.seek(anchor_start)
                cursor.anchor = handle.read(cursor.offset - anchor_start)
        except OSError:
            return []
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            cursor.partial = payload
            return []
        complete = payload[: last_newline + 1]
        cursor.partial = payload[last_newline + 1 :]
        events: list[NormalizedEvent] = []
        base_offset = read_start - len(previous_partial)
        position = base_offset
        for raw_line in complete.splitlines(keepends=True):
            line_offset = position
            position += len(raw_line)
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                source_id = f"rollout:{stat.st_ino}:{line_offset}"
                normalized = normalize_rollout_record(record, source_id)
                events.extend(normalized)
                if not normalized and record.get("type") in {
                    "event_msg",
                    "response_item",
                }:
                    item = record.get("payload")
                    item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
                    if item_type:
                        event_type = f"{record['type']}:{item_type}"
                        if event_type not in KNOWN_IGNORED_TYPES:
                            self.unknown_types.setdefault(key, Counter())[event_type] += 1
        return events

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

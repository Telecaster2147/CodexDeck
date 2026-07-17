"""Minimal compact hook receiver and incremental event reader."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from codex.events import parse_timestamp
from models import Confidence, FailureInfo, NormalizedEvent


ALLOWED_HOOKS = {"precompact", "postcompact"}


def sanitize_hook_payload(payload: dict[str, Any], now: float | None = None) -> dict[str, object]:
    event = str(
        payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("name")
        or payload.get("type")
        or ""
    )
    normalized = event.replace("_", "").replace("-", "").lower()
    if normalized not in ALLOWED_HOOKS:
        raise ValueError("仅接受 PreCompact 或 PostCompact hook")
    trigger = str(payload.get("trigger") or payload.get("matcher") or "unknown").lower()
    if trigger not in {"manual", "auto"}:
        trigger = "unknown"
    outcome = str(payload.get("outcome") or payload.get("result") or "success").lower()
    if bool(payload.get("error")):
        outcome = "failed"
    if bool(payload.get("aborted")) or bool(payload.get("stopped")):
        outcome = "aborted"
    timestamp = parse_timestamp(payload.get("timestamp")) or (now or time.time())
    return {
        "timestamp": timestamp,
        "event": "PreCompact" if normalized == "precompact" else "PostCompact",
        "session_id": str(payload.get("session_id") or payload.get("thread_id") or ""),
        "turn_id": str(payload.get("turn_id") or ""),
        "trigger": trigger,
        "outcome": outcome,
    }


def receive_hook_event(path: Path, stream: TextIO) -> None:
    payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("hook payload 必须是 JSON object")
    sanitized = sanitize_hook_payload(payload)
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitized, ensure_ascii=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line.encode("ascii"))
    finally:
        os.close(descriptor)


@dataclass
class HookCursor:
    device: int
    inode: int
    offset: int = 0
    partial: bytes = b""


class HookEventReader:
    def __init__(self, path: Path | None) -> None:
        self.path = path.expanduser().resolve(strict=False) if path else None
        self.cursor: HookCursor | None = None
        self.last_probe_at: float | None = None
        self.last_event_at: float | None = None
        self.error = ""

    @property
    def configured(self) -> bool:
        return self.path is not None

    def read(self) -> list[tuple[str, NormalizedEvent]]:
        self.last_probe_at = time.time()
        if self.path is None:
            return []
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.error = "hook event 文件尚未创建"
            return []
        except OSError as exc:
            self.error = str(exc)
            return []
        if (
            self.cursor is None
            or (self.cursor.device, self.cursor.inode) != (stat.st_dev, stat.st_ino)
            or stat.st_size < self.cursor.offset
        ):
            self.cursor = HookCursor(stat.st_dev, stat.st_ino)
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.cursor.offset)
                payload = self.cursor.partial + handle.read()
                self.cursor.offset = handle.tell()
        except OSError as exc:
            self.error = str(exc)
            return []
        self.error = ""
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            self.cursor.partial = payload[-65536:]
            return []
        complete = payload[: last_newline + 1]
        self.cursor.partial = payload[last_newline + 1 :][-65536:]
        results: list[tuple[str, NormalizedEvent]] = []
        for index, raw_line in enumerate(complete.splitlines()):
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_name = str(payload.get("event") or "")
            session_id = str(payload.get("session_id") or "")
            turn_id = str(payload.get("turn_id") or "")
            trigger = str(payload.get("trigger") or "unknown")
            outcome = str(payload.get("outcome") or "success")
            timestamp = parse_timestamp(payload.get("timestamp")) or self.last_probe_at
            kind = "COMPACTING"
            summary = "compact 已开始"
            failure = None
            if event_name == "PostCompact":
                if outcome in {"failed", "error"}:
                    kind = "COMPACT_FAILED"
                    summary = "compact 失败"
                    failure = FailureInfo(
                        "compact_error",
                        summary,
                        turn_id=turn_id,
                        timestamp=timestamp,
                        source="compact_hook",
                    )
                elif outcome in {"aborted", "stopped", "stop"}:
                    kind = "COMPACT_ABORTED"
                    summary = "compact 已中止"
                else:
                    kind = "COMPACT_COMPLETED"
                    summary = "compact 已完成"
            elif event_name != "PreCompact":
                continue
            source_id = f"compact-hook:{stat.st_ino}:{index}:{timestamp}"
            event = NormalizedEvent(
                timestamp=timestamp,
                kind=kind,
                summary=summary,
                source="compact_hook",
                confidence=Confidence.HIGH,
                turn_id=turn_id,
                source_id=source_id,
                failure=failure,
                observed_at=self.last_probe_at,
                metadata={"trigger": trigger, "hook_event": event_name, "outcome": outcome},
            )
            results.append((session_id, event))
            self.last_event_at = max(self.last_event_at or timestamp, timestamp)
        return results

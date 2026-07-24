"""Minimal compact hook receiver and incremental event reader."""

from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from codex.events import parse_timestamp
from codex.ingress import (
    MAX_INGRESS_BYTES_PER_TICK,
    MAX_INGRESS_PARSE_SECONDS,
    MAX_INGRESS_RECORDS_PER_TICK,
    MAX_JSONL_RECORD_BYTES,
)
from codex.mutable_stream import (
    STREAM_ANCHOR_BYTES,
    anchor_matches,
    anchor_sha256,
    stream_metadata,
    stream_source_id,
)
from models import Confidence, FailureInfo, NormalizedEvent
from utils import PrivateFileError, open_private_regular_file


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
    line = json.dumps(sanitized, ensure_ascii=True, separators=(",", ":")) + "\n"
    private_file = open_private_regular_file(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
    try:
        os.write(private_file.descriptor, line.encode("ascii"))
        private_file.verify_path()
    finally:
        private_file.close()


@dataclass
class HookCursor:
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


class HookEventReader:
    def __init__(self, path: Path | None) -> None:
        self.path = (
            path.expanduser().parent.resolve(strict=False) / path.expanduser().name
            if path
            else None
        )
        self.cursor: HookCursor | None = None
        self.last_probe_at: float | None = None
        self.last_event_at: float | None = None
        self.error = ""
        self.bytes_read = 0
        self.consumed_bytes = 0
        self.record_count = 0
        self.backlog_bytes = 0
        self.backlog_records_lower_bound = 0
        self.backlog_age_seconds: float | None = None
        self.budget_exceeded = False
        self.parse_duration_seconds = 0.0

    def _refresh_anchor(self) -> None:
        if self.path is None or self.cursor is None:
            return
        try:
            private_file = open_private_regular_file(
                self.path,
                os.O_RDONLY,
                create_parent=False,
                tighten_mode=False,
            )
            try:
                start = max(0, self.cursor.offset - STREAM_ANCHOR_BYTES)
                self.cursor.anchor = os.pread(
                    private_file.descriptor,
                    self.cursor.offset - start,
                    start,
                )
                private_file.verify_path()
            finally:
                private_file.close()
        except (OSError, PrivateFileError):
            self.cursor.anchor = b""

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def anchor_hash(self) -> str:
        return anchor_sha256(self.cursor.anchor) if self.cursor is not None else ""

    def _record_gap(self, payload: bytes, reason: str) -> None:
        if self.cursor is None:
            return
        self.cursor.skipped_bytes += len(payload)
        self.cursor.gap_reason = reason
        self.cursor.gap_hash = hashlib.sha256(
            self.cursor.gap_hash.encode("ascii") + payload[:MAX_JSONL_RECORD_BYTES]
        ).hexdigest()

    def _update_backlog(self, stat_size: int) -> None:
        if self.cursor is None or self.last_probe_at is None:
            return
        self.backlog_bytes = max(0, stat_size - self.cursor.offset)
        if self.backlog_bytes:
            self.cursor.backlog_since = self.cursor.backlog_since or self.last_probe_at
        else:
            self.cursor.backlog_since = None
        self.backlog_age_seconds = (
            max(0.0, self.last_probe_at - self.cursor.backlog_since)
            if self.cursor.backlog_since is not None
            else None
        )

    def read(self) -> list[tuple[str, NormalizedEvent]]:
        self.last_probe_at = time.time()
        self.bytes_read = 0
        self.consumed_bytes = 0
        self.record_count = 0
        self.backlog_bytes = 0
        self.backlog_records_lower_bound = 0
        self.backlog_age_seconds = None
        self.budget_exceeded = False
        self.parse_duration_seconds = 0.0
        if self.path is None:
            return []
        try:
            private_file = open_private_regular_file(
                self.path,
                os.O_RDONLY,
                create_parent=False,
                tighten_mode=False,
            )
        except PrivateFileError as exc:
            self.error = str(exc)
            return []
        file_stat = os.fstat(private_file.descriptor)
        replaced = self.cursor is not None and (
            self.cursor.device,
            self.cursor.inode,
        ) != (file_stat.st_dev, file_stat.st_ino)
        truncated = self.cursor is not None and file_stat.st_size < self.cursor.offset
        if self.cursor is None or replaced or truncated:
            generation = self.cursor.generation + 1 if self.cursor is not None else 0
            uncertainty_count = (
                self.cursor.stream_uncertainty_count if self.cursor is not None else 0
            )
            self.cursor = HookCursor(
                file_stat.st_dev,
                file_stat.st_ino,
                generation=generation,
                stat_size=file_stat.st_size,
                mtime_ns=file_stat.st_mtime_ns,
                stream_uncertain=bool(truncated),
                stream_uncertainty_count=uncertainty_count + int(truncated),
                stream_uncertainty_reason="truncated" if truncated else "",
            )
        try:
            with os.fdopen(os.dup(private_file.descriptor), "rb") as handle:
                if not anchor_matches(handle, self.cursor):
                    self.cursor = HookCursor(
                        file_stat.st_dev,
                        file_stat.st_ino,
                        generation=self.cursor.generation + 1,
                        stat_size=file_stat.st_size,
                        mtime_ns=file_stat.st_mtime_ns,
                        stream_uncertain=True,
                        stream_uncertainty_count=(self.cursor.stream_uncertainty_count + 1),
                        stream_uncertainty_reason="content_anchor_mismatch",
                    )
                elif (
                    self.cursor.offset
                    and file_stat.st_size == self.cursor.stat_size
                    and file_stat.st_mtime_ns != self.cursor.mtime_ns
                ):
                    self.cursor.stream_uncertain = True
                    self.cursor.stream_uncertainty_count += 1
                    self.cursor.stream_uncertainty_reason = (
                        "same_size_mtime_change_anchor_unchanged"
                    )
                handle.seek(self.cursor.offset)
                read_start = self.cursor.offset
                previous_partial = self.cursor.partial
                fresh = handle.read(MAX_INGRESS_BYTES_PER_TICK)
            private_file.verify_path()
        except (OSError, PrivateFileError) as exc:
            self.error = str(exc)
            return []
        finally:
            private_file.close()
        self.error = ""
        self.bytes_read = len(fresh)
        if self.cursor.skipping_oversize:
            newline = fresh.find(b"\n")
            if newline < 0:
                self._record_gap(fresh, self.cursor.gap_reason or "oversize_jsonl_record")
                self.cursor.offset = read_start + len(fresh)
                self.cursor.stat_size = file_stat.st_size
                self.cursor.mtime_ns = file_stat.st_mtime_ns
                self._refresh_anchor()
                self.consumed_bytes = len(fresh)
                self._update_backlog(file_stat.st_size)
                self.backlog_records_lower_bound = int(bool(self.backlog_bytes))
                self.budget_exceeded = bool(self.backlog_bytes)
                return []
            self._record_gap(
                fresh[: newline + 1],
                self.cursor.gap_reason or "oversize_jsonl_record",
            )
            self.cursor.skipping_oversize = False
            read_start += newline + 1
            fresh = fresh[newline + 1 :]
            previous_partial = b""
        payload = previous_partial + fresh
        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            if len(payload) > MAX_JSONL_RECORD_BYTES:
                self.cursor.partial = b""
                self.cursor.skipping_oversize = True
                self.cursor.oversize_records += 1
                self.cursor.gap_count += 1
                self._record_gap(payload, "oversize_jsonl_record")
            else:
                self.cursor.partial = payload
            self.cursor.offset = read_start + len(fresh)
            self.cursor.stat_size = file_stat.st_size
            self.cursor.mtime_ns = file_stat.st_mtime_ns
            self._refresh_anchor()
            self.consumed_bytes = len(fresh)
            self._update_backlog(file_stat.st_size)
            self.backlog_records_lower_bound = int(bool(self.backlog_bytes))
            self.budget_exceeded = bool(self.backlog_bytes)
            return []
        complete = payload[: last_newline + 1]
        trailing_partial = payload[last_newline + 1 :]
        results: list[tuple[str, NormalizedEvent]] = []
        offset = read_start - len(previous_partial)
        parse_started = time.monotonic()
        budget_exceeded = False
        self.record_count = 0
        for raw_line in complete.splitlines(keepends=True):
            if (
                self.record_count >= MAX_INGRESS_RECORDS_PER_TICK
                or time.monotonic() - parse_started >= MAX_INGRESS_PARSE_SECONDS
            ):
                budget_exceeded = True
                break
            line_offset = offset
            offset += len(raw_line)
            self.record_count += 1
            if len(raw_line) > MAX_JSONL_RECORD_BYTES:
                self.cursor.oversize_records += 1
                self.cursor.gap_count += 1
                self._record_gap(raw_line, "oversize_jsonl_record")
                continue
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
            source_id = stream_source_id(
                "compact-hook",
                self.cursor.device,
                self.cursor.inode,
                self.cursor.generation,
                line_offset,
            )
            event = NormalizedEvent(
                timestamp=timestamp,
                kind=kind,
                summary=summary,
                source="compact_hook",
                confidence=Confidence.MEDIUM,
                turn_id=turn_id,
                source_id=source_id,
                failure=failure,
                observed_at=self.last_probe_at,
                parse_validity=Confidence.HIGH,
                source_authenticity=Confidence.LOW,
                identity_binding=Confidence.LOW,
                semantic_confidence=Confidence.MEDIUM,
                binding_evidence=(
                    "private_regular_file",
                    "current_user_owner",
                    "whitelisted_schema",
                    "producer_not_authenticated",
                ),
                metadata={
                    "trigger": trigger,
                    "hook_event": event_name,
                    "outcome": outcome,
                    **stream_metadata(
                        self.path,
                        self.cursor.device,
                        self.cursor.inode,
                        self.cursor.generation,
                        line_offset,
                        uncertain=self.cursor.stream_uncertain,
                        uncertainty_reason=self.cursor.stream_uncertainty_reason,
                    ),
                },
            )
            results.append((session_id, event))
            self.last_event_at = max(self.last_event_at or timestamp, timestamp)
        if budget_exceeded:
            self.cursor.offset = offset
            self.cursor.partial = b""
        else:
            self.cursor.offset = read_start + len(fresh)
            if len(trailing_partial) > MAX_JSONL_RECORD_BYTES:
                self.cursor.partial = b""
                self.cursor.skipping_oversize = True
                self.cursor.oversize_records += 1
                self.cursor.gap_count += 1
                self._record_gap(trailing_partial, "oversize_jsonl_record")
            else:
                self.cursor.partial = trailing_partial
        self.consumed_bytes = max(0, self.cursor.offset - read_start)
        self.cursor.stat_size = file_stat.st_size
        self.cursor.mtime_ns = file_stat.st_mtime_ns
        self._refresh_anchor()
        self._update_backlog(file_stat.st_size)
        self.backlog_records_lower_bound = (
            max(0, complete.count(b"\n") - self.record_count) + int(bool(trailing_partial))
            if self.backlog_bytes
            else 0
        )
        self.budget_exceeded = budget_exceeded or bool(self.backlog_bytes)
        self.parse_duration_seconds = time.monotonic() - parse_started
        return results

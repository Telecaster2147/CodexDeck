"""Codex rollout, structured-log, and SSE activity tracking."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .config import (
    ACTIVITY_BOOTSTRAP_LOOKBACK,
    ACTIVITY_LABELS,
    ALERT_HTTP_RESPONSE,
    ALERT_KEEPALIVE_ONLY,
    ALERT_POST_TOOL,
    ALERT_PRE_REQUEST,
    ALERT_THRESHOLDS,
    DEFAULT_EVENT_LOOKBACK,
    LOG_DB,
    MAX_SESSION_TAIL,
)
from .models import ActivityEvent, ConversationActivity, ProcessInfo, SseEvent, SseHealth
from .utils import format_duration, message_text


def parse_log_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def redact_sensitive(text: str) -> str:
    if not text:
        return text
    redacted = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        text,
    )
    redacted = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password|passwd)"
        r"([\s\"']*[:=][\s\"']*)([^\s\"',;}]+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", redacted)
    return redacted


def event_detail(value: object, limit: int = 240) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            value = str(value)
    compact = " ".join(redact_sensitive(value).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class ActivityTracker:
    """Build per-session activity timelines from rollout files and Codex logs."""

    RAW_SSE_TARGET = "codex_api::sse::responses"
    MEANINGFUL_KINDS = {
        "REASONING",
        "MESSAGE",
        "TOOL_BUILD",
        "TOOL_CALL",
        "TOOL_DONE",
        "RESPONSE_DONE",
        "TASK_DONE",
        "COMPACT_START",
        "COMPACT_DONE",
        "COMPACT_FAIL",
        "INTERRUPT",
    }

    def __init__(self, lookback_seconds: float = DEFAULT_EVENT_LOOKBACK) -> None:
        self.lookback_seconds = int(lookback_seconds)
        self.log_cursor: dict[int, int] = {}
        self.rollout_offsets: dict[str, int] = {}
        self.events: dict[str, list[ActivityEvent]] = {}
        self.previous_alerts: dict[str, tuple[str, float]] = {}
        self.error = ""

    @staticmethod
    def _event(timestamp: float, kind: str, detail: str = "", source: str = "codex") -> ActivityEvent:
        return ActivityEvent(
            timestamp=timestamp,
            kind=kind,
            summary=ACTIVITY_LABELS.get(kind, kind),
            detail=event_detail(detail),
            source=source,
        )

    def _append(self, session_id: str, event: ActivityEvent) -> None:
        if not session_id or event.timestamp <= 0:
            return
        bucket = self.events.setdefault(session_id, [])
        fingerprint = (event.timestamp, event.kind, event.detail)
        if any((item.timestamp, item.kind, item.detail) == fingerprint for item in bucket[-12:]):
            return
        bucket.append(event)
        bucket.sort(key=lambda item: item.timestamp)

    @staticmethod
    def _rollout_event(record: dict[str, object]) -> ActivityEvent | None:
        timestamp = parse_log_timestamp(str(record.get("timestamp") or ""))
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        item_type = str(payload.get("type") or "")
        if record_type == "event_msg":
            if item_type in {"task_started", "turn_started"}:
                return ActivityTracker._event(timestamp, "TASK_START", source="rollout")
            if item_type in {"task_complete", "turn_complete"}:
                return ActivityTracker._event(timestamp, "TASK_DONE", source="rollout")
            if item_type in {"turn_aborted", "task_aborted"}:
                return ActivityTracker._event(timestamp, "INTERRUPT", source="rollout")
            if item_type == "agent_message":
                detail = str(payload.get("message") or "")
                return (
                    ActivityTracker._event(timestamp, "MESSAGE", detail, "rollout")
                    if detail
                    else None
                )
            return None
        if record_type != "response_item":
            return None
        if item_type == "reasoning":
            return ActivityTracker._event(timestamp, "REASONING", source="rollout")
        if item_type == "message" and payload.get("role") == "assistant":
            detail = message_text(payload)
            return (
                ActivityTracker._event(timestamp, "MESSAGE", detail, "rollout")
                if detail
                else None
            )
        if item_type in {"custom_tool_call", "function_call"}:
            name = str(payload.get("name") or "tool")
            return ActivityTracker._event(timestamp, "TOOL_CALL", name, "rollout")
        if item_type in {"custom_tool_call_output", "function_call_output"}:
            return ActivityTracker._event(timestamp, "TOOL_DONE", source="rollout")
        if item_type in {"compaction", "context_compaction", "compacted"}:
            return ActivityTracker._event(timestamp, "COMPACT_DONE", source="rollout")
        return None

    def _read_rollout(self, process: ProcessInfo) -> None:
        path_string = process.rollout_path
        if not path_string:
            return
        path = Path(path_string)
        try:
            size = path.stat().st_size
            known = self.rollout_offsets.get(path_string)
            with path.open("rb") as handle:
                if known is None:
                    start = max(0, size - MAX_SESSION_TAIL)
                    handle.seek(start)
                    if start:
                        handle.readline()
                elif known <= size:
                    handle.seek(known)
                else:
                    handle.seek(0)
                payload = handle.read().decode("utf-8", errors="replace")
            self.rollout_offsets[path_string] = size
        except OSError:
            return
        for line in payload.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = self._rollout_event(record)
            if event:
                self._append(process.session_id, event)

    @staticmethod
    def _sse_event(timestamp: float, body: str) -> ActivityEvent | None:
        marker = "SSE event: "
        if marker not in body:
            return None
        encoded = body.split(marker, 1)[1]
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            payload = {}
        type_match = re.search(r'^\s*\{"type":"([^"]+)"', encoded)
        event_type = str(payload.get("type") or (type_match.group(1) if type_match else ""))
        if event_type == "keepalive":
            return ActivityTracker._event(timestamp, "KEEPALIVE", source="sse")
        if event_type == "response.created":
            return ActivityTracker._event(timestamp, "RESPONSE_STARTED", source="sse")
        if event_type == "response.completed":
            return ActivityTracker._event(timestamp, "RESPONSE_DONE", source="sse")
        if event_type in {"response.failed", "response.incomplete"}:
            return ActivityTracker._event(timestamp, "RESPONSE_FAIL", event_type, "sse")
        if event_type == "response.output_item.added":
            item = payload.get("item") or {}
            item_type = item.get("type") if isinstance(item, dict) else ""
            if item_type == "reasoning":
                return ActivityTracker._event(timestamp, "REASONING", source="sse")
            if item_type in {"custom_tool_call", "function_call"}:
                name = str(item.get("name") or "tool") if isinstance(item, dict) else "tool"
                return ActivityTracker._event(timestamp, "TOOL_BUILD", name, "sse")
            if item_type == "message":
                return ActivityTracker._event(timestamp, "MESSAGE", source="sse")
        return None

    @staticmethod
    def _structured_log_event(timestamp: float, level: str, target: str, body: str) -> ActivityEvent | None:
        lowered = body.lower()
        if target == "codex_http_client::transport" and " post to " in lowered and "/responses" in lowered:
            if "run_auto_compact{" in body or "run_remote_compact" in lowered:
                mode = "自动" if "run_auto_compact{" in body else "手动"
                phase_match = re.search(r"phase=([A-Za-z]+)", body)
                phase = phase_match.group(1) if phase_match else ""
                return ActivityTracker._event(
                    timestamp, "COMPACT_START", f"{mode}压缩 {phase}".strip(), "log"
                )
            return ActivityTracker._event(timestamp, "HTTP_POST", source="log")
        if target == "codex_core::responses_retry" and "idle timeout waiting for sse" in lowered:
            return ActivityTracker._event(timestamp, "SSE_TIMEOUT", source="log")
        if target == "codex_core::responses_retry" and "run_auto_compact" in lowered:
            return ActivityTracker._event(timestamp, "COMPACT_FAIL", "自动压缩流重试", "log")
        if target == "codex_core::tasks" and "aborting running task" in lowered:
            return ActivityTracker._event(timestamp, "INTERRUPT", source="log")
        if target == "codex_core::session::turn" and "post sampling token usage" in lowered:
            match = re.search(
                r"total_usage_tokens=(\d+).*?auto_compact_scope_limit=Some\((\d+)\)", body
            )
            if match:
                return ActivityTracker._event(
                    timestamp, "TOKEN_USAGE", f"{match.group(1)}/{match.group(2)}", "log"
                )
        return None

    def _read_logs(self, process: ProcessInfo) -> None:
        if not LOG_DB.exists() or not process.session_id:
            return
        cursor = self.log_cursor.get(process.pid, 0)
        cutoff = int(time.time()) - ACTIVITY_BOOTSTRAP_LOOKBACK
        raw_patterns = (
            '%"type":"keepalive"%',
            '%"type":"response.created"%',
            '%"type":"response.completed"%',
            '%"type":"response.failed"%',
            '%"type":"response.incomplete"%',
            '%"type":"response.output_item.added"%',
        )
        base = (
            "SELECT id, ts + ts_nanos / 1000000000.0, level, target, "
            "substr(coalesce(feedback_log_body, ''), 1, 8192) FROM logs "
            "WHERE process_uuid LIKE ? AND "
        )
        scope = (
            "(thread_id = ? OR target = 'codex_api::sse::responses' OR "
            "target = 'codex_core::responses_retry' OR target = 'codex_core::tasks' OR "
            "target = 'codex_http_client::transport' OR "
            "target = 'codex_core::session::turn') AND "
            "(target != 'codex_api::sse::responses' OR "
            + " OR ".join("feedback_log_body LIKE ?" for _ in raw_patterns)
            + ") "
        )
        if cursor:
            query = base + "id > ? AND " + scope + "ORDER BY id LIMIT 20000"
            params: tuple[object, ...] = (
                f"pid:{process.pid}:%",
                cursor,
                process.session_id,
                *raw_patterns,
            )
        else:
            query = base + "ts >= ? AND " + scope + "ORDER BY id LIMIT 20000"
            params = (
                f"pid:{process.pid}:%",
                cutoff,
                process.session_id,
                *raw_patterns,
            )
        try:
            connection = sqlite3.connect(f"file:{LOG_DB}?mode=ro", uri=True, timeout=0.25)
            try:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(query, params).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self.error = f"读取活动日志失败：{exc}"
            return
        if rows:
            self.log_cursor[process.pid] = int(rows[-1][0])
        for _, timestamp, level, target, body in rows:
            if target == self.RAW_SSE_TARGET:
                event = self._sse_event(float(timestamp), str(body))
            else:
                event = self._structured_log_event(
                    float(timestamp), str(level), str(target), str(body)
                )
            if event:
                self._append(process.session_id, event)

    @staticmethod
    def _latest(events: Sequence[ActivityEvent], *kinds: str) -> ActivityEvent | None:
        wanted = set(kinds)
        return next((event for event in reversed(events) if event.kind in wanted), None)

    def _derive(self, process: ProcessInfo) -> ConversationActivity:
        now = time.time()
        cutoff = now - max(self.lookback_seconds, 24 * 60 * 60)
        all_events = [event for event in self.events.get(process.session_id, []) if event.timestamp >= cutoff]
        self.events[process.session_id] = all_events[-2000:]
        visible_cutoff = now - self.lookback_seconds
        visible = [event for event in all_events if event.timestamp >= visible_cutoff]
        state = ConversationActivity(process.session_id, process.pid, events=visible)
        if not all_events:
            return state

        last_event = all_events[-1]
        phase_event = self._latest(all_events, *ACTIVITY_LABELS.keys()) or last_event
        state.phase = ACTIVITY_LABELS.get(phase_event.kind, phase_event.summary)
        state.phase_since = phase_event.timestamp
        meaningful = [event for event in all_events if event.kind in self.MEANINGFUL_KINDS]
        if meaningful:
            state.last_meaningful_at = meaningful[-1].timestamp
        keepalive = self._latest(all_events, "KEEPALIVE")
        state.last_keepalive_at = keepalive.timestamp if keepalive else None

        token_event = self._latest(all_events, "TOKEN_USAGE")
        if token_event and "/" in token_event.detail:
            used, limit = token_event.detail.split("/", 1)
            state.token_used, state.token_limit = int(used), int(limit)

        compact_start = self._latest(all_events, "COMPACT_START")
        compact_end = self._latest(
            all_events, "COMPACT_DONE", "COMPACT_FAIL", "RESPONSE_DONE", "RESPONSE_FAIL"
        )
        if compact_start and (not compact_end or compact_start.timestamp > compact_end.timestamp):
            state.compacting = True
            state.compact_age_seconds = max(0, int(now - compact_start.timestamp))
            state.compact_mode = "自动" if "自动" in compact_start.detail else "手动"
            if "MidTurn" in compact_start.detail:
                state.compact_phase = "mid-turn"
            elif "PreTurn" in compact_start.detail:
                state.compact_phase = "pre-turn"
            else:
                state.compact_phase = "standalone"
            state.phase = ACTIVITY_LABELS["COMPACT_START"]
            state.phase_since = compact_start.timestamp
        elif compact_start and compact_end:
            state.compact_result = (
                "失败" if compact_end.kind in {"COMPACT_FAIL", "RESPONSE_FAIL"} else "完成"
            )

        task_start = self._latest(all_events, "TASK_START")
        task_end = self._latest(all_events, "TASK_DONE", "INTERRUPT")
        http_post = self._latest(all_events, "HTTP_POST")
        response_start = self._latest(all_events, "RESPONSE_STARTED")
        response_done = self._latest(all_events, "RESPONSE_DONE")
        tool_done = self._latest(all_events, "TOOL_DONE")
        semantic_after_response = None
        if response_start:
            semantic_after_response = next(
                (
                    event
                    for event in reversed(all_events)
                    if event.timestamp > response_start.timestamp
                    and event.kind in {"REASONING", "MESSAGE", "TOOL_BUILD", "TOOL_CALL", "RESPONSE_DONE"}
                ),
                None,
            )

        downstream_after_post = None
        if http_post:
            downstream_after_post = next(
                (
                    event
                    for event in reversed(all_events)
                    if event.timestamp > http_post.timestamp
                    and event.kind
                    in {
                        "RESPONSE_STARTED",
                        "REASONING",
                        "MESSAGE",
                        "TOOL_BUILD",
                        "TOOL_CALL",
                        "TOOL_DONE",
                        "RESPONSE_DONE",
                        "TASK_DONE",
                        "INTERRUPT",
                    }
                ),
                None,
            )

        alert = ""
        since = 0.0
        reason = ""
        if (
            http_post
            and not downstream_after_post
            and (not response_start or response_start.timestamp < http_post.timestamp)
            and (not response_done or response_done.timestamp < http_post.timestamp)
            and (not task_end or task_end.timestamp < http_post.timestamp)
        ):
            alert, since = ALERT_HTTP_RESPONSE, http_post.timestamp
            reason = "POST /responses 已发出，但尚未收到 HTTP 响应头或 response.created"
        elif task_start and (not task_end or task_start.timestamp > task_end.timestamp) and (
            not http_post or http_post.timestamp < task_start.timestamp
        ):
            alert, since = ALERT_PRE_REQUEST, task_start.timestamp
            reason = "turn 已启动，但尚未进入模型请求传输阶段"
        elif (
            tool_done
            and response_start
            and response_start.timestamp > tool_done.timestamp
            and (not task_start or tool_done.timestamp > task_start.timestamp)
            and not semantic_after_response
        ):
            alert, since = ALERT_POST_TOOL, response_start.timestamp
            reason = "工具结果已返回，上游接收了后续请求，但没有产生实际模型输出"
        elif response_start and (not response_done or response_done.timestamp < response_start.timestamp):
            last_semantic = semantic_after_response or response_start
            if keepalive and keepalive.timestamp > last_semantic.timestamp:
                alert, since = ALERT_KEEPALIVE_ONLY, last_semantic.timestamp
                reason = "SSE 仍有 keepalive，但没有 reasoning、正文或工具事件"

        if alert:
            age = max(0, int(now - since))
            warn, critical = ALERT_THRESHOLDS[alert]
            if age >= warn:
                state.alert = alert
                state.alert_level = "严重" if age >= critical else "警告"
                state.alert_age_seconds = age
                state.alert_reason = reason

        previous = self.previous_alerts.get(process.session_id)
        current_key = state.alert
        if state.alert and (not previous or previous[0] != state.alert):
            self._append(
                process.session_id,
                self._event(now, "ALERT", f"{state.alert}：{state.alert_reason}", "detector"),
            )
            self.previous_alerts[process.session_id] = (state.alert, now)
        elif not state.alert and previous:
            duration = max(0, int(now - previous[1]))
            self._append(
                process.session_id,
                ActivityEvent(
                    now,
                    "RECOVERED",
                    "告警已恢复",
                    f"{previous[0]}，持续 {format_duration(duration)}",
                    "detector",
                ),
            )
            del self.previous_alerts[process.session_id]
        state.events = [
            event
            for event in self.events.get(process.session_id, [])
            if event.timestamp >= visible_cutoff
        ]
        return state

    def update(self, processes: Sequence[ProcessInfo]) -> dict[str, ConversationActivity]:
        active = [
            process
            for process in processes
            if process.role == "session" and process.session_id
        ]
        for process in active:
            self._read_rollout(process)
            self._read_logs(process)
        return {process.session_id: self._derive(process) for process in active}


class SseHealthTracker:
    """Track recent compaction-stream failures without repeatedly scanning the log DB."""

    def __init__(self, lookback_seconds: float) -> None:
        self.lookback_seconds = int(lookback_seconds)
        self.last_log_id = 0
        self.events: list[SseEvent] = []
        self.error = ""

    @staticmethod
    def classify_event(level: str, target: str, body: str) -> str | None:
        lowered = body.lower()
        # Codex logs user-visible messages and tool output in other targets. Match
        # only structured runtime targets so quoted error text never becomes an alert.
        if (
            target == "codex_core::responses_retry"
            and "idle timeout waiting for sse" in lowered
        ):
            return "idle_timeout"
        if target == "codex_core::responses_retry" and "run_auto_compact" in lowered:
            return "auto_compact"
        if (
            level.upper() == "ERROR"
            and target == "codex_api::endpoint::responses_websocket"
            and "wss://api.openai.com/v1/responses" in lowered
            and "401 unauthorized" in lowered
        ):
            return "websocket_401"
        return None

    def read_new_events(self) -> None:
        if not LOG_DB.exists():
            self.error = f"日志库不存在：{LOG_DB}"
            return
        now = int(time.time())
        cutoff = now - self.lookback_seconds
        try:
            connection = sqlite3.connect(f"file:{LOG_DB}?mode=ro", uri=True, timeout=0.25)
            try:
                connection.execute("PRAGMA query_only=ON")
                newest_id = int(connection.execute("SELECT coalesce(max(id), 0) FROM logs").fetchone()[0])
                if newest_id < self.last_log_id:
                    self.last_log_id = 0
                    self.events.clear()
                if self.last_log_id:
                    query = (
                        "SELECT id, ts, coalesce(thread_id, ''), level, target, "
                        "coalesce(feedback_log_body, '') "
                        "FROM logs WHERE id > ? AND target IN "
                        "('codex_core::responses_retry', "
                        "'codex_api::endpoint::responses_websocket') ORDER BY id"
                    )
                    rows = connection.execute(query, (self.last_log_id,)).fetchall()
                else:
                    query = (
                        "SELECT id, ts, coalesce(thread_id, ''), level, target, "
                        "coalesce(feedback_log_body, '') "
                        "FROM logs WHERE ts >= ? AND target IN "
                        "('codex_core::responses_retry', "
                        "'codex_api::endpoint::responses_websocket') ORDER BY id"
                    )
                    rows = connection.execute(query, (cutoff,)).fetchall()
                self.last_log_id = newest_id
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self.error = f"读取 Codex 日志失败：{exc}"
            return

        self.error = ""
        for log_id, timestamp, thread_id, level, target, body in rows:
            kind = self.classify_event(str(level), str(target), str(body))
            if kind:
                self.events.append(
                    SseEvent(int(log_id), int(timestamp), str(thread_id), kind)
                )
        self.events = [event for event in self.events if event.timestamp >= cutoff]

    def health(self, active_session_ids: set[str]) -> SseHealth:
        self.read_new_events()
        if self.error:
            return SseHealth(False, self.lookback_seconds, error=self.error)

        now = int(time.time())
        idle_timeouts = [event for event in self.events if event.kind == "idle_timeout"]
        auto_compactions = [event for event in self.events if event.kind == "auto_compact"]
        websocket_401s = [event for event in self.events if event.kind == "websocket_401"]
        active_ids = {session_id for session_id in active_session_ids if session_id}
        active_timeouts = [event for event in idle_timeouts if event.thread_id in active_ids]
        active_compactions = [event for event in auto_compactions if event.thread_id in active_ids]
        last_timeout = max((event.timestamp for event in idle_timeouts), default=None)
        age = max(0, now - last_timeout) if last_timeout is not None else None
        return SseHealth(
            available=True,
            lookback_seconds=self.lookback_seconds,
            recent_idle_timeouts=len(idle_timeouts),
            active_session_idle_timeouts=len(active_timeouts),
            recent_auto_compactions=len(auto_compactions),
            active_session_auto_compactions=len(active_compactions),
            recent_websocket_401s=len(websocket_401s),
            last_idle_timeout_at=last_timeout,
            last_idle_timeout_age_seconds=age,
        )

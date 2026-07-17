"""Textual application for the interactive CodexNet monitor."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    Switch,
    Tab,
    Tabs,
)

from config import (
    LIFECYCLE_LABELS,
    NETWORK_LABELS,
    RECOVERY_LABELS,
    TUI_CLOCK_INTERVAL,
    TUI_EVENT_POLL_INTERVAL,
    VERSION,
)
from engine import MonitorEngine
from models import (
    AlertStatus,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    SessionHealth,
    SilenceState,
)
from presentation.tui.preferences import (
    TuiPreferences,
    load_preferences,
    save_preferences,
)
from utils import compact_path, format_duration


STATE_COLORS = {
    "error": "#f87171",
    "warning": "#fbbf24",
    "success": "#4ade80",
    "info": "#38bdf8",
    "muted": "#64748b",
}


def session_title(session: SessionHealth) -> str:
    return (
        session.process.session_title
        or session.process.current_task
        or session.session_id[:12]
    )


def session_status(session: SessionHealth) -> str:
    if session.process_exited:
        return "进程已退出"
    if session.attention_request:
        labels = {
            "APPROVAL": "等待审批",
            "PERMISSIONS": "等待权限确认",
            "USER_INPUT": "等待用户回答",
            "MCP_ELICITATION": "等待 MCP 输入",
            "AUTH_ELICITATION": "等待登录操作",
        }
        return labels.get(session.attention.value, "等待用户操作")
    if (
        session.lifecycle == LifecycleState.RUNNING_TOOL
        and session.current_operation.category != "idle"
        and session.current_operation.label
    ):
        return f"{session.phase or '工具正在运行'} · {session.current_operation.label}"
    if session.phase and (
        session.phase != LIFECYCLE_LABELS[LifecycleState.IDLE.value]
        or session.lifecycle == LifecycleState.IDLE
    ):
        return session.phase
    recovery = RECOVERY_LABELS[session.recovery.value]
    return recovery or LIFECYCLE_LABELS[session.lifecycle.value]


def session_marker(session: SessionHealth) -> tuple[str, str]:
    if session.attention_request:
        return "?", STATE_COLORS["warning"]
    if session.current_failure:
        return "×", STATE_COLORS["error"]
    if session.network.state.value == "STALLED" or session.alert_level == "严重":
        return "!", STATE_COLORS["error"]
    if session.silence.state == SilenceState.STALL_SUSPECT:
        return "!", STATE_COLORS["error"]
    if session.silence.state == SilenceState.OBSERVER_BLIND:
        return "?", STATE_COLORS["warning"]
    if session.alert:
        return "!", STATE_COLORS["warning"]
    if session.recovery.value in {"SUSPECT", "RECONNECTING", "TRANSPORT_FALLBACK"}:
        return "↻", STATE_COLORS["warning"]
    if session.process_exited:
        return "○", STATE_COLORS["muted"]
    if session.lifecycle == LifecycleState.COMPACTING:
        return "C", STATE_COLORS["warning"]
    if session.network.state.value == "ACTIVE":
        return "●", STATE_COLORS["success"]
    return "•", STATE_COLORS["info"]


def network_color(session: SessionHealth) -> str:
    if session.network.state.value in {"STALLED", "CLOSED"}:
        return STATE_COLORS["error"]
    if session.network.state.value in {"SUSPECT", "UNKNOWN"}:
        return STATE_COLORS["warning"]
    if session.network.state.value == "ACTIVE":
        return STATE_COLORS["success"]
    return STATE_COLORS["info"]


def matches_session(session: SessionHealth, query: str) -> bool:
    if not query:
        return True
    failure = session.current_failure or session.latest_failure
    values = (
        session.session_id,
        session.process.session_title,
        session.process.current_task,
        session.process.model,
        session.process.cwd,
        failure.message if failure else "",
    )
    return query.casefold() in " ".join(values).casefold()


def session_workspace(session: SessionHealth) -> str:
    cwd = session.process.cwd.strip()
    return compact_path(cwd) if cwd else "工作区未知"


def workspace_group_key(instance_id: str, workspace: str) -> str:
    return f"workspace:{instance_id}:{workspace}"


def workspace_groups(sessions: Iterable[SessionHealth]) -> list[tuple[str, list[SessionHealth]]]:
    groups: dict[str, list[SessionHealth]] = {}
    for session in sessions:
        groups.setdefault(session_workspace(session), []).append(session)
    return sorted(groups.items(), key=lambda item: item[0].casefold())


def _tool_name_is_fallback(metadata: dict[str, object]) -> bool:
    name = str(metadata.get("display_name") or "")
    return bool(
        metadata.get("display_name_is_fallback")
        or name.endswith("_output")
        or "tool_call_output" in name
    )


def event_severity(kind: str) -> tuple[str, str]:
    if kind in {
        "TURN_FAILED",
        "COMPACT_FAILED",
        "OPERATION_ERROR",
        "FILE_CHANGE_FAILED",
        "ALERT_ESCALATED",
    }:
        return "ERR", STATE_COLORS["error"]
    if kind in {
        "WARNING",
        "RECONNECTING",
        "TRANSPORT_FALLBACK",
        "TURN_ABORTED",
        "COMPACT_ABORTED",
        "ALERT_OPENED",
        "ALERT_ACKNOWLEDGED",
        "ACTION_REQUIRED",
        "COMPACT_REQUESTED",
        "UNPARSED_PAYLOAD",
    }:
        return "WARN", STATE_COLORS["warning"]
    if kind in {
        "RECOVERED",
        "TURN_COMPLETED",
        "TOOL_COMPLETED",
        "COMPACT_COMPLETED",
        "ALERT_RESOLVED",
    }:
        return "OK", STATE_COLORS["success"]
    return "INFO", STATE_COLORS["info"]


def timeline_entries(
    session: SessionHealth,
    preferences: TuiPreferences | None = None,
    auto_compact_token_limit: int | None = None,
) -> list[object]:
    preferences = preferences or TuiPreferences()
    tools = {item.call_id: item for item in session.tool_executions}
    tool_starts = {
        str(event.metadata.get("call_id")): event
        for event in session.events
        if event.kind == "TOOL_RUNNING" and event.metadata.get("call_id")
    }
    entries: list[object] = []
    for event in session.events:
        hidden = {
            "MODEL_CONFIG",
            "KEEPALIVE",
            "TOKEN_USAGE",
            "RATE_LIMIT",
            "ITEM_STARTED",
            "ITEM_COMPLETED",
            "COMPACT_CANDIDATE",
            "COMPACT_PROGRESS",
        }
        if event.kind in hidden:
            continue
        if preferences.mode == "operational" and event.kind in {
            "REASONING_SUMMARY",
            "MODEL_PROGRESS",
        }:
            continue
        call_id = str(event.metadata.get("call_id") or "")
        tool = tools.get(call_id)
        start = tool_starts.get(call_id)
        if event.kind in {"TOOL_RUNNING", "TOOL_COMPLETED"} and (tool or start):
            start_metadata = start.metadata if start else {}
            fallback_name = _tool_name_is_fallback(event.metadata)

            def resolved(name: str, summary_value: object = "") -> object:
                if summary_value not in (None, "", (), []):
                    return summary_value
                current = event.metadata.get(name)
                if name in {"category", "display_name", "tool_name"} and fallback_name:
                    current = None
                return current or start_metadata.get(name)

            metadata = {
                **event.metadata,
                "category": resolved("category", tool.category if tool else ""),
                "display_name": resolved(
                    "display_name", tool.display_name if tool else ""
                ),
                "tool_name": resolved("tool_name", tool.tool_name if tool else ""),
                "command": resolved("command", tool.command if tool else ""),
                "cwd": resolved("cwd", tool.cwd if tool else ""),
                "arguments": resolved("arguments", tool.arguments if tool else ""),
                "output": resolved("output", tool.output if tool else ""),
                "files": resolved("files", list(tool.files) if tool else []),
                "nested_tools": resolved("nested_tools"),
            }
            detail = event.detail
            if not detail or _tool_name_is_fallback({"display_name": detail}):
                detail = str(metadata.get("display_name") or metadata.get("tool_name") or "")
            event = replace(event, detail=detail, metadata=metadata)
        if event.kind.startswith("COMPACT") and auto_compact_token_limit:
            event = replace(
                event,
                metadata={
                    **event.metadata,
                    "auto_compact_token_limit": event.metadata.get(
                        "auto_compact_token_limit"
                    )
                    or auto_compact_token_limit,
                },
            )
        entries.append(event)
    visible_compactions = {
        (event.kind, event.timestamp)
        for event in session.events
        if event.kind
        in {
            "COMPACT_REQUESTED",
            "COMPACTING",
            "COMPACT_COMPLETED",
            "COMPACT_FAILED",
            "COMPACT_ABORTED",
        }
    }
    for compact in session.compactions:
        metadata = {
            "trigger": compact.trigger,
            "context_tokens": compact.context_tokens,
            "context_tokens_after": compact.context_tokens_after,
            "context_window": compact.context_window,
            "auto_compact_token_limit": (
                compact.auto_compact_token_limit or auto_compact_token_limit
            ),
            "historical_summary": True,
            "operation_id": compact.operation_id,
            "status": compact.status,
            "source": compact.source,
            "confidence": compact.confidence.value,
            "reconstructed": compact.reconstructed,
        }
        trigger = "手动" if compact.trigger == "manual" else "自动/未知"
        if compact.requested_at is not None and (
            "COMPACT_REQUESTED",
            compact.requested_at,
        ) not in visible_compactions:
            entries.append(
                {
                    "timestamp": compact.requested_at,
                    "kind": "COMPACT_REQUESTED",
                    "summary": "用户已请求上下文压缩",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if compact.started_at is not None and (
            "COMPACTING",
            compact.started_at,
        ) not in visible_compactions:
            entries.append(
                {
                    "timestamp": compact.started_at,
                    "kind": "COMPACTING",
                    "summary": "上下文压缩开始",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if compact.completed_at is not None and (
            "COMPACT_COMPLETED",
            compact.completed_at,
        ) not in visible_compactions:
            entries.append(
                {
                    "timestamp": compact.completed_at,
                    "kind": "COMPACT_COMPLETED",
                    "summary": "上下文压缩完成",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if compact.failed_at is not None and (
            "COMPACT_FAILED",
            compact.failed_at,
        ) not in visible_compactions:
            entries.append(
                {
                    "timestamp": compact.failed_at,
                    "kind": "COMPACT_FAILED",
                    "summary": "上下文压缩失败",
                    "detail": compact.failure.message if compact.failure else trigger,
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if compact.aborted_at is not None and (
            "COMPACT_ABORTED",
            compact.aborted_at,
        ) not in visible_compactions:
            entries.append(
                {
                    "timestamp": compact.aborted_at,
                    "kind": "COMPACT_ABORTED",
                    "summary": "上下文压缩中止",
                    "detail": trigger,
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
    labels = {
        AlertStatus.OPENED: "告警已打开",
        AlertStatus.ESCALATED: "告警已升级",
        AlertStatus.ACKNOWLEDGED: "告警已确认",
        AlertStatus.RESOLVED: "告警已恢复",
    }
    for alert in session.alerts:
        for transition in alert.transitions:
            entries.append(
                {
                    "timestamp": transition.timestamp,
                    "kind": f"ALERT_{transition.status.value}",
                    "summary": labels[transition.status],
                    "detail": transition.reason or alert.reason,
                }
            )
    failure = session.current_failure
    if failure and not any(_matches_failure(entry, failure) for entry in entries):
        entries.append(
            {
                "timestamp": failure.timestamp or time.time(),
                "kind": "TURN_FAILED",
                "summary": "模型调用失败",
                "detail": failure.message,
                "failure": failure,
                "turn_id": failure.turn_id,
            }
        )
    entries = sorted(entries, key=lambda item: float(_value(item, "timestamp", 0) or 0))
    if preferences.mode == "operational":
        compact_kinds = {
            "COMPACT_REQUESTED",
            "COMPACTING",
            "COMPACT_COMPLETED",
        }
        completed_compactions = [
            compact
            for compact in session.compactions
            if compact.status == "completed" or compact.completed_at is not None
        ]
        compact_folded: list[object] = []
        emitted_operations: set[str] = set()
        for entry in entries:
            kind = str(_value(entry, "kind", ""))
            timestamp = float(_value(entry, "timestamp", 0) or 0)
            turn_id = str(_value(entry, "turn_id", ""))
            compact = next(
                (
                    item
                    for item in completed_compactions
                    if (not item.turn_id or not turn_id or item.turn_id == turn_id)
                    and timestamp >= (item.requested_at or item.started_at or timestamp)
                    and timestamp <= (item.completed_at or timestamp)
                ),
                None,
            )
            if compact and kind in compact_kinds:
                if compact.operation_id not in emitted_operations:
                    emitted_operations.add(compact.operation_id)
                    duration = compact.duration_seconds
                    detail_parts = [
                        "手动 compact" if compact.trigger == "manual" else "自动/未知 compact"
                    ]
                    if duration is not None:
                        detail_parts.append(f"{duration:.1f}s")
                    if compact.started_at is not None:
                        detail_parts.append(
                            "开始 " + datetime.fromtimestamp(compact.started_at).strftime("%H:%M:%S")
                        )
                    compact_folded.append(
                        {
                            "timestamp": compact.completed_at or timestamp,
                            "kind": "COMPACT_COMPLETED",
                            "summary": "上下文压缩完成",
                            "detail": " · ".join(detail_parts),
                            "turn_id": compact.turn_id,
                            "metadata": {
                                "trigger": compact.trigger,
                                "duration_seconds": duration,
                                "started_at": compact.started_at,
                                "completed_at": compact.completed_at,
                                "source": compact.source,
                                "confidence": compact.confidence.value,
                                "reconstructed": compact.reconstructed,
                                "context_tokens": compact.context_tokens,
                                "context_tokens_after": compact.context_tokens_after,
                                "context_window": compact.context_window,
                            },
                        }
                    )
                continue
            compact_folded.append(entry)
        entries = sorted(
            compact_folded,
            key=lambda item: float(_value(item, "timestamp", 0) or 0),
        )
        folded: list[object] = []
        pending_tools: dict[str, object] = {}
        for entry in entries:
            kind = str(_value(entry, "kind", ""))
            metadata = _value(entry, "metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            call_id = str(metadata.get("call_id") or "")
            if kind == "TOOL_RUNNING" and call_id:
                pending_tools[call_id] = entry
                continue
            if kind == "TOOL_COMPLETED" and call_id in pending_tools:
                started = pending_tools.pop(call_id)
                duration = float(_value(entry, "timestamp", 0) or 0) - float(
                    _value(started, "timestamp", 0) or 0
                )
                if hasattr(entry, "metadata"):
                    entry = replace(
                        entry,
                        metadata={
                            **metadata,
                            "duration_seconds": metadata.get("duration_seconds")
                            or max(0.0, duration),
                        },
                    )
            folded.append(entry)
        folded.extend(pending_tools.values())
        entries = sorted(folded, key=lambda item: float(_value(item, "timestamp", 0) or 0))
    return entries


def _value(item: object, name: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    value = getattr(item, name, None)
    return default if value is None else value


def _matches_failure(event: object, failure: object) -> bool:
    if _value(event, "kind") != "TURN_FAILED":
        return False
    event_failure = _value(event, "failure", None)
    event_turn = str(_value(event, "turn_id", "") or _value(event_failure, "turn_id", ""))
    failure_turn = str(_value(failure, "turn_id", ""))
    if failure_turn and event_turn == failure_turn:
        return True
    return bool(
        event_failure
        and _value(event_failure, "timestamp", 0) == _value(failure, "timestamp", 0)
        and _value(event_failure, "message", "") == _value(failure, "message", "")
    )


class SampleCompleted(Message):
    """Deliver a sampling result back to the Textual message loop."""

    def __init__(
        self,
        snapshot: MonitorSnapshot | None,
        error: str = "",
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        super().__init__()


class NavigationItem(ListItem):
    """A focusable row carrying a domain key."""

    def __init__(
        self,
        content: Text,
        *,
        kind: str,
        key: str,
        instance_id: str,
        session_key: str = "",
        classes: str = "",
    ) -> None:
        super().__init__(Static(content), classes=classes)
        self._content = content
        self.kind = kind
        self.key_value = key
        self.instance_id = instance_id
        self.session_key = session_key

    def update_from(self, item: "NavigationItem") -> None:
        """Refresh row content without replacing the focused widget."""
        self._content = item._content
        self.kind = item.kind
        self.instance_id = item.instance_id
        self.session_key = item.session_key
        self.query_one(Static).update(self._content)


class SessionInspector(Vertical):
    """Persistent session status and tabbed diagnostic content."""

    def compose(self) -> ComposeResult:
        yield Static("选择一个会话查看实时状态", id="session-title")
        yield Static("HEALTH  等待会话", id="health-strip")
        yield Tabs(
            Tab("1  Activity", id="activity-tab"),
            Tab("2  Diagnosis", id="diagnosis-tab"),
            id="detail-tabs",
        )
        with ContentSwitcher(initial="activity-panel", id="detail-content"):
            yield RichLog(
                id="activity-panel",
                min_width=1,
                wrap=True,
                markup=False,
                auto_scroll=True,
                max_lines=1000,
            )
            with VerticalScroll(id="diagnosis-panel"):
                yield Static("暂无诊断", id="diagnosis-content")

    def show_session(
        self,
        session: SessionHealth | None,
        instance: InstanceSnapshot | None,
        *,
        follow: bool,
        preferences: TuiPreferences | None = None,
    ) -> None:
        preferences = preferences or TuiPreferences()
        if session is None:
            self.query_one("#session-title", Static).update("选择一个会话查看实时状态")
            self.query_one("#health-strip", Static).update("HEALTH  等待会话")
            log = self.query_one("#activity-panel", RichLog)
            log.clear()
            log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
            self._timeline_session_key = ""
            self._timeline_signatures: tuple[tuple[object, ...], ...] = ()
            self._timeline_render_options: tuple[object, ...] = ()
            self.query_one("#diagnosis-content", Static).update("暂无诊断")
            return

        marker, color = session_marker(session)
        title = Text()
        title.append(f"{marker}  ", style=f"bold {color}")
        title.append(session_title(session), style="bold #f8fafc")
        title.append(f"   {session.session_id[:12]}", style="#64748b")
        self.query_one("#session-title", Static).update(title)

        health = Text("HEALTH  ", style="bold #64748b")
        health.append(session_status(session), style="bold #f8fafc")
        now = time.time()
        if session.phase_since is not None:
            health.append(
                f"  {format_duration(max(0, now - session.phase_since))}",
                style="#64748b",
            )
        semantic_at = session.observation.last_semantic_at
        if semantic_at is not None:
            health.append(
                f"  |  语义静默 {format_duration(max(0, now - semantic_at))}",
                style="#94a3b8",
            )
        evidence_at = session.observation.last_evidence_at
        if evidence_at is not None:
            health.append(
                f"  |  最近证据 {session.observation.last_evidence_source or '未知'} "
                f"{format_duration(max(0, now - evidence_at))}前",
                style="#94a3b8",
            )
        if session.silence.state != SilenceState.NORMAL:
            silence_color = (
                STATE_COLORS["error"]
                if session.silence.state == SilenceState.STALL_SUSPECT
                else STATE_COLORS["warning"]
                if session.silence.state == SilenceState.OBSERVER_BLIND
                else STATE_COLORS["info"]
            )
            health.append(
                f"  |  {session.silence.state.value}", style=f"bold {silence_color}"
            )
        health.append("  |  ", style="#64748b")
        health.append(
            NETWORK_LABELS[session.network.state.value],
            style=f"bold {network_color(session)}",
        )
        health.append(
            f"   ·   PID {session.process.pid}   ·   {session.process.model or '模型未知'}",
            style="#94a3b8",
        )
        self.query_one("#health-strip", Static).update(health)

        self._render_timeline(session, instance, follow, preferences)
        self.query_one("#diagnosis-content", Static).update(
            _diagnosis_renderable(
                session,
                instance,
                diagnostic=preferences.mode == "diagnostic",
            )
        )

    def _render_timeline(
        self,
        session: SessionHealth,
        instance: InstanceSnapshot | None,
        follow: bool,
        preferences: TuiPreferences,
    ) -> None:
        log = self.query_one("#activity-panel", RichLog)
        entries = timeline_entries(
            session,
            preferences,
            instance.auto_compact_token_limit if instance else None,
        )
        rendered_at = time.time()
        entries = [
            replace(event, rendered_at=rendered_at)
            if hasattr(event, "rendered_at")
            else event
            for event in entries
        ]
        render_delays = sorted(
            max(0.0, rendered_at - (event.observed_at or event.timestamp))
            for event in entries
            if hasattr(event, "timestamp")
        )

        def render_percentile(fraction: float) -> float | None:
            if not render_delays:
                return None
            position = (len(render_delays) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(render_delays) - 1)
            weight = position - lower
            return render_delays[lower] * (1.0 - weight) + render_delays[upper] * weight

        session.event_telemetry = replace(
            session.event_telemetry,
            rendered_events=len(render_delays),
            render_p50_seconds=render_percentile(0.50),
            render_p95_seconds=render_percentile(0.95),
        )
        signatures = tuple(_timeline_signature(event) for event in entries)
        render_options = (
            preferences.mode,
        )
        previous_session = getattr(self, "_timeline_session_key", "")
        previous_signatures = getattr(self, "_timeline_signatures", ())
        previous_options = getattr(self, "_timeline_render_options", ())
        same_session = previous_session == session.key
        same_options = previous_options == render_options
        was_at_end = log.is_vertical_scroll_end
        previous_scroll_y = getattr(self, "_resize_scroll_y", log.scroll_y)
        resize_follow = getattr(self, "_resize_follow", None)
        should_follow = (
            resize_follow
            if resize_follow is not None
            else follow and (not same_session or was_at_end)
        )
        self._resize_follow = None
        self._resize_scroll_y = log.scroll_y

        self._timeline_session_key = session.key
        self._timeline_signatures = signatures
        self._timeline_render_options = render_options
        log.auto_scroll = follow

        if same_session and same_options and signatures == previous_signatures:
            return

        if (
            same_session
            and same_options
            and previous_signatures
            and signatures[: len(previous_signatures)] == previous_signatures
        ):
            for event in entries[len(previous_signatures) :]:
                log.write(
                    _timeline_line(event, preferences),
                    expand=True,
                    shrink=True,
                    scroll_end=False,
                )
            if should_follow:
                log.scroll_end(animate=False, immediate=True, x_axis=False)
                log.scroll_to(x=0, animate=False, immediate=True)
            return

        log.auto_scroll = False
        log.clear()
        if not entries:
            log.write(Text("暂无事件", style="#64748b"), scroll_end=False)
            log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
            log.auto_scroll = follow
            return
        for event in entries:
            log.write(
                _timeline_line(event, preferences),
                expand=True,
                shrink=True,
                scroll_end=False,
            )
        if should_follow:
            log.scroll_end(animate=False, immediate=True, x_axis=False)
            log.scroll_to(x=0, animate=False, immediate=True)
        elif same_session:
            log.scroll_to(
                x=0,
                y=previous_scroll_y,
                animate=False,
                immediate=True,
                force=True,
            )
        else:
            log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
        log.auto_scroll = follow

    def reflow_activity(
        self,
        session: SessionHealth,
        instance: InstanceSnapshot | None,
        *,
        follow: bool,
        was_at_end: bool,
        scroll_y: float,
        preferences: TuiPreferences,
    ) -> None:
        """Re-render stored RichLog strips after the available width changes."""
        self._timeline_render_options = ()
        self._resize_follow = follow and was_at_end
        self._resize_scroll_y = scroll_y
        self.show_session(
            session,
            instance,
            follow=follow,
            preferences=preferences,
        )


def _timeline_signature(event: object) -> tuple[object, ...]:
    failure = _value(event, "failure", None)
    metadata = _value(event, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    return (
        _value(event, "timestamp", 0),
        _value(event, "kind", ""),
        _value(event, "summary", ""),
        _value(event, "detail", ""),
        _value(event, "turn_id", ""),
        metadata.get("command", ""),
        metadata.get("cwd", ""),
        tuple(metadata.get("files", ()) or ()),
        metadata.get("arguments", ""),
        metadata.get("output", ""),
        _value(failure, "timestamp", 0),
        _value(failure, "message", ""),
    )


def _trace_tag(kind: str, metadata: dict[str, object]) -> str:
    name = " ".join(
        str(metadata.get(key) or "") for key in ("tool_name", "display_name")
    ).lower()
    category = str(metadata.get("category") or "").lower()
    nested_tools = [str(item) for item in (metadata.get("nested_tools") or [])]
    if kind == "ACTION_REQUIRED":
        return "ACTION"
    if kind == "UNPARSED_PAYLOAD":
        return "UNPARSED"
    if kind == "REASONING_SUMMARY":
        return "THINK"
    if kind == "PLAN_UPDATED" or "update_plan" in name:
        return "PLAN"
    if kind.startswith("FILE_CHANGE") or "apply_patch" in name or metadata.get("files"):
        return "WRITE"
    if kind == "COMPACTING" or kind == "COMPACT_COMPLETED":
        return "COMPACT"
    if kind in {"TOOL_RUNNING", "TOOL_COMPLETED"}:
        if "update_plan" in nested_tools and not metadata.get("command"):
            return "PLAN"
        if "mcp" in category or metadata.get("server"):
            return "MCP"
        if metadata.get("command") or "shell" in category or name == "exec":
            return "CMD"
        if "web" in category or "search" in name:
            return "WEB"
        return "TOOL"
    if kind.startswith("AGENT_"):
        return "AGENT"
    if kind == "MODEL_PROGRESS":
        return "MODEL"
    return "EVENT"


def _trace_excerpt(value: object, *, max_lines: int = 6, max_chars: int = 900) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        clipped += f"\n… {len(lines) - max_lines} more lines"
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 1] + "…"
    return clipped


PLAN_MARKERS = {"completed": "✓", "in_progress": "→", "pending": "○"}


def _looks_serialized(value: object) -> bool:
    text = str(value or "").strip()
    return bool(
        text[:1] in "[{\""
        or text.startswith("```")
        or "tools." in text
        or "await " in text
    )

def _timeline_line(
    event: object,
    preferences: TuiPreferences | None = None,
) -> Table:
    preferences = preferences or TuiPreferences()
    timestamp = float(_value(event, "timestamp", 0) or 0)
    stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    kind = str(_value(event, "kind", ""))
    severity, color = event_severity(kind)
    summary = str(_value(event, "summary", kind or "事件"))
    detail = str(_value(event, "detail", ""))
    metadata = _value(event, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    tag = _trace_tag(kind, metadata)
    if kind in {"TOOL_RUNNING", "TOOL_COMPLETED"}:
        tool_label = str(
            metadata.get("display_name")
            or metadata.get("tool_name")
            or detail
            or "工具"
        )
        failed = (
            metadata.get("exit_code") not in (None, 0)
            or str(metadata.get("completion_status") or "").lower()
            in {"failed", "error", "errored"}
        )
        if kind == "TOOL_RUNNING":
            summary = f"正在调用 {tool_label}"
        elif failed:
            summary = f"{tool_label} 调用失败"
        else:
            summary = f"{tool_label} 调用完成"
        if detail == tool_label or _tool_name_is_fallback({"display_name": detail}):
            detail = ""
    table = Table.grid(expand=True, padding=0)
    table.add_column(width=8, no_wrap=True)
    table.add_column(width=3, no_wrap=True)
    table.add_column(width=9, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")

    headline = Text(summary, style=f"bold {color}")
    if detail and kind != "UNPARSED_PAYLOAD":
        detail_text = _trace_excerpt(detail, max_lines=2, max_chars=420)
        headline.append(f"  ·  {detail_text}", style="#cbd5e1")
    table.add_row(
        Text(stamp, style="#64748b"),
        Text(" │ ", style=color),
        Text(tag, style=f"bold {color}"),
        headline,
    )

    def add_detail(label: str, value: object, style: str = "#94a3b8") -> None:
        content = value if isinstance(value, Text) else Text(str(value), style=style)
        table.add_row(
            "",
            Text(" │ ", style="#334155"),
            Text(label, style="bold #64748b"),
            content,
        )

    command = _trace_excerpt(metadata.get("command"), max_lines=4, max_chars=700)
    tool_name = str(metadata.get("tool_name") or "")
    cwd = str(metadata.get("cwd") or "")
    files = [str(path) for path in (metadata.get("files") or [])]
    nested_tools = [str(name) for name in (metadata.get("nested_tools") or []) if name]
    if tool_name:
        add_detail("TOOL", tool_name, "#7dd3fc")
    if command:
        add_detail("CMD", f"$ {command}", "#e2e8f0")
    if cwd:
        add_detail("CWD", cwd, "#64748b")
    if files:
        change_types = metadata.get("change_types")
        change_types = change_types if isinstance(change_types, dict) else {}
        for path in files[:6]:
            operation = str(change_types.get(path) or "pending").upper()
            add_detail("FILE", f"{operation:<7}  {path}", "#7dd3fc")
        if len(files) > 6:
            add_detail("FILE", f"… 另有 {len(files) - 6} 个文件", "#64748b")
    if nested_tools:
        add_detail("CALLS", ", ".join(nested_tools[:6]), "#7dd3fc")
    if metadata.get("background_running"):
        cell_id = str(metadata.get("background_cell_id") or "?")
        waited = metadata.get("background_wait_seconds")
        task = f"cell {cell_id}"
        if isinstance(waited, (int, float)):
            task += f" · 已等待 {float(waited):.1f}s"
        task += " · 暂无新输出" if metadata.get("background_output_empty") else " · 有新输出"
        add_detail("TASK", task, "#fbbf24")
    if kind in {
        "COMPACT_REQUESTED",
        "COMPACTING",
        "COMPACT_COMPLETED",
        "COMPACT_FAILED",
        "COMPACT_ABORTED",
    }:
        context_tokens = metadata.get("context_tokens")
        context_window = metadata.get("context_window")
        if isinstance(context_tokens, (int, float)) and isinstance(
            context_window, (int, float)
        ) and context_window:
            ratio = float(context_tokens) / float(context_window)
            add_detail(
                "CONTEXT",
                f"{int(context_tokens):,} / {int(context_window):,} · {ratio:.1%}",
                "#7dd3fc",
            )
        auto_limit = metadata.get("auto_compact_token_limit")
        if isinstance(context_tokens, (int, float)) and isinstance(
            auto_limit, (int, float)
        ) and auto_limit:
            remaining = int(auto_limit) - int(context_tokens)
            boundary = Text(
                f"{int(context_tokens):,} / {int(auto_limit):,} · "
                f"{float(context_tokens) / float(auto_limit):.1%}",
                style="#fbbf24" if remaining <= 0 else "#7dd3fc",
            )
            boundary.append(f" · 剩余 {max(0, remaining):,}", style="#64748b")
            add_detail("BOUNDARY", boundary)
        source = metadata.get("source")
        confidence = metadata.get("confidence")
        if source or confidence:
            add_detail(
                "SOURCE",
                " · ".join(value for value in (str(source or ""), str(confidence or "")) if value),
            )
        if metadata.get("reconstructed"):
            add_detail("START", "由 completion 回溯重建", "#fbbf24")
    plan = metadata.get("plan")
    if isinstance(plan, list):
        for step in plan[:8]:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "pending")
            marker = PLAN_MARKERS.get(status, "•")
            text = str(step.get("step") or step.get("text") or "-")
            add_detail("STEP", f"{marker} {text}")
    arguments = metadata.get("arguments")
    if isinstance(arguments, str) and arguments.strip() and not _looks_serialized(arguments):
        add_detail("ARG", _trace_excerpt(arguments, max_lines=3, max_chars=600))
    output = metadata.get("output")
    if (
        preferences.mode == "diagnostic"
        and isinstance(output, str)
        and output.strip()
        and not _looks_serialized(output)
    ):
        add_detail("OUT", _trace_excerpt(output, max_lines=4, max_chars=800))
    unparsed = _value(event, "unparsed", None)
    if unparsed:
        add_detail(
            "SOURCE",
            f"{getattr(unparsed, 'source_type', 'unknown')} · "
            f"{getattr(unparsed, 'length', 0)} chars · "
            f"sha256 {getattr(unparsed, 'sha256', '')[:10]}",
            "#fbbf24",
        )
        if preferences.mode == "diagnostic":
            add_detail("PREVIEW", getattr(unparsed, "preview", ""))
    exit_code = metadata.get("exit_code")
    duration = metadata.get("duration_seconds")
    footer = []
    if exit_code is not None:
        footer.append(f"exit {exit_code}")
    if isinstance(duration, (int, float)):
        footer.append(f"{float(duration):.2f}s")
    if footer:
        add_detail("META", " · ".join(footer), "#64748b")
    return table


def _diagnosis_renderable(
    session: SessionHealth,
    instance: InstanceSnapshot | None,
    *,
    diagnostic: bool = False,
) -> object:
    confidence_labels = {"high": "高", "medium": "中", "low": "低"}
    status_labels = {
        "pending": "等待中",
        "running": "运行中",
        "completed": "已完成",
        "closed": "已关闭",
        "failed": "失败",
        "error": "错误",
        "errored": "错误",
        "aborted": "已中断",
        "unknown": "未知",
    }

    def confidence_label(value: object) -> str:
        raw = str(getattr(value, "value", value) or "").lower()
        return confidence_labels.get(raw, raw or "未知")

    def status_label(value: object) -> str:
        raw = str(value or "unknown").lower()
        return status_labels.get(raw, str(value or "未知"))

    blocks: list[object] = []
    observation = session.observation
    silence = Text("静默分析\n", style="bold #64748b")
    now = time.time()
    silence.append(
        f"当前阶段       {session.lifecycle.value} · "
        f"{format_duration(max(0, now - (session.phase_since or now)))}\n"
    )
    silence.append(
        f"最近语义事件   {observation.last_semantic_kind or '-'}"
        f" · {observation.last_semantic_source or '-'} · "
        + (
            f"{format_duration(max(0, now - observation.last_semantic_at))}前\n"
            if observation.last_semantic_at is not None
            else "-\n"
        )
    )
    silence.append(
        f"最近系统证据   {observation.last_evidence_source or '-'} · "
        f"{observation.last_evidence_detail or '-'}"
        + (
            f" · {format_duration(max(0, now - observation.last_evidence_at))}前\n"
            if observation.last_evidence_at is not None
            else "\n"
        )
    )
    process_activity = observation.process_activity
    silence.append(
        f"进程活动       {process_activity.detail or '本窗口无 CPU/IO 差值'}\n"
    )
    for child in process_activity.children[:6]:
        silence.append(
            f"  child          {child.command} · {child.state or '-'} · "
            f"{format_duration(child.elapsed_seconds)}"
            + (" · active" if child.active else "")
            + "\n",
            style="#94a3b8",
        )
    freshness_parts = []
    for label, timestamp in (
        ("rollout", observation.rollout_probe_at),
        ("process", observation.process_probe_at),
        ("network", observation.network_probe_at),
        ("log", observation.log_probe_at),
    ):
        freshness_parts.append(
            f"{label} "
            + (f"{format_duration(max(0, now - timestamp))}" if timestamp else "-")
        )
    silence.append("数据新鲜度     " + " · ".join(freshness_parts) + "\n", style="#94a3b8")
    silence.append(
        f"观察结论       {session.silence.state.value} · {session.silence.reason or '-'}\n"
    )
    if observation.silence_baseline_samples:
        baseline = f"n={observation.silence_baseline_samples}"
        if (
            observation.silence_baseline_samples >= 3
            and observation.silence_p50_seconds is not None
            and observation.silence_p95_seconds is not None
        ):
            baseline = (
                f"p50 {observation.silence_p50_seconds:.0f}s · "
                f"p95 {observation.silence_p95_seconds:.0f}s · "
                f"n={observation.silence_baseline_samples}"
            )
            semantic_age = (
                max(0, now - observation.last_semantic_at)
                if observation.last_semantic_at is not None
                else 0
            )
            if semantic_age > observation.silence_p95_seconds:
                baseline += " · 当前超过 p95"
        silence.append(f"同类静默基线   {baseline}\n", style="#94a3b8")
    silence.append(
        f"置信度         {confidence_label(session.silence.provenance.confidence)} · "
        f"{session.silence.provenance.source}",
        style="#94a3b8",
    )
    blocks.append(silence)

    if session.compactions:
        compact = session.compactions[-1]
        compact_block = Text("\nCompact 生命周期\n", style="bold #64748b")
        compact_block.append(f"状态             {status_label(compact.status)}\n")
        compact_block.append(f"trigger          {compact.trigger or 'unknown'}\n")
        for label, timestamp in (
            ("requested", compact.requested_at),
            ("started", compact.started_at),
            ("completed", compact.completed_at),
            ("failed", compact.failed_at),
            ("aborted", compact.aborted_at),
        ):
            if timestamp is not None:
                compact_block.append(
                    f"{label:<17} {datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')[:-3]}\n"
                )
        if compact.duration_seconds is not None:
            compact_block.append(f"duration          {compact.duration_seconds:.2f}s\n")
        compact_block.append(
            f"source/confidence {compact.source or '-'} · "
            f"{confidence_label(compact.confidence)}"
            + (" · reconstructed" if compact.reconstructed else "")
        )
        for evidence in compact.evidence:
            compact_block.append(
                f"\n  {evidence.edge:<10} {evidence.source} · "
                f"{datetime.fromtimestamp(evidence.timestamp).strftime('%H:%M:%S.%f')[:-3]}",
                style="#94a3b8",
            )
        blocks.append(compact_block)

    for finding in session.diagnosis:
        color = STATE_COLORS.get(finding.severity, STATE_COLORS["info"])
        conclusion = Text("诊断结论\n", style="bold #64748b")
        conclusion.append(finding.conclusion, style=f"bold {color}")
        if finding.reason:
            conclusion.append(f"\n{finding.reason}", style="#e2e8f0")
        provenance = finding.provenance
        mode = "推导结论" if provenance.derived else "直接证据"
        freshness = (
            f"{finding.freshness_seconds:.1f}s"
            if finding.freshness_seconds is not None
            else "未知"
        )
        conclusion.append(
            f"\n{mode} · 置信度 {confidence_label(provenance.confidence)} · "
            f"来源 {provenance.source or '未知'} · 新鲜度 {freshness}",
            style="#94a3b8",
        )
        for evidence in finding.evidence:
            conclusion.append(f"\n  • {evidence}", style="#cbd5e1")
        if finding.action:
            conclusion.append(f"\n建议操作  {finding.action}", style="#fbbf24")
        blocks.append(conclusion)

    if instance:
        quality = Text("\n数据质量\n", style="bold #64748b")
        quality.append(f"工作区    {session_workspace(session)}\n")
        degraded = [item for item in instance.collector_health if item.error]
        quality.append(
            f"采集器    {len(instance.collector_health) - len(degraded)}/"
            f"{len(instance.collector_health)} 正常"
        )
        telemetry = session.event_telemetry
        quality.append(
            f"\n事件      {telemetry.total_events} · 未解析 "
            f"{telemetry.unparsed_events} ({telemetry.unknown_rate:.1%})"
        )
        if telemetry.observation_p50_seconds is not None:
            quality.append(
                f"\n采集延迟  p50 {telemetry.observation_p50_seconds * 1000:.0f}ms · "
                f"p95 {(telemetry.observation_p95_seconds or 0) * 1000:.0f}ms",
                style="#94a3b8",
            )
        if telemetry.render_p50_seconds is not None:
            quality.append(
                f"\n渲染延迟  p50 {telemetry.render_p50_seconds * 1000:.0f}ms · "
                f"p95 {(telemetry.render_p95_seconds or 0) * 1000:.0f}ms",
                style="#94a3b8",
            )
        for collector in instance.collector_health:
            freshness = (
                f"陈旧 {collector.stale_age_seconds:.1f}s"
                if collector.stale_age_seconds is not None
                else "新鲜"
            )
            quality.append(
                f"\n  {collector.name} · {collector.duration_seconds * 1000:.0f}ms · "
                f"{freshness}{f' · {collector.error}' if collector.error else ''}",
                style="#94a3b8" if not collector.error else "#fbbf24",
            )
        for diagnostic in instance.diagnostics:
            quality.append(f"\n  ! {diagnostic}", style="#fbbf24")
        if instance.rollout_context_truncated:
            quality.append("\n  ! rollout 初始读取已截断", style="#fbbf24")
        if instance.process_data_stale_age_seconds is not None:
            quality.append(
                f"\n  ! 进程数据已陈旧 {instance.process_data_stale_age_seconds:.1f}s",
                style="#fbbf24",
            )
        if instance.socket_data_stale_age_seconds is not None:
            quality.append(
                f"\n  ! socket 数据已陈旧 {instance.socket_data_stale_age_seconds:.1f}s",
                style="#fbbf24",
            )
        for event_type, count in instance.unknown_event_types.items():
            quality.append(
                f"\n  ! 未解析 · {event_type} · {count}", style="#fbbf24"
            )
        for label, source in (
            ("TUI session log", instance.tui_session_log),
            ("compact hook", instance.hook_events),
        ):
            state = (
                "未启用"
                if not source.configured
                else "可读"
                if source.readable
                else "不可读"
            )
            quality.append(f"\n  {label} · {state}", style="#94a3b8")
            if source.last_probe_at is not None:
                quality.append(
                    f" · freshness {format_duration(max(0, now - source.last_probe_at))}"
                )
            if source.error:
                quality.append(f" · {source.error}", style="#fbbf24")
        blocks.append(quality)
        unparsed_events = [event for event in session.events if event.unparsed]
        incomplete_events = [
            event for event in session.events if not event.complete and not event.unparsed
        ]
        if unparsed_events or incomplete_events:
            protocol = Text("\n协议健康\n", style="bold #64748b")
            for event in unparsed_events[-8:]:
                payload = event.unparsed
                protocol.append(
                    f"未解析 · {payload.source_type} · {payload.length} 字符 · "
                    f"{payload.sha256[:10]}",
                    style="#fbbf24",
                )
                if payload.truncated:
                    protocol.append(" · 预览已截断", style="#64748b")
                if diagnostic and payload.preview:
                    protocol.append(f"\n  {payload.preview}", style="#94a3b8")
                protocol.append("\n")
            for event in incomplete_events[-8:]:
                protocol.append(
                    f"不完整 · {event.kind} · {event.source} · "
                    f"置信度 {confidence_label(event.confidence)}\n",
                    style="#fbbf24",
                )
            blocks.append(protocol)
        if instance.history_windows:
            trends = Table(title="历史趋势", box=None, expand=True)
            trends.add_column("窗口")
            trends.add_column("样本", justify="right")
            trends.add_column("TTFT P50/P95", justify="right")
            trends.add_column("失败", justify="right")
            trends.add_column("重连/Fallback", justify="right")
            trends.add_column("恢复", justify="right")
            trends.add_column("工具 P50/P95", justify="right")
            trends.add_column("静默 / Compact", justify="right")
            for window in instance.history_windows:
                ttft = (
                    f"{window.ttft_p50_seconds:.1f}/{window.ttft_p95_seconds:.1f}s"
                    if window.ttft_samples >= 3
                    and window.ttft_p50_seconds is not None
                    and window.ttft_p95_seconds is not None
                    else f"n={window.ttft_samples}"
                )
                tool = (
                    f"{window.tool_p50_seconds:.1f}/{window.tool_p95_seconds:.1f}s"
                    if window.tool_samples >= 3
                    and window.tool_p50_seconds is not None
                    and window.tool_p95_seconds is not None
                    else f"n={window.tool_samples}"
                )
                failure = (
                    f"{window.failure_rate:.0%} ({window.failure_count}/{window.turn_count})"
                    if window.failure_rate is not None
                    else "n=0"
                )
                recovery = (
                    f"{window.recovery_average_seconds:.1f}s (n={window.recovery_samples})"
                    if window.recovery_average_seconds is not None
                    else "n=0"
                )
                silence_compact = f"n={window.silence_samples} / C {window.compact_count}"
                if (
                    window.silence_samples >= 3
                    and window.silence_p50_seconds is not None
                    and window.silence_p95_seconds is not None
                ):
                    silence_compact = (
                        f"S {window.silence_p50_seconds:.0f}/{window.silence_p95_seconds:.0f}s"
                        f" · C {window.compact_manual_count}m/{window.compact_auto_count}a"
                        f"/{window.compact_failure_count}f"
                    )
                if window.compact_retry_count:
                    silence_compact += f" · retry {window.compact_retry_count}"
                if (
                    window.compact_duration_samples >= 3
                    and window.compact_duration_p50_seconds is not None
                    and window.compact_duration_p95_seconds is not None
                ):
                    silence_compact += (
                        f" · dur {window.compact_duration_p50_seconds:.1f}/"
                        f"{window.compact_duration_p95_seconds:.1f}s"
                    )
                elif window.compact_duration_samples:
                    silence_compact += f" · dur n={window.compact_duration_samples}"
                if (
                    window.compact_context_samples
                    and window.compact_context_before_average is not None
                    and window.compact_context_after_average is not None
                ):
                    silence_compact += (
                        f" · ctx {window.compact_context_before_average:.0f}"
                        f"->{window.compact_context_after_average:.0f}"
                    )
                trends.add_row(
                    window.label,
                    str(window.sample_count),
                    ttft,
                    failure,
                    f"{window.reconnect_count}/{window.fallback_count}",
                    recovery,
                    tool,
                    silence_compact,
                )
            blocks.append(trends)

    if session.rate_limits:
        limits = Text("\n容量与限额\n", style="bold #64748b")
        for label, window in (
            ("主限额", session.rate_limits.primary),
            ("次限额", session.rate_limits.secondary),
        ):
            if not window:
                continue
            used = f"{window.used_percent:.0f}%" if window.used_percent is not None else "-"
            reset = (
                datetime.fromtimestamp(window.reset_at).astimezone().strftime("%H:%M:%S")
                if window.reset_at
                else "-"
            )
            limits.append(f"{label:<6} {used} · 重置 {reset}\n")
        if session.rate_limits.credits is not None:
            limits.append(f"Credits  {session.rate_limits.credits:g}\n")
        if session.rate_limits.reached is not None:
            limits.append(
                f"已触达   {'是' if session.rate_limits.reached else '否'}\n"
            )
        if session.rate_limits.reason:
            limits.append(f"原因     {session.rate_limits.reason}\n")
        limits.append(
            f"来源     {session.rate_limits.provenance.source or '未知'}",
            style="#94a3b8",
        )
        blocks.append(limits)

    if session.token_usage:
        usage = session.token_usage
        capacity = Text("\n上下文\n", style="bold #64748b")
        capacity.append(f"model 窗口      {usage.context_tokens or '-'} / {usage.context_window or '-'}")
        if usage.context_percent is not None:
            capacity.append(f" ({usage.context_percent:.1f}%)")
        if instance and instance.auto_compact_token_limit:
            limit = instance.auto_compact_token_limit
            remaining = None if usage.context_tokens is None else limit - usage.context_tokens
            capacity.append(f"\n自动 compact 边界  {limit:,}")
            if remaining is not None:
                capacity.append(f" · 剩余 {max(0, remaining):,}")
            capacity.append(
                f"\n配置来源         {instance.auto_compact_config_source or 'config.toml'}",
                style="#94a3b8",
            )
        blocks.append(capacity)

    if session.turns:
        turn = session.turns[-1]
        bottleneck = Text("\nTurn 瓶颈\n", style="bold #64748b")
        bottleneck.append(f"状态       {status_label(turn.status)}\n")
        bottleneck.append(
            f"总耗时     {turn.duration_seconds:.2f}s\n"
            if turn.duration_seconds is not None
            else "总耗时     -\n"
        )
        bottleneck.append(
            f"TTFT         {turn.time_to_first_token_seconds:.2f}s\n"
            if turn.time_to_first_token_seconds is not None
            else "TTFT         -\n"
        )
        bottleneck.append(f"工具       {turn.tool_count}")
        if turn.tool_duration_seconds is not None:
            bottleneck.append(f" · 共 {turn.tool_duration_seconds:.2f}s")
            if turn.duration_seconds:
                bottleneck.append(
                    f" · 占 Turn {turn.tool_duration_seconds / turn.duration_seconds:.0%}"
                )
        if turn.longest_tool:
            bottleneck.append(
                f"\n最慢工具   {turn.longest_tool.display_name} · "
                f"{turn.longest_tool.duration_seconds or 0:.2f}s"
            )
        bottleneck.append(
            f"\n恢复       reconnect {turn.reconnect_count} · fallback "
            f"{turn.fallback_count} · compact {turn.compact_count}"
        )
        if turn.recovery_duration_seconds is not None:
            bottleneck.append(f" · {turn.recovery_duration_seconds:.2f}s")
        blocks.append(bottleneck)

    if session.network.state.value not in {"IDLE", "ACTIVE"} and session.network.connections:
        table = Table(title="异常 TCP", box=None, expand=True)
        table.add_column("对端")
        table.add_column("状态")
        table.add_column("RX", justify="right")
        table.add_column("TX", justify="right")
        table.add_column("重传", justify="right")
        table.add_column("TLS 距今", justify="right")
        for connection in session.network.connections:
            table.add_row(
                connection.peer,
                NETWORK_LABELS[connection.health.value],
                str(connection.received_delta),
                str(connection.sent_delta),
                str(connection.retrans_delta),
                (
                    f"{max(0.0, time.time() - connection.tls_observed_at):.1f}s"
                    if connection.tls_observed_at is not None
                    else "-"
                ),
            )
        blocks.append(table)

    if session.agents:
        agents = Text("\nagent 树\n", style="bold #64748b")

        def add_agent(node: object, depth: int = 0) -> None:
            label = (
                getattr(node, "nickname", "")
                or getattr(node, "agent_path", "")
                or getattr(node, "thread_id", "")[:12]
            )
            agents.append(
                "  " * depth
                + f"├─ {label} [{status_label(getattr(node, 'status', 'unknown'))}]\n"
            )
            model = getattr(node, "model", "") or "model 未知"
            role = getattr(node, "role", "") or "角色未知"
            wait = getattr(node, "wait_seconds", 0.0)
            spawned_at = getattr(node, "spawned_at", None)
            updated_at = getattr(node, "updated_at", None)
            runtime = (
                max(0.0, (updated_at or time.time()) - spawned_at)
                if spawned_at is not None
                else 0.0
            )
            agents.append(
                "  " * (depth + 1)
                + f"{role} · {model} · 运行 {runtime:.1f}s · 等待 {wait:.1f}s\n",
                style="#94a3b8",
            )
            error = getattr(node, "error", None)
            if error:
                agents.append("  " * (depth + 1) + f"错误 {error.message}\n", style="#f87171")
            for child in getattr(node, "children", []):
                add_agent(child, depth + 1)

        for node in session.agents:
            add_agent(node)
        blocks.append(agents)

    if not blocks:
        return Text("暂无诊断数据", style="#64748b")
    return Group(*blocks)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,enter", "dismiss", "返回")]

    def __init__(self, interval: float) -> None:
        super().__init__()
        self.interval = interval

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label("KEYBOARD REFERENCE", id="dialog-title")
            yield Static(
                "NAVIGATION\n"
                "  ↑↓ / j k      移动焦点        Enter  打开会话\n"
                "  Esc           返回列表        Tab    下一个异常\n\n"
                "VIEWS\n"
                "  1 / 2         Activity / Diagnosis\n"
                "  g             分组 / 扁平     a      辅助进程\n"
                "  ,             设置\n\n"
                "OPERATIONS\n"
                "  /             搜索            r      立即刷新\n"
                "  f             自动跟随\n"
                "  q             退出\n\n"
                f"RUNTIME\n  v{VERSION} · full sample {self.interval:g}s · event feed "
                f"{TUI_EVENT_POLL_INTERVAL * 1000:.0f}ms",
                markup=False,
            )
            yield Label("? / Esc / Enter  返回", classes="dialog-hint")


class SettingsScreen(ModalScreen[TuiPreferences | None]):
    """Clickable settings form for presentation-only preferences."""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, preferences: TuiPreferences) -> None:
        super().__init__()
        self.preferences = preferences

    def compose(self) -> ComposeResult:
        with Container(id="settings-dialog"):
            yield Label("DISPLAY SETTINGS", id="dialog-title")
            with VerticalScroll(id="settings-fields"):
                with Horizontal(classes="setting-row"):
                    yield Label("信息模式", classes="setting-label")
                    yield Select(
                        [
                            ("Operational", "operational"),
                            ("Diagnostic", "diagnostic"),
                        ],
                        value=self.preferences.mode,
                        allow_blank=False,
                        compact=True,
                        id="setting-mode",
                    )
                with Horizontal(classes="setting-row"):
                    yield Label("按工作区分组", classes="setting-label")
                    yield Switch(self.preferences.grouped, id="setting-grouped")
                with Horizontal(classes="setting-row"):
                    yield Label("显示辅助进程", classes="setting-label")
                    yield Switch(self.preferences.show_auxiliary, id="setting-auxiliary")
            with Horizontal(id="settings-actions"):
                yield Button("取消", id="settings-cancel")
                yield Button("保存", id="settings-save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-cancel":
            self.dismiss(None)
            return
        if event.button.id != "settings-save":
            return
        mode = self.query_one("#setting-mode", Select).value
        self.dismiss(
            TuiPreferences(
                grouped=self.query_one("#setting-grouped", Switch).value,
                show_auxiliary=self.query_one("#setting-auxiliary", Switch).value,
                mode=str(mode),
            )
        )


class CodexNetApp(App[MonitorSnapshot]):
    """Persistent multi-panel Textual monitor."""

    CSS_PATH = "codexnet.tcss"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = [
        Binding("q", "request_quit", "退出"),
        Binding("question_mark", "help", "帮助"),
        Binding("slash", "search", "搜索"),
        Binding("r", "sample_now", "刷新"),
        Binding("comma", "settings", "设置"),
        Binding("g", "toggle_grouped", "分组"),
        Binding("a", "toggle_auxiliary", "进程", show=False),
        Binding("tab", "next_anomaly", "异常"),
        Binding("1", "show_tab('activity')", "Activity", show=False),
        Binding("2", "show_tab('diagnosis')", "Diagnosis", show=False),
        Binding("f", "toggle_follow", "跟随"),
        Binding("j", "cursor_down", "向下", show=False),
        Binding("k", "cursor_up", "向上", show=False),
        Binding("escape", "back", "返回", show=False),
    ]

    def __init__(
        self,
        engine: MonitorEngine,
        snapshot: MonitorSnapshot,
        *,
        use_color: bool = True,
        show_auxiliary: bool = False,
        flat: bool = False,
        sampling: bool = True,
        preferences: TuiPreferences | None = None,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(ansi_color=use_color)
        loaded_preferences = preferences or (
            load_preferences(settings_path) if sampling else TuiPreferences()
        )
        if show_auxiliary:
            loaded_preferences = replace(loaded_preferences, show_auxiliary=True)
        if flat:
            loaded_preferences = replace(loaded_preferences, grouped=False)
        self.engine = engine
        self.snapshot = snapshot
        self.preferences = loaded_preferences
        self.settings_path = settings_path
        self.show_auxiliary = loaded_preferences.show_auxiliary
        self.grouped = loaded_preferences.grouped
        self.sampling = sampling
        self.collapsed: set[str] = set()
        self.selected_key = ""
        self.selected_session: SessionHealth | None = None
        self.follow = True
        self.compact = False
        self.compact_detail = False
        self.rebuilding = False
        self.sample_in_flight = False
        self.next_full_sample_at = time.monotonic() + self.engine.interval
        self._resize_timer = None
        self._resize_was_at_end = True
        self._resize_scroll_y = 0.0

    def compose(self) -> ComposeResult:
        yield Static(id="app-header")
        yield Static("终端尺寸过小\n至少需要 50 × 12", id="too-small")
        with Horizontal(id="workspace"):
            with Vertical(id="navigation"):
                yield Input(placeholder="搜索会话、模型或错误", id="search")
                yield ListView(id="session-list")
            yield SessionInspector(id="inspector")
        yield Static("READY", id="status-line")
        yield Footer()

    async def on_mount(self) -> None:
        self._update_header()
        self.query_one("#status-line", Static).update(self._live_status())
        await self._rebuild_navigation()
        self.query_one("#session-list", ListView).focus()
        self.set_interval(TUI_CLOCK_INTERVAL, self._clock_tick)
        if self.sampling:
            self.set_interval(TUI_EVENT_POLL_INTERVAL, self._poll_live_events)

    async def _clock_tick(self) -> None:
        """Refresh display-only ages without invoking any collector."""

        if self.rebuilding:
            return
        self._update_header()
        self.query_one("#status-line", Static).update(self._live_status())
        await self._rebuild_navigation()

    def on_resize(self, event: events.Resize) -> None:
        self.compact = event.size.width < 96
        too_small = event.size.width < 50 or event.size.height < 12
        self.screen.set_class(self.compact, "compact")
        self.screen.set_class(self.compact and self.compact_detail, "detail-open")
        self.screen.set_class(too_small, "too-small")
        if not self.is_mounted or not self.selected_session:
            return
        if self._resize_timer is None:
            log = self.query_one("#activity-panel", RichLog)
            self._resize_was_at_end = log.is_vertical_scroll_end
            self._resize_scroll_y = log.scroll_y
        else:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.05, self._reflow_activity_after_resize)

    def _reflow_activity_after_resize(self) -> None:
        self._resize_timer = None
        if not self.selected_session:
            return
        instance = next(
            (
                entry
                for entry in self.snapshot.instances
                if entry.instance_id == self.selected_session.instance_id
            ),
            None,
        )
        self.query_one(SessionInspector).reflow_activity(
            self.selected_session,
            instance,
            follow=self.follow,
            was_at_end=self._resize_was_at_end,
            scroll_y=self._resize_scroll_y,
            preferences=self.preferences,
        )

    def _update_header(self) -> None:
        sessions = self.snapshot.sessions
        issues = sum(
            bool(session.current_failure)
            or bool(session.attention_request)
            or bool(session.alert)
            or session.network.state.value == "STALLED"
            or session.silence.state
            in {SilenceState.STALL_SUSPECT, SilenceState.OBSERVER_BLIND}
            for session in sessions
        )
        text = Text(" CODEXNET", style="bold #38bdf8")
        text.append(
            f"   SESSIONS {len(sessions)}   ISSUES {issues}   "
            f"{self.preferences.mode.upper()}",
            style="#cbd5e1",
        )
        self.query_one("#app-header", Static).update(text)

    def _update_metrics(self) -> None:
        self._update_header()

    def _live_status(self) -> str:
        follow = "FOLLOW" if self.follow else "PAUSED"
        return f"{self.preferences.mode.upper()}  ·  {follow}"

    async def _rebuild_navigation(self) -> None:
        while self.rebuilding:
            await asyncio.sleep(0.01)
        self.rebuilding = True
        list_view = self.query_one("#session-list", ListView)
        query = self.query_one("#search", Input).value.strip()
        items: list[NavigationItem] = []
        for instance in self.snapshot.instances:
            sessions = [item for item in instance.sessions if matches_session(item, query)]
            if query and not sessions:
                continue
            groups = workspace_groups(sessions) if self.grouped else [("", sessions)]
            for workspace, workspace_sessions in groups:
                if self.grouped:
                    group_key = workspace_group_key(instance.instance_id, workspace)
                    open_group = group_key not in self.collapsed
                    marker = "▼" if open_group else "▶"
                    label = Text(f"{marker}  {workspace}", style="bold #e2e8f0")
                    label.append(
                        f"\n   CODEX_HOME {instance.display_codex_home}  ·  "
                        f"{len(workspace_sessions)} sessions",
                        style="#64748b",
                    )
                    failures = sum(bool(item.current_failure) for item in workspace_sessions)
                    if failures:
                        label.append(f"  ·  {failures} failed", style=STATE_COLORS["error"])
                    actions = sum(bool(item.attention_request) for item in workspace_sessions)
                    if actions:
                        label.append(
                            f"  ·  {actions} action required", style=STATE_COLORS["warning"]
                        )
                    items.append(
                        NavigationItem(
                            label,
                            kind="workspace",
                            key=group_key,
                            instance_id=instance.instance_id,
                            classes="workspace-row",
                        )
                    )
                    if not open_group:
                        continue
                for session in sorted(
                    workspace_sessions,
                    key=lambda item: (
                        not bool(item.attention_request),
                        not bool(item.current_failure),
                        item.silence.state != SilenceState.STALL_SUSPECT,
                        item.silence.state != SilenceState.OBSERVER_BLIND,
                        item.alert_level != "严重",
                        item.process.identity.start_time,
                    ),
                ):
                    marker, color = session_marker(session)
                    operation = session.current_operation
                    operation_category = operation.category
                    operation_label = operation.label
                    operation_detail = operation.detail
                    operation_started_at = operation.started_at
                    if operation_category == "idle" and session.lifecycle != LifecycleState.IDLE:
                        operation_category = session.lifecycle.value.lower()
                        operation_label = session_status(session)
                        operation_detail = session.phase
                        operation_started_at = session.phase_since
                    age = format_duration(
                        max(0, time.time() - (operation_started_at or time.time()))
                    )
                    label = Text(f"{marker}  ", style=f"bold {color}")
                    title_text = session_title(session)
                    if not self.grouped:
                        workspace_name = Path(session_workspace(session)).name
                        title_text = f"{workspace_name} · {title_text}"
                    if len(title_text) > 28:
                        title_text = title_text[:27] + "…"
                    label.append(title_text, style="#f8fafc")
                    operation_detail = operation_detail or operation_label
                    auxiliary = []
                    now = time.time()
                    semantic_at = session.observation.last_semantic_at
                    evidence_at = session.observation.last_evidence_at
                    if session.silence.state != SilenceState.NORMAL:
                        operation_detail = session.silence.reason
                    elif semantic_at is not None and now - semantic_at >= 10:
                        operation_detail = (
                            f"静默 {format_duration(max(0, now - semantic_at))}"
                        )
                    if evidence_at is not None and now - evidence_at <= 60:
                        auxiliary.append(
                            f"{session.observation.last_evidence_source or 'evidence'} "
                            f"{format_duration(max(0, now - evidence_at))}前"
                        )
                    if operation.tool_count:
                        auxiliary.append(f"t{operation.tool_count}")
                    if operation.file_count:
                        auxiliary.append(f"f{operation.file_count}")
                    if session.token_usage and session.token_usage.context_percent is not None:
                        auxiliary.append(f"ctx{session.token_usage.context_percent:.0f}%")
                    if operation.agent:
                        auxiliary.append(f"a:{operation.agent[:6]}")
                    if session.observation.process_activity.child_count:
                        auxiliary.append(
                            f"child{session.observation.process_activity.child_count}"
                        )
                    detail_limit = 20 if not auxiliary else 10
                    if len(operation_detail) > detail_limit:
                        operation_detail = operation_detail[: detail_limit - 1] + "…"
                    second_line = (
                        f"\n   {operation_category.upper()} · {operation_detail} · {age}"
                    )
                    if auxiliary:
                        second_line += " · " + " · ".join(auxiliary[:2])
                    label.append(second_line, style="#94a3b8")
                    items.append(
                        NavigationItem(
                            label,
                            kind="session",
                            key=f"session:{session.key}",
                            instance_id=instance.instance_id,
                            session_key=session.key,
                            classes="session-row",
                        )
                    )
            if self.show_auxiliary:
                for process in (item for item in instance.processes if item.role != "session"):
                    label = Text(
                        f"  {process.role}  PID {process.pid}\n"
                        f"   {compact_path(process.cwd) if process.cwd else process.command}",
                        style="#64748b",
                    )
                    items.append(
                        NavigationItem(
                            label,
                            kind="auxiliary",
                            key=f"process:{instance.instance_id}:{process.stable_key}",
                            instance_id=instance.instance_id,
                            classes="aux-row",
                        )
                    )

        previous = self.selected_key
        current_items = [
            item for item in list_view.children if isinstance(item, NavigationItem)
        ]
        stable_structure = [item.key_value for item in current_items] == [
            item.key_value for item in items
        ]
        if stable_structure:
            for current, updated in zip(current_items, items):
                current.update_from(updated)
            keys = [item.key_value for item in current_items]
            if keys:
                index = keys.index(previous) if previous in keys else next(
                    (position for position, item in enumerate(current_items) if item.kind == "session"),
                    0,
                )
                if list_view.index != index:
                    list_view.index = index
                self.selected_key = current_items[index].key_value
                self._select_item(current_items[index])
            self.rebuilding = False
            return

        await list_view.clear()
        if items:
            await list_view.extend(items)
            keys = [item.key_value for item in items]
            index = keys.index(previous) if previous in keys else next(
                (position for position, item in enumerate(items) if item.kind == "session"),
                0,
            )
            list_view.index = index
            self.selected_key = items[index].key_value
            self._select_item(items[index])
        else:
            self.selected_key = ""
            self.selected_session = None
            self.query_one(SessionInspector).show_session(
                None,
                None,
                follow=self.follow,
                preferences=self.preferences,
            )
        self.rebuilding = False

    def _select_item(self, item: NavigationItem) -> None:
        self.selected_key = item.key_value
        if item.kind != "session":
            return
        session = next(
            (entry for entry in self.snapshot.sessions if entry.key == item.session_key),
            None,
        )
        instance = next(
            (entry for entry in self.snapshot.instances if entry.instance_id == item.instance_id),
            None,
        )
        self.selected_session = session
        self.engine.pin_session(session)
        self.query_one(SessionInspector).show_session(
            session,
            instance,
            follow=self.follow,
            preferences=self.preferences,
        )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if not self.rebuilding and isinstance(event.item, NavigationItem):
            self._select_item(event.item)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, NavigationItem):
            return
        if event.item.kind == "workspace":
            if event.item.key_value in self.collapsed:
                self.collapsed.remove(event.item.key_value)
            else:
                self.collapsed.add(event.item.key_value)
            await self._rebuild_navigation()
        elif event.item.kind == "session" and self.compact:
            self.compact_detail = True
            self.screen.add_class("detail-open")
            self.query_one("#detail-tabs", Tabs).focus()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        name = event.tab.id.removesuffix("-tab") if event.tab.id else "activity"
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            await self._rebuild_navigation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.add_class("settled")
        self.query_one("#session-list", ListView).focus()

    def action_request_quit(self) -> None:
        self.exit(self.snapshot)

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.engine.interval))

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.preferences), self._settings_closed)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Expose Textual system commands except the removed screenshot action."""
        for command in super().get_system_commands(screen):
            if command.title != "Screenshot":
                yield command

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.remove_class("settled")
        search.focus()

    def action_cursor_down(self) -> None:
        self.query_one("#session-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#session-list", ListView).action_cursor_up()

    async def action_toggle_grouped(self) -> None:
        self.grouped = not self.grouped
        self.preferences = replace(self.preferences, grouped=self.grouped)
        self._save_preferences()
        await self._rebuild_navigation()

    async def action_toggle_auxiliary(self) -> None:
        self.show_auxiliary = not self.show_auxiliary
        self.preferences = replace(
            self.preferences,
            show_auxiliary=self.show_auxiliary,
        )
        self._save_preferences()
        await self._rebuild_navigation()

    def action_show_tab(self, name: str) -> None:
        if name not in {"activity", "diagnosis"}:
            return
        self.query_one("#detail-tabs", Tabs).active = f"{name}-tab"
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"
        if self.compact:
            self.compact_detail = True
            self.screen.add_class("detail-open")

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.query_one("#status-line", Static).update(self._live_status())
        log = self.query_one("#activity-panel", RichLog)
        log.auto_scroll = self.follow
        if self.follow:
            log.scroll_end(animate=False, immediate=True, x_axis=False)
            log.scroll_to(x=0, animate=False, immediate=True)
        if self.selected_session:
            instance = next(
                (
                    entry
                    for entry in self.snapshot.instances
                    if entry.instance_id == self.selected_session.instance_id
                ),
                None,
            )
            self.query_one(SessionInspector).show_session(
                self.selected_session,
                instance,
                follow=self.follow,
                preferences=self.preferences,
            )

    def _settings_closed(self, preferences: TuiPreferences | None) -> None:
        if preferences is None:
            return
        self.preferences = preferences
        self.grouped = preferences.grouped
        self.show_auxiliary = preferences.show_auxiliary
        log = self.query_one("#activity-panel", RichLog)
        log.auto_scroll = self.follow
        self._save_preferences()
        self._show_selected_session()
        self.call_later(self._rebuild_navigation)
        self.query_one("#status-line", Static).update(
            f"SETTINGS SAVED  ·  {preferences.mode.upper()}"
        )

    def _save_preferences(self) -> None:
        try:
            save_preferences(self.preferences, self.settings_path)
        except OSError as error:
            if self.is_mounted:
                self.query_one("#status-line", Static).update(
                    f"SETTINGS ERROR  {error}"
                )

    def _show_selected_session(self) -> None:
        if not self.selected_session:
            return
        instance = next(
            (
                entry
                for entry in self.snapshot.instances
                if entry.instance_id == self.selected_session.instance_id
            ),
            None,
        )
        self.query_one(SessionInspector).show_session(
            self.selected_session,
            instance,
            follow=self.follow,
            preferences=self.preferences,
        )

    def action_next_anomaly(self) -> None:
        anomalies = [
            item
            for item in self.snapshot.sessions
            if item.current_failure
            or item.attention_request
            or item.alert_level == "严重"
            or item.network.state.value == "STALLED"
        ]
        if not anomalies:
            self.query_one("#status-line", Static).update("NO ACTIVE ANOMALIES")
            return
        keys = [item.key for item in anomalies]
        current = self.selected_session.key if self.selected_session else ""
        index = (keys.index(current) + 1) % len(keys) if current in keys else 0
        target = f"session:{anomalies[index].key}"
        list_view = self.query_one("#session-list", ListView)
        for position, item in enumerate(list_view.children):
            if isinstance(item, NavigationItem) and item.key_value == target:
                list_view.index = position
                item.scroll_visible()
                self._select_item(item)
                break

    def action_back(self) -> None:
        search = self.query_one("#search", Input)
        if search.has_focus:
            search.value = ""
            search.add_class("settled")
            self.query_one("#session-list", ListView).focus()
            return
        if self.compact and self.compact_detail:
            self.compact_detail = False
            self.screen.remove_class("detail-open")
            self.query_one("#session-list", ListView).focus()

    def action_sample_now(self) -> None:
        if not self.sampling or self.sample_in_flight:
            return
        self.next_full_sample_at = time.monotonic() + self.engine.interval
        self._start_sample(full=True)

    def _poll_live_events(self) -> None:
        if not self.sampling or self.sample_in_flight:
            return
        now = time.monotonic()
        full = now >= self.next_full_sample_at
        if full:
            self.next_full_sample_at = now + self.engine.interval
        self._start_sample(full=full)

    def _start_sample(self, *, full: bool) -> None:
        self.sample_in_flight = True
        self._sample_worker(full, self.snapshot)

    @work(thread=True, group="snapshot")
    def _sample_worker(self, full: bool, current: MonitorSnapshot) -> None:
        try:
            snapshot = self.engine.sample() if full else self.engine.refresh_events(current)
        except Exception as error:  # UI worker must report rather than tear down terminal state.
            self.post_message(SampleCompleted(None, str(error)))
            return
        self.post_message(SampleCompleted(snapshot))

    def on_sample_completed(self, event: SampleCompleted) -> None:
        self._finish_sample(event.snapshot, event.error)

    def _finish_sample(
        self,
        snapshot: MonitorSnapshot | None,
        error: str,
    ) -> None:
        self.sample_in_flight = False
        if error:
            self._show_collector_error(error)
        elif snapshot is not None and snapshot is not self.snapshot:
            self._apply_snapshot(snapshot)

    def _show_collector_error(self, message: str) -> None:
        self.query_one("#status-line", Static).update(f"COLLECTOR ERROR  {message}")

    def _notify_transitions(self, previous: MonitorSnapshot, current: MonitorSnapshot) -> None:
        before = {session.key: session for session in previous.sessions}
        active_states = {
            LifecycleState.STARTING,
            LifecycleState.WAITING_RESPONSE,
            LifecycleState.GENERATING,
            LifecycleState.RUNNING_TOOL,
            LifecycleState.COMPACTING,
        }
        for session in current.sessions:
            old = before.get(session.key)
            if old is None:
                continue
            title = session_title(session)
            if session.attention_request and not old.attention_request:
                self.notify(
                    session.attention_request.detail or session_status(session),
                    title=f"ACTION REQUIRED · {title}",
                    severity="warning",
                )
            elif session.current_failure and not old.current_failure:
                self.notify(
                    session.current_failure.message,
                    title=f"FAILED · {title}",
                    severity="error",
                )
            elif (
                session.silence.state == SilenceState.STALL_SUSPECT
                and old.silence.state != SilenceState.STALL_SUSPECT
            ):
                self.notify(
                    session.silence.reason,
                    title=f"STALL SUSPECT · {title}",
                    severity="warning",
                )
            elif (
                session.silence.state == SilenceState.OBSERVER_BLIND
                and old.silence.state != SilenceState.OBSERVER_BLIND
            ):
                self.notify(
                    session.silence.reason,
                    title=f"OBSERVER BLIND · {title}",
                    severity="warning",
                )
            elif (
                session.compactions
                and session.compactions[-1].status
                in {"completed", "failed", "aborted"}
                and (
                    not old.compactions
                    or old.compactions[-1].status != session.compactions[-1].status
                )
            ):
                compact = session.compactions[-1]
                self.notify(
                    f"compact {compact.status}",
                    title=title,
                    severity="error" if compact.status == "failed" else "information",
                )
            elif old.lifecycle in active_states and session.lifecycle == LifecycleState.COMPLETED:
                self.notify("Turn completed", title=title, severity="information")
            elif old.recovery.value != "RECOVERED" and session.recovery.value == "RECOVERED":
                self.notify("Connection recovered", title=title, severity="information")

    def _apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        self._notify_transitions(self.snapshot, snapshot)
        self.snapshot = snapshot
        active = {
            workspace_group_key(instance.instance_id, session_workspace(session))
            for instance in snapshot.instances
            for session in instance.sessions
        }
        self.collapsed.intersection_update(active)
        self._update_metrics()
        self.query_one("#status-line", Static).update(self._live_status())
        self.call_later(self._rebuild_navigation)


def run_textual_tui(
    engine: MonitorEngine,
    use_color: bool,
    show_auxiliary: bool,
    flat: bool,
) -> MonitorSnapshot:
    """Start Textual after the initial baseline window and return the last snapshot."""
    engine.baseline()
    snapshot = engine.sample()
    app = CodexNetApp(
        engine,
        snapshot,
        use_color=use_color,
        show_auxiliary=show_auxiliary,
        flat=flat,
    )
    return app.run() or snapshot

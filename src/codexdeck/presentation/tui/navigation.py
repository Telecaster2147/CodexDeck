"""Navigation projection and stable Textual row widgets."""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from rich.text import Text
from textual.widgets import ListItem, Static

from codexdeck.config import LIFECYCLE_LABELS, RECOVERY_LABELS
from codexdeck.models import (
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    SessionHealth,
    SilenceState,
)
from codexdeck.presentation.tui.theme import STATE_COLORS
from codexdeck.utils import compact_path, format_duration, operator_text


def session_title(session: SessionHealth) -> str:
    value = session.process.session_title or session.process.current_task or session.session_id[:12]
    return operator_text(value, max_cells=80)


def session_status(session: SessionHealth) -> str:
    suffix = " · 证据不完整" if session.completeness.incomplete_axes else ""
    if session.process_exited:
        return "进程已退出" + suffix
    if session.process.foreground_active is False:
        return "终端后台作业" + suffix
    if session.protocol_uncertain:
        return session.phase + suffix
    if session.attention_request:
        labels = {
            "APPROVAL": "等待审批",
            "PERMISSIONS": "等待权限确认",
            "USER_INPUT": "等待用户回答",
            "MCP_ELICITATION": "等待 MCP 输入",
            "AUTH_ELICITATION": "等待登录操作",
        }
        return labels.get(session.attention.value, "等待用户操作") + suffix
    if (
        session.lifecycle == LifecycleState.RUNNING_TOOL
        and session.current_operation.category != "idle"
        and session.current_operation.label
    ):
        label = operator_text(session.current_operation.label, max_cells=48)
        return f"{session.phase or '工具正在运行'} · {label}" + suffix
    if session.phase and (
        session.phase != LIFECYCLE_LABELS[LifecycleState.IDLE.value]
        or session.lifecycle == LifecycleState.IDLE
    ):
        return session.phase + suffix
    recovery = RECOVERY_LABELS[session.recovery.value]
    return (recovery or LIFECYCLE_LABELS[session.lifecycle.value]) + suffix


def session_marker(session: SessionHealth) -> tuple[str, str]:
    if session.protocol_uncertain:
        return "?", STATE_COLORS["warning"]
    if session.attention_request:
        return "?", STATE_COLORS["warning"]
    if session.current_failure:
        return "×", STATE_COLORS["error"]
    if session.completeness.incomplete_axes:
        return "?", STATE_COLORS["warning"]
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


def session_is_visible(session: SessionHealth) -> bool:
    """Keep unknown/headless sessions, but hide exited or confirmed background jobs."""

    return not session.process_exited and session.process.foreground_active is not False


def session_hidden_label(session: SessionHealth) -> str:
    if session.process_exited:
        if any(event.kind == "SESSION_CLOSED" for event in session.events):
            return "CLOSED"
        return "EXITED"
    if session.process.foreground_active is False:
        return "BG"
    return ""


def session_workspace(session: SessionHealth) -> str:
    cwd = session.process.cwd.strip()
    return operator_text(compact_path(cwd), max_cells=96) if cwd else "工作区未知"


def workspace_group_key(instance_id: str, workspace: str) -> str:
    return f"workspace:{instance_id}:{workspace}"


def workspace_groups(sessions: Iterable[SessionHealth]) -> list[tuple[str, list[SessionHealth]]]:
    groups: dict[str, list[SessionHealth]] = {}
    for session in sessions:
        groups.setdefault(session_workspace(session), []).append(session)
    return sorted(groups.items(), key=lambda item: item[0].casefold())


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

    def update_from(self, item: NavigationItem) -> bool:
        """Refresh row content without replacing the focused widget."""

        changed = self._content != item._content
        self._content = item._content
        self.kind = item.kind
        self.instance_id = item.instance_id
        self.session_key = item.session_key
        if changed:
            self.query_one(Static).update(self._content)
        return changed


def _workspace_item(
    instance: InstanceSnapshot,
    workspace: str,
    sessions: list[SessionHealth],
    *,
    open_group: bool,
) -> NavigationItem:
    marker = "▼" if open_group else "▶"
    label = Text(f"{marker}  {workspace}", style="bold #e2e8f0")
    label.append(
        f"\n   CODEX_HOME {instance.display_codex_home}  ·  {len(sessions)} sessions",
        style="#64748b",
    )
    failures = sum(bool(item.current_failure) for item in sessions)
    if failures:
        label.append(f"  ·  {failures} failed", style=STATE_COLORS["error"])
    actions = sum(bool(item.attention_request) for item in sessions)
    if actions:
        label.append(f"  ·  {actions} action required", style=STATE_COLORS["warning"])
    return NavigationItem(
        label,
        kind="workspace",
        key=workspace_group_key(instance.instance_id, workspace),
        instance_id=instance.instance_id,
        classes="workspace-row",
    )


def _session_item(
    instance: InstanceSnapshot,
    session: SessionHealth,
    *,
    grouped: bool,
    now: float,
) -> NavigationItem:
    hidden_label = session_hidden_label(session)
    marker, color = session_marker(session)
    if hidden_label:
        marker, color = "○", STATE_COLORS["muted"]
    operation = session.current_operation
    category = operation.category
    operation_label = operation.label
    detail = operation.detail
    started_at = operation.started_at
    if category == "idle" and session.lifecycle != LifecycleState.IDLE:
        category = session.lifecycle.value.lower()
        operation_label = session_status(session)
        detail = session.phase
        started_at = session.phase_since
    age = format_duration(max(0, now - (started_at or now)))
    label = Text(f"{marker}  ", style=f"bold {color}")
    title_text = session_title(session)
    if not grouped:
        title_text = f"{Path(session_workspace(session)).name} · {title_text}"
    if len(title_text) > 28:
        title_text = title_text[:27] + "…"
    label.append(title_text, style="#94a3b8" if hidden_label else "#f8fafc")
    if hidden_label:
        category = hidden_label
        operation_label = session_status(session)
        detail = operation_label
    detail = detail or operation_label
    auxiliary: list[str] = []
    semantic_at = session.observation.last_semantic_at
    evidence_at = session.observation.last_evidence_at
    if session.silence.state != SilenceState.NORMAL:
        detail = session.silence.reason
    elif semantic_at is not None and now - semantic_at >= 10:
        detail = f"静默 {format_duration(max(0, now - semantic_at))}"
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
        auxiliary.append(f"child{session.observation.process_activity.child_count}")
    detail_limit = 20 if not auxiliary else 10
    if len(detail) > detail_limit:
        detail = detail[: detail_limit - 1] + "…"
    second_line = f"\n   {category.upper()} · {detail} · {age}"
    if auxiliary:
        second_line += " · " + " · ".join(auxiliary[:2])
    label.append(second_line, style="#94a3b8")
    return NavigationItem(
        label,
        kind="session",
        key=f"session:{session.key}",
        instance_id=instance.instance_id,
        session_key=session.key,
        classes="session-row",
    )


def navigation_items(
    snapshot: MonitorSnapshot,
    *,
    query: str,
    grouped: bool,
    show_hidden: bool,
    collapsed: set[str],
    now: float | None = None,
) -> list[NavigationItem]:
    """Project one snapshot into desired rows without touching mounted widgets."""

    projected: list[NavigationItem] = []
    observed_at = time.time() if now is None else now
    for instance in snapshot.instances:
        sessions = [
            item
            for item in instance.sessions
            if (show_hidden or session_is_visible(item)) and matches_session(item, query)
        ]
        if query and not sessions:
            continue
        groups = workspace_groups(sessions) if grouped else [("", sessions)]
        for workspace, workspace_sessions in groups:
            if grouped:
                group_key = workspace_group_key(instance.instance_id, workspace)
                open_group = group_key not in collapsed
                projected.append(
                    _workspace_item(
                        instance,
                        workspace,
                        workspace_sessions,
                        open_group=open_group,
                    )
                )
                if not open_group:
                    continue
            ordered = sorted(
                workspace_sessions,
                key=lambda item: (
                    not session_is_visible(item),
                    not bool(item.attention_request),
                    not bool(item.current_failure),
                    item.silence.state != SilenceState.STALL_SUSPECT,
                    item.silence.state != SilenceState.OBSERVER_BLIND,
                    item.alert_level != "严重",
                    item.process.identity.start_time,
                ),
            )
            projected.extend(
                _session_item(instance, session, grouped=grouped, now=observed_at)
                for session in ordered
            )
    return projected

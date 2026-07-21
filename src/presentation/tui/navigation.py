"""Navigation projection and stable Textual row widgets."""

from __future__ import annotations

from typing import Iterable

from rich.text import Text
from textual.widgets import ListItem, Static

from config import LIFECYCLE_LABELS, RECOVERY_LABELS
from models import LifecycleState, SessionHealth, SilenceState
from presentation.tui.theme import STATE_COLORS
from utils import compact_path


def session_title(session: SessionHealth) -> str:
    return (
        session.process.session_title
        or session.process.current_task
        or session.session_id[:12]
    )


def session_status(session: SessionHealth) -> str:
    if session.process_exited:
        return "进程已退出"
    if session.process.foreground_active is False:
        return "终端后台作业"
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
    return compact_path(cwd) if cwd else "工作区未知"


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

    def update_from(self, item: "NavigationItem") -> bool:
        """Refresh row content without replacing the focused widget."""

        changed = self._content != item._content
        self._content = item._content
        self.kind = item.kind
        self.instance_id = item.instance_id
        self.session_key = item.session_key
        if changed:
            self.query_one(Static).update(self._content)
        return changed

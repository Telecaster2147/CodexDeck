"""Textual application for the interactive CodexNet monitor."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    Tab,
    Tabs,
)

from config import LIFECYCLE_LABELS, NETWORK_LABELS, RECOVERY_LABELS, VERSION
from engine import MonitorEngine
from models import AlertStatus, InstanceSnapshot, MonitorSnapshot, SessionHealth
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
    if session.alert:
        return "严重停顿" if session.alert_level == "严重" else "等待警告"
    recovery = RECOVERY_LABELS[session.recovery.value]
    return recovery or LIFECYCLE_LABELS[session.lifecycle.value]


def session_marker(session: SessionHealth) -> tuple[str, str]:
    if session.current_failure:
        return "×", STATE_COLORS["error"]
    if session.network.state.value == "STALLED" or session.alert_level == "严重":
        return "!", STATE_COLORS["error"]
    if session.recovery.value in {"SUSPECT", "RECONNECTING", "TRANSPORT_FALLBACK"}:
        return "↻", STATE_COLORS["warning"]
    if session.process_exited:
        return "○", STATE_COLORS["muted"]
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


def event_severity(kind: str) -> tuple[str, str]:
    if kind in {"TURN_FAILED", "OPERATION_ERROR", "ALERT_ESCALATED"}:
        return "ERR", STATE_COLORS["error"]
    if kind in {
        "WARNING",
        "RECONNECTING",
        "TRANSPORT_FALLBACK",
        "TURN_ABORTED",
        "ALERT_OPENED",
        "ALERT_ACKNOWLEDGED",
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


def timeline_entries(session: SessionHealth) -> list[object]:
    entries: list[object] = list(session.events)
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
    return sorted(entries, key=lambda item: float(_value(item, "timestamp", 0) or 0))


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
        yield Static("NETWORK  等待会话", id="network-strip")
        yield Static("STATUS   -", id="runtime-strip")
        yield Tabs(
            Tab("1  Timeline", id="timeline-tab"),
            Tab("2  Turns", id="turns-tab"),
            Tab("3  Evidence", id="evidence-tab"),
            id="detail-tabs",
        )
        with ContentSwitcher(initial="timeline-panel", id="detail-content"):
            yield RichLog(
                id="timeline-panel",
                wrap=True,
                markup=False,
                auto_scroll=True,
                max_lines=1000,
            )
            with VerticalScroll(id="turns-panel"):
                yield Static("暂无 Turn 摘要", id="turns-content")
            with VerticalScroll(id="evidence-panel"):
                yield Static("暂无 Evidence", id="evidence-content")

    def show_session(
        self,
        session: SessionHealth | None,
        instance: InstanceSnapshot | None,
        *,
        follow: bool,
    ) -> None:
        if session is None:
            self.query_one("#session-title", Static).update("选择一个会话查看实时状态")
            self.query_one("#network-strip", Static).update("NETWORK  等待会话")
            self.query_one("#runtime-strip", Static).update("STATUS   -")
            log = self.query_one("#timeline-panel", RichLog)
            log.clear()
            log.scroll_home(animate=False, immediate=True)
            self._timeline_session_key = ""
            self._timeline_signatures: tuple[tuple[object, ...], ...] = ()
            self.query_one("#turns-content", Static).update("暂无 Turn 摘要")
            self.query_one("#evidence-content", Static).update("暂无 Evidence")
            return

        marker, color = session_marker(session)
        title = Text()
        title.append(f"{marker}  ", style=f"bold {color}")
        title.append(session_title(session), style="bold #f8fafc")
        title.append(f"   {session.session_id[:12]}", style="#64748b")
        self.query_one("#session-title", Static).update(title)

        network = Text("NETWORK  ", style="bold #64748b")
        network.append(
            NETWORK_LABELS[session.network.state.value],
            style=f"bold {network_color(session)}",
        )
        if session.network.reason:
            network.append(f"  ·  {session.network.reason}", style="#cbd5e1")
        self.query_one("#network-strip", Static).update(network)

        runtime = Text("STATUS   ", style="bold #64748b")
        runtime.append(session_status(session), style="bold #f8fafc")
        runtime.append(
            f"   ·   PID {session.process.pid}   ·   {session.process.model or '模型未知'}",
            style="#94a3b8",
        )
        self.query_one("#runtime-strip", Static).update(runtime)

        self._render_timeline(session, follow)
        self.query_one("#turns-content", Static).update(_turns_renderable(session))
        self.query_one("#evidence-content", Static).update(
            _evidence_renderable(session, instance)
        )

    def _render_timeline(self, session: SessionHealth, follow: bool) -> None:
        log = self.query_one("#timeline-panel", RichLog)
        entries = timeline_entries(session)
        signatures = tuple(_timeline_signature(event) for event in entries)
        previous_session = getattr(self, "_timeline_session_key", "")
        previous_signatures = getattr(self, "_timeline_signatures", ())
        same_session = previous_session == session.key
        was_at_end = log.is_vertical_scroll_end
        previous_scroll_y = log.scroll_y
        should_follow = follow and (not same_session or was_at_end)

        self._timeline_session_key = session.key
        self._timeline_signatures = signatures
        log.auto_scroll = follow

        if same_session and signatures == previous_signatures:
            return

        if (
            same_session
            and previous_signatures
            and signatures[: len(previous_signatures)] == previous_signatures
        ):
            for event in entries[len(previous_signatures) :]:
                log.write(_timeline_line(event), scroll_end=False)
            if should_follow:
                log.scroll_end(animate=False, immediate=True)
            return

        log.auto_scroll = False
        log.clear()
        if not entries:
            log.write(Text("暂无事件", style="#64748b"), scroll_end=False)
            log.scroll_home(animate=False, immediate=True)
            log.auto_scroll = follow
            return
        for event in entries:
            log.write(_timeline_line(event), scroll_end=False)
        if should_follow:
            log.scroll_end(animate=False, immediate=True)
        elif same_session:
            log.scroll_to(y=previous_scroll_y, animate=False, immediate=True, force=True)
        else:
            log.scroll_home(animate=False, immediate=True)
        log.auto_scroll = follow


def _timeline_signature(event: object) -> tuple[object, ...]:
    failure = _value(event, "failure", None)
    return (
        _value(event, "timestamp", 0),
        _value(event, "kind", ""),
        _value(event, "summary", ""),
        _value(event, "detail", ""),
        _value(event, "turn_id", ""),
        _value(failure, "timestamp", 0),
        _value(failure, "message", ""),
    )


def _timeline_line(event: object) -> Text:
    timestamp = float(_value(event, "timestamp", 0) or 0)
    stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    kind = str(_value(event, "kind", ""))
    severity, color = event_severity(kind)
    summary = str(_value(event, "summary", kind or "事件"))
    detail = str(_value(event, "detail", ""))
    line = Text()
    line.append(stamp, style="#64748b")
    line.append("  │ ", style=color)
    line.append(f"{severity:<5}", style=f"bold {color}")
    line.append(summary, style=f"bold {color}")
    if detail:
        line.append(f"  ·  {detail}", style="#cbd5e1")
    return line


def _turns_renderable(session: SessionHealth) -> object:
    if not session.turns:
        return Text("暂无 Turn 摘要；数据源可能未提供 turn 边界", style="#64748b")
    table = Table(box=None, expand=True, padding=(0, 1))
    table.add_column("TURN", style="#e2e8f0", no_wrap=True)
    table.add_column("STATUS")
    table.add_column("DURATION", justify="right")
    table.add_column("TTFT", justify="right")
    table.add_column("TOKENS", justify="right")
    table.add_column("TOOLS", justify="right")
    for turn in reversed(session.turns):
        color = STATE_COLORS["error"] if turn.status == "failed" else STATE_COLORS["success"]
        table.add_row(
            turn.turn_id[:16],
            Text(turn.status, style=color),
            f"{turn.duration_seconds:.1f}s" if turn.duration_seconds is not None else "-",
            f"{turn.time_to_first_token_seconds:.1f}s"
            if turn.time_to_first_token_seconds is not None
            else "-",
            str(turn.token_usage.total_tokens)
            if turn.token_usage and turn.token_usage.total_tokens is not None
            else "-",
            str(turn.tool_count),
        )
    return table


def _evidence_renderable(
    session: SessionHealth, instance: InstanceSnapshot | None
) -> object:
    blocks: list[object] = []
    if instance:
        source = Text("SOURCE\n", style="bold #64748b")
        source.append(f"WORKSPACE    {session_workspace(session)}\n")
        source.append(f"CODEX_HOME   {instance.display_codex_home}\n")
        source.append(f"SQLITE_HOME  {instance.display_sqlite_home}")
        blocks.append(source)
    failure = session.current_failure or session.latest_failure
    if failure:
        label = "CURRENT FAILURE" if session.current_failure else "LAST FAILURE"
        error = Text(f"\n{label}\n", style=f"bold {STATE_COLORS['error']}")
        error.append(f"{failure.category}\n", style=STATE_COLORS["error"])
        error.append(failure.message, style="#f8fafc")
        if failure.additional_details:
            error.append(f"\n{failure.additional_details}", style="#94a3b8")
        blocks.append(error)
    if session.network.connections:
        table = Table(title="TCP EVIDENCE", box=None, expand=True)
        table.add_column("ROUTE")
        table.add_column("PEER")
        table.add_column("STATE")
        table.add_column("RX", justify="right")
        table.add_column("TX", justify="right")
        table.add_column("RETRANS", justify="right")
        for connection in session.network.connections:
            table.add_row(
                connection.route,
                connection.peer,
                connection.health.value,
                str(connection.received_delta),
                str(connection.sent_delta),
                str(connection.retrans_delta),
            )
        blocks.append(table)
    if session.token_usage:
        usage = session.token_usage
        capacity = Text("\nCAPACITY\n", style="bold #64748b")
        capacity.append(
            f"Context  {usage.context_tokens or '-'} / {usage.context_window or '-'}"
        )
        if usage.context_percent is not None:
            capacity.append(f"  ({usage.context_percent:.1f}%)")
        blocks.append(capacity)
    if session.agents:
        agents = Text("\nSUBAGENTS\n", style="bold #64748b")

        def add_agent(node: object, depth: int = 0) -> None:
            agents.append("  " * depth + "├─ ")
            agents.append(
                f"{getattr(node, 'agent_path', '') or getattr(node, 'nickname', '') or 'agent'} "
            )
            agents.append(f"[{getattr(node, 'status', 'unknown')}]\n", style="#94a3b8")
            for child in getattr(node, "children", []):
                add_agent(child, depth + 1)

        for node in session.agents:
            add_agent(node)
        blocks.append(agents)
    if not blocks:
        return Text("暂无额外证据", style="#64748b")
    return Group(*blocks)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,enter", "dismiss", "返回")]

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label("KEYBOARD REFERENCE", id="dialog-title")
            yield Static(
                "NAVIGATION\n"
                "  ↑↓ / j k      移动焦点        Enter  打开会话\n"
                "  Esc           返回列表        Tab    下一个异常\n\n"
                "VIEWS\n"
                "  1 / 2 / 3     Timeline / Turns / Evidence\n"
                "  g             分组 / 扁平     a      辅助进程\n"
                "  c             多 Home 对比\n\n"
                "OPERATIONS\n"
                "  /             搜索            r      立即刷新\n"
                "  f             自动跟随        x      确认告警\n"
                "  q             退出",
                markup=False,
            )
            yield Label("? / Esc / Enter  返回", classes="dialog-hint")


class CompareScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,c", "dismiss", "返回")]

    def __init__(self, snapshot: MonitorSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        with Container(id="compare-dialog"):
            yield Label("CODEX_HOME COMPARE", id="dialog-title")
            yield DataTable(id="compare-table", zebra_stripes=True, cursor_type="row")
            yield Label("c / Esc  返回", classes="dialog-hint")

    def on_mount(self) -> None:
        table = self.query_one("#compare-table", DataTable)
        table.add_columns("CODEX_HOME", "LIVE", "FAIL", "RETRY", "STALL", "HEALTH", "TTFT", "LIMIT")
        for instance in self.snapshot.instances:
            sessions = instance.sessions
            ttfts = [
                turn.time_to_first_token_seconds
                for session in sessions
                for turn in session.turns
                if turn.time_to_first_token_seconds is not None
            ]
            limits = [
                session.rate_limits.primary.used_percent
                for session in sessions
                if session.rate_limits
                and session.rate_limits.primary
                and session.rate_limits.primary.used_percent is not None
            ]
            table.add_row(
                instance.display_codex_home,
                str(sum(not item.process_exited for item in sessions)),
                str(sum(bool(item.current_failure) for item in sessions)),
                str(
                    sum(
                        item.recovery.value
                        in {"SUSPECT", "RECONNECTING", "TRANSPORT_FALLBACK"}
                        for item in sessions
                    )
                ),
                str(sum(item.network.state.value == "STALLED" for item in sessions)),
                "DEGRADED" if instance.diagnostics else "HEALTHY",
                f"{sum(ttfts) / len(ttfts):.1f}s" if ttfts else "-",
                f"{max(limits):.0f}%" if limits else "-",
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
        Binding("g", "toggle_grouped", "分组"),
        Binding("a", "toggle_auxiliary", "进程", show=False),
        Binding("c", "compare", "对比", show=False),
        Binding("tab", "next_anomaly", "异常"),
        Binding("1", "show_tab('timeline')", "Timeline", show=False),
        Binding("2", "show_tab('turns')", "Turns", show=False),
        Binding("3", "show_tab('evidence')", "Evidence", show=False),
        Binding("f", "toggle_follow", "跟随"),
        Binding("x", "acknowledge", "确认", show=False),
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
    ) -> None:
        super().__init__(ansi_color=use_color)
        self.engine = engine
        self.snapshot = snapshot
        self.show_auxiliary = show_auxiliary
        self.grouped = not flat
        self.sampling = sampling
        self.collapsed: set[str] = set()
        self.selected_key = ""
        self.selected_session: SessionHealth | None = None
        self.follow = True
        self.compact = False
        self.compact_detail = False
        self.rebuilding = False

    def compose(self) -> ComposeResult:
        yield Static(id="app-header")
        yield Static(id="metrics")
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
        self._update_metrics()
        await self._rebuild_navigation()
        self.query_one("#session-list", ListView).focus()
        if self.sampling:
            self.set_interval(self.engine.interval, self.action_sample_now)

    def on_resize(self, event: events.Resize) -> None:
        self.compact = event.size.width < 96
        too_small = event.size.width < 50 or event.size.height < 12
        self.screen.set_class(self.compact, "compact")
        self.screen.set_class(self.compact and self.compact_detail, "detail-open")
        self.screen.set_class(too_small, "too-small")

    def _update_header(self) -> None:
        clock = self.snapshot.generated_at.split("T", 1)[-1][:8]
        text = Text(" CODEXNET", style="bold #38bdf8")
        text.append("  /  OVERVIEW", style="#94a3b8")
        text.append(f"    v{VERSION}   {clock}", style="#64748b")
        self.query_one("#app-header", Static).update(text)

    def _update_metrics(self) -> None:
        summary = self.snapshot.summary()
        workspaces = {
            (instance.instance_id, session_workspace(session))
            for instance in self.snapshot.instances
            for session in instance.sessions
        }
        text = Text("● LIVE", style="bold #4ade80")
        text.append(
            f"   HOMES {summary['instances']}   WORK {len(workspaces)}   "
            f"SESSIONS {summary['sessions']}   "
            f"FAIL {summary['current_failures']}   ALERT {summary['alerts']}   "
            f"STALL {summary['network_stalls']}   "
            f"SAMPLE {self.snapshot.collection_duration_seconds * 1000:.0f}ms",
            style="#cbd5e1",
        )
        self.query_one("#metrics", Static).update(text)

    async def _rebuild_navigation(self) -> None:
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
                        not bool(item.current_failure),
                        item.alert_level != "严重",
                        item.process.identity.start_time,
                    ),
                ):
                    marker, color = session_marker(session)
                    age = format_duration(
                        max(0, time.time() - (session.phase_since or time.time()))
                    )
                    label = Text(f"{marker}  ", style=f"bold {color}")
                    label.append(session_title(session), style="#f8fafc")
                    label.append(f"\n   {session_status(session)}  ·  {age}", style="#94a3b8")
                    if not self.grouped:
                        label.append(f"  ·  {session_workspace(session)}", style="#64748b")
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
            self.query_one(SessionInspector).show_session(None, None, follow=self.follow)
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
        self.query_one(SessionInspector).show_session(session, instance, follow=self.follow)

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
        name = event.tab.id.removesuffix("-tab") if event.tab.id else "timeline"
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
        self.push_screen(HelpScreen())

    def action_compare(self) -> None:
        self.push_screen(CompareScreen(self.snapshot))

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
        await self._rebuild_navigation()

    async def action_toggle_auxiliary(self) -> None:
        self.show_auxiliary = not self.show_auxiliary
        await self._rebuild_navigation()

    def action_show_tab(self, name: str) -> None:
        if name not in {"timeline", "turns", "evidence"}:
            return
        self.query_one("#detail-tabs", Tabs).active = f"{name}-tab"
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"
        if self.compact:
            self.compact_detail = True
            self.screen.add_class("detail-open")

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.query_one("#status-line", Static).update(
            "LIVE FOLLOW" if self.follow else "PAUSED"
        )
        log = self.query_one("#timeline-panel", RichLog)
        log.auto_scroll = self.follow
        if self.follow:
            log.scroll_end(animate=False, immediate=True)
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
                self.selected_session, instance, follow=self.follow
            )

    def action_acknowledge(self) -> None:
        session = self.selected_session
        active = [alert for alert in session.alerts if alert.active] if session else []
        if session and active:
            self.engine.machine.acknowledge_alert(
                f"{session.instance_id}:{session.session_id}", active[-1].id
            )
            self.query_one("#status-line", Static).update("ALERT ACKNOWLEDGED")

    def action_next_anomaly(self) -> None:
        anomalies = [
            item
            for item in self.snapshot.sessions
            if item.current_failure
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
        if self.sampling:
            self._sample_worker()

    @work(thread=True, exclusive=True, group="snapshot")
    def _sample_worker(self) -> None:
        try:
            snapshot = self.engine.sample()
        except Exception as error:  # UI worker must report rather than tear down terminal state.
            self.call_from_thread(self._show_collector_error, str(error))
            return
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _show_collector_error(self, message: str) -> None:
        self.query_one("#status-line", Static).update(f"COLLECTOR ERROR  {message}")

    def _apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        self.snapshot = snapshot
        active = {
            workspace_group_key(instance.instance_id, session_workspace(session))
            for instance in snapshot.instances
            for session in instance.sessions
        }
        self.collapsed.intersection_update(active)
        self._update_header()
        self._update_metrics()
        self.query_one("#status-line", Static).update("LIVE")
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

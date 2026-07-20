"""Textual application for the interactive CodexNet monitor."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListView,
    RichLog,
    Static,
    Tab,
    Tabs,
)

from config import (
    NETWORK_LABELS,
    TUI_CLOCK_INTERVAL,
    TUI_EVENT_POLL_INTERVAL,
    VERSION,
)
from engine import MonitorEngine
from models import (
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    SessionHealth,
    SilenceState,
)
from presentation.tui.activity import _timeline_line, _timeline_signature, timeline_entries
from presentation.tui.diagnosis import _diagnosis_renderable
from presentation.tui.navigation import (
    NavigationItem,
    matches_session,
    network_color,
    session_marker,
    session_status,
    session_title,
    session_workspace,
    workspace_group_key,
    workspace_groups,
)
from presentation.tui.terminal_panel import TerminalLog, TerminalPanel
from presentation.tui.theme import STATE_COLORS
from utils import format_duration


APP_BINDINGS = [
    Binding("q", "request_quit", "退出"),
    Binding("question_mark", "help", "帮助"),
    Binding("slash", "search", "搜索"),
    Binding("r", "sample_now", "刷新"),
    Binding("g", "toggle_grouped", "分组"),
    Binding("right_square_bracket", "next_anomaly", "异常"),
    Binding("1", "show_tab('activity')", "Activity", show=False),
    Binding("2", "show_tab('diagnosis')", "Diagnosis", show=False),
    Binding("3", "show_tab('terminal')", "Terminal", show=False),
    Binding("f", "toggle_follow", "跟随"),
    Binding("n", "next_match", "下一匹配", show=False),
    Binding("shift+n", "previous_match", "上一匹配", show=False),
    Binding("j", "cursor_down", "向下", show=False),
    Binding("k", "cursor_up", "向上", show=False),
    Binding("escape", "back", "返回", show=False),
]

BINDING_KEY_LABELS = {
    "question_mark": "?",
    "slash": "/",
    "right_square_bracket": "]",
    "shift+n": "Shift+N",
    "escape": "Esc",
}


def binding_key_label(key: str) -> str:
    return BINDING_KEY_LABELS.get(key, key)


def keyboard_reference() -> str:
    lines = ["SHORTCUTS"]
    for binding in APP_BINDINGS:
        key = binding_key_label(binding.key)
        lines.append(f"  {key:<12} {binding.description}")
    lines.extend(("", "FRAMEWORK", "  Enter        打开会话", "  Tab          切换焦点"))
    return "\n".join(lines)


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


class SessionInspector(Vertical):
    """Persistent session status and tabbed diagnostic content."""

    def compose(self) -> ComposeResult:
        yield Static("选择一个会话查看实时状态", id="session-title")
        yield Static("HEALTH  等待会话", id="health-strip")
        yield Tabs(
            Tab("1  Activity", id="activity-tab"),
            Tab("2  Diagnosis", id="diagnosis-tab"),
            Tab("3  Terminal", id="terminal-tab"),
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
            yield TerminalPanel(id="terminal-panel")

    def show_session(
        self,
        session: SessionHealth | None,
        instance: InstanceSnapshot | None,
        *,
        follow: bool,
    ) -> None:
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
            self.query_one(TerminalPanel).show_session(None, follow=follow)
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

        self._render_timeline(session, instance, follow)
        self.query_one("#diagnosis-content", Static).update(
            _diagnosis_renderable(
                session,
                instance,
            )
        )
        self.query_one(TerminalPanel).show_session(session, follow=follow)

    def _render_timeline(
        self,
        session: SessionHealth,
        instance: InstanceSnapshot | None,
        follow: bool,
    ) -> None:
        log = self.query_one("#activity-panel", RichLog)
        entries = timeline_entries(
            session,
            instance.auto_compact_token_limit if instance else None,
        )
        signatures = tuple(_timeline_signature(event) for event in entries)
        render_options: tuple[object, ...] = ()
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
                    _timeline_line(event),
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
                _timeline_line(event),
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
    ) -> None:
        """Re-render stored RichLog strips after the available width changes."""
        self._timeline_render_options = ()
        self._resize_follow = follow and was_at_end
        self._resize_scroll_y = scroll_y
        self.show_session(
            session,
            instance,
            follow=follow,
        )




class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,enter", "dismiss", "返回")]

    def __init__(self, interval: float) -> None:
        super().__init__()
        self.interval = interval

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label("KEYBOARD REFERENCE", id="dialog-title")
            yield Static(
                keyboard_reference()
                + "\n\n"
                f"RUNTIME\n  v{VERSION} · full sample {self.interval:g}s · event feed "
                f"{TUI_EVENT_POLL_INTERVAL * 1000:.0f}ms",
                markup=False,
            )
            yield Label("? / Esc / Enter  返回", classes="dialog-hint")


class CodexNetApp(App[MonitorSnapshot]):
    """Persistent multi-panel Textual monitor."""

    CSS_PATH = "codexnet.tcss"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = APP_BINDINGS

    def __init__(
        self,
        engine: MonitorEngine,
        snapshot: MonitorSnapshot,
        *,
        use_color: bool = True,
        flat: bool = False,
        sampling: bool = True,
    ) -> None:
        super().__init__(ansi_color=use_color)
        self.engine = engine
        self.snapshot = snapshot
        self.grouped = not flat
        self.sampling = sampling
        self.collapsed: set[str] = set()
        self.selected_key = ""
        self.selected_session: SessionHealth | None = None
        self.follow = True
        self.compact = False
        self.compact_detail = False
        self.rebuilding = False
        self.navigation_dirty = False
        self.sample_in_flight = False
        self.next_full_sample_at = time.monotonic() + self.engine.interval
        self._resize_timer = None
        self._resize_was_at_end = True
        self._resize_scroll_y = 0.0
        self._collector_error = ""
        self._status_message = ""
        self._status_message_until = 0.0

    def compose(self) -> ComposeResult:
        yield Static(id="app-header")
        yield Static("终端尺寸过小\n至少需要 50 × 20", id="too-small")
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
        try:
            self._update_header()
            self.query_one("#status-line", Static).update(self._live_status())
        except NoMatches:
            return
        await self._rebuild_navigation()

    def on_resize(self, event: events.Resize) -> None:
        self.compact = event.size.width < 96
        too_small = event.size.width < 50 or event.size.height < 20
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
        try:
            inspector = self.query_one(SessionInspector)
        except NoMatches:
            return
        inspector.reflow_activity(
            self.selected_session,
            instance,
            follow=self.follow,
            was_at_end=self._resize_was_at_end,
            scroll_y=self._resize_scroll_y,
        )

    def _update_header(self) -> None:
        summary = self.snapshot.summary()
        text = Text(" CODEXNET", style="bold #38bdf8")
        text.append(
            f"   SESSIONS {summary['sessions']}   ISSUES {summary['issues']}",
            style="#cbd5e1",
        )
        self.query_one("#app-header", Static).update(text)

    def _update_metrics(self) -> None:
        self._update_header()

    def _live_status(self) -> str:
        if self._collector_error:
            return f"COLLECTOR ERROR  {self._collector_error}"
        if self._status_message and time.monotonic() < self._status_message_until:
            return self._status_message
        follow = "FOLLOW" if self.follow else "PAUSED"
        return follow

    def _set_status_message(self, message: str, duration: float = 3.0) -> None:
        self._status_message = message
        self._status_message_until = time.monotonic() + duration
        if self.is_mounted:
            self.query_one("#status-line", Static).update(self._live_status())

    async def _rebuild_navigation(self) -> None:
        if self.rebuilding:
            self.navigation_dirty = True
            return
        self.rebuilding = True
        self.navigation_dirty = False
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
            rerun = self.navigation_dirty
            self.rebuilding = False
            if rerun:
                self.call_later(self._rebuild_navigation)
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
            )
        rerun = self.navigation_dirty
        self.rebuilding = False
        if rerun:
            self.call_later(self._rebuild_navigation)

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

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Expose Textual system commands except the removed screenshot action."""
        for command in super().get_system_commands(screen):
            if command.title != "Screenshot":
                yield command

    def action_search(self) -> None:
        if self.query_one("#detail-tabs", Tabs).active == "terminal-tab":
            self.query_one(TerminalPanel).action_search()
            return
        search = self.query_one("#search", Input)
        search.remove_class("settled")
        search.focus()

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, (RichLog, VerticalScroll)):
            focused.scroll_relative(y=1, animate=False, immediate=True)
        elif isinstance(focused, DataTable):
            focused.action_cursor_down()
        else:
            self.query_one("#session-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, (RichLog, VerticalScroll)):
            focused.scroll_relative(y=-1, animate=False, immediate=True)
        elif isinstance(focused, DataTable):
            focused.action_cursor_up()
        else:
            self.query_one("#session-list", ListView).action_cursor_up()

    def action_next_match(self) -> None:
        if self.query_one("#detail-tabs", Tabs).active == "terminal-tab":
            if not self.query_one(TerminalPanel).next_match(1):
                self._set_status_message("NO TERMINAL MATCHES")

    def action_previous_match(self) -> None:
        if self.query_one("#detail-tabs", Tabs).active == "terminal-tab":
            if not self.query_one(TerminalPanel).next_match(-1):
                self._set_status_message("NO TERMINAL MATCHES")

    async def action_toggle_grouped(self) -> None:
        self.grouped = not self.grouped
        await self._rebuild_navigation()

    def action_show_tab(self, name: str) -> None:
        if name not in {"activity", "diagnosis", "terminal"}:
            return
        self.query_one("#detail-tabs", Tabs).active = f"{name}-tab"
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"
        if name == "terminal":
            self.query_one(TerminalPanel).focus_transcript()
        if self.compact:
            self.compact_detail = True
            self.screen.add_class("detail-open")

    def action_toggle_follow(self) -> None:
        if not self.follow:
            active = self.query_one("#detail-tabs", Tabs).active
            target = (
                self.query_one("#terminal-output", TerminalLog)
                if active == "terminal-tab"
                else self.query_one("#activity-panel", RichLog)
            )
            if not target.is_vertical_scroll_end:
                self._set_status_message("FOLLOW REQUIRES END")
                return
        self.follow = not self.follow
        self.query_one("#status-line", Static).update(self._live_status())
        log = self.query_one("#activity-panel", RichLog)
        log.auto_scroll = self.follow
        terminal_log = self.query_one("#terminal-output", TerminalLog)
        terminal_log.auto_scroll = self.follow
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
            self._set_status_message("NO ACTIVE ANOMALIES")
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
        if (
            self.query_one("#detail-tabs", Tabs).active == "terminal-tab"
            and self.query_one(TerminalPanel).back()
        ):
            return
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
            self._collector_error = ""
            self._apply_snapshot(snapshot)
        else:
            self._collector_error = ""
            self.query_one("#status-line", Static).update(self._live_status())

    def _show_collector_error(self, message: str) -> None:
        self._collector_error = message
        self.query_one("#status-line", Static).update(self._live_status())

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
    flat: bool,
) -> MonitorSnapshot:
    """Start Textual after the initial baseline window and return the last snapshot."""
    engine.baseline()
    snapshot = engine.sample()
    app = CodexNetApp(
        engine,
        snapshot,
        use_color=use_color,
        flat=flat,
    )
    return app.run() or snapshot

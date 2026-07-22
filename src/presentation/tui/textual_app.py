"""Textual application for the interactive CodexDeck monitor."""

from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import (
    ContentSwitcher,
    Collapsible,
    DataTable,
    Input,
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
from presentation.attention import attention_queue
from presentation.tui.activity import _timeline_line, _timeline_signature, timeline_entries
from presentation.tui.controls import (
    APP_BINDINGS,
    ControlsScreen,
    SettingsScreen,
    ShortcutFooter,
    keyboard_reference as _keyboard_reference,
)
from presentation.tui.diagnosis import (
    _diagnosis_details_renderable,
    _diagnosis_renderable,
)
from presentation.tui.navigation import (
    NavigationItem,
    matches_session,
    network_color,
    session_marker,
    session_hidden_label,
    session_is_visible,
    session_status,
    session_title,
    session_workspace,
    workspace_group_key,
    workspace_groups,
)
from presentation.tui.sampling import SamplingCoordinator
from presentation.tui.terminal_panel import TerminalLog, TerminalPanel
from presentation.tui.theme import CODEXDECK_BLUE_THEME, STATE_COLORS
from preferences import (
    CodexDeckPreferences,
    load_preferences,
    preferences_path,
    save_preferences,
)
from utils import format_duration

BINDING_KEY_LABELS = {
    "question_mark": "?",
    "slash": "/",
    "right_square_bracket": "]",
    "shift+n": "Shift+N",
    "ctrl+c": "Ctrl+C",
    "escape": "Esc",
}

STARTUP_FRAME_INTERVAL = 0.10
STARTUP_DURATION = 3.0
STARTUP_FRAMES_PER_STAGE = 6
STARTUP_SYSTEMS = ("CORE", "EVENTS", "TERMINALS", "NETWORK")
STARTUP_STAGES = (
    "DISCOVERING ACTIVE SESSIONS",
    "CORRELATING ROLLOUT EVENTS",
    "VERIFYING TERMINAL PROCESSES",
    "CONSOLE READY",
)

STARTUP_LOGO = (
    " ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗",
    "██╔════╝██╔═══██╗██╔══██╗██╔════╝╚██╗██╔╝",
    "██║     ██║   ██║██║  ██║█████╗   ╚███╔╝ ",
    "██║     ██║   ██║██║  ██║██╔══╝   ██╔██╗ ",
    "╚██████╗╚██████╔╝██████╔╝███████╗██╔╝ ██╗",
    " ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝",
)

STARTUP_DECK_LOGO = (
    "██████╗ ███████╗ ██████╗██╗  ██╗",
    "██╔══██╗██╔════╝██╔════╝██║ ██╔╝",
    "██║  ██║█████╗  ██║     █████╔╝ ",
    "██║  ██║██╔══╝  ██║     ██╔═██╗ ",
    "██████╔╝███████╗╚██████╗██║  ██╗",
    "╚═════╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝",
)


def binding_key_label(key: str) -> str:
    return BINDING_KEY_LABELS.get(key, key)


def keyboard_reference() -> str:
    """Compatibility export for the application shortcut reference."""

    return _keyboard_reference()


def startup_renderable(frame: int, *, compact: bool = False) -> Text:
    """Build one stable startup animation frame for wide or compact terminals."""

    stage = min(frame // STARTUP_FRAMES_PER_STAGE, len(STARTUP_STAGES) - 1)
    text = Text(justify="center")
    if compact:
        text.append("CODEXDECK\n", style="bold #67d8ff")
    else:
        for line in STARTUP_LOGO:
            text.append(f"{line}\n", style="bold #67d8ff")
        for line in STARTUP_DECK_LOGO:
            text.append(f"{line}\n", style="bold #f8fafc")
    text.append("\nREAD-ONLY PROCESS OBSERVATORY\n\n", style="bold #cbd5e1")

    for index, system in enumerate(STARTUP_SYSTEMS):
        active = index <= stage
        text.append("◆ " if active else "◇ ", style="#67d8ff" if active else "#334155")
        text.append(system, style="bold #e2e8f0" if active else "#64748b")
        if index < len(STARTUP_SYSTEMS) - 1:
            text.append("   " if compact else "    ")

    rail_width = 24 if compact else 40
    filled = max(1, round(rail_width * (stage + 1) / len(STARTUP_STAGES)))
    text.append("\n\n")
    text.append("━" * filled, style="#67d8ff")
    text.append("━" * (rail_width - filled), style="#1e293b")
    text.append(f"\n{STARTUP_STAGES[stage]}", style="bold #94a3b8")
    text.append(f"\n\nv{VERSION}", style="#475569")
    return text


class StartupOverlay(Static):
    """Short-lived, non-modal brand layer shown while the console mounts."""


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
                with Collapsible(title="异常详情", collapsed=True, id="diagnosis-details"):
                    yield Static("当前没有异常详情", id="diagnosis-details-content")
            yield TerminalPanel(id="terminal-panel")

    def show_session(
        self,
        session: SessionHealth | None,
        instance: InstanceSnapshot | None,
        *,
        follow: bool,
    ) -> None:
        if session is None:
            self._session_title_value = None
            self._health_strip_value = None
            self.query_one("#session-title", Static).update("选择一个会话查看实时状态")
            self.query_one("#health-strip", Static).update("HEALTH  等待会话")
            log = self.query_one("#activity-panel", RichLog)
            log.clear()
            log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
            self._timeline_session_key = ""
            self._timeline_signatures: tuple[tuple[object, ...], ...] = ()
            self._timeline_render_options: tuple[object, ...] = ()
            self.query_one("#diagnosis-content", Static).update("暂无诊断")
            self.query_one("#diagnosis-details", Collapsible).display = False
            self.query_one(TerminalPanel).show_session(None, follow=follow)
            return

        self.refresh_session_header(session)

        self._render_timeline(session, instance, follow)
        self.query_one("#diagnosis-content", Static).update(
            _diagnosis_renderable(
                session,
                instance,
            )
        )
        detail_count, detail_renderable = _diagnosis_details_renderable(session, instance)
        details = self.query_one("#diagnosis-details", Collapsible)
        if getattr(self, "_diagnosis_session_key", "") != session.key:
            details.collapsed = True
        self._diagnosis_session_key = session.key
        details.title = f"异常详情 ({detail_count})"
        details.display = detail_count > 0
        self.query_one("#diagnosis-details-content", Static).update(detail_renderable)
        self.query_one(TerminalPanel).show_session(session, follow=follow)

    def refresh_session_header(self, session: SessionHealth) -> None:
        """Refresh only the selected session fields whose displayed ages can change."""

        marker, color = session_marker(session)
        title = Text()
        title.append(f"{marker}  ", style=f"bold {color}")
        title.append(session_title(session), style="bold #f8fafc")
        title.append(f"   {session.session_id[:12]}", style="#64748b")
        if title != getattr(self, "_session_title_value", None):
            self._session_title_value = title
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
        if health != getattr(self, "_health_strip_value", None):
            self._health_strip_value = health
            self.query_one("#health-strip", Static).update(health)

    def _render_timeline(
        self,
        session: SessionHealth,
        instance: InstanceSnapshot | None,
        follow: bool,
    ) -> None:
        log = self.query_one("#activity-panel", RichLog)
        render_width = log.size.width
        if render_width < 24:
            return
        entries = timeline_entries(
            session,
            instance.auto_compact_token_limit if instance else None,
        )
        row_console = Console(
            width=render_width,
            color_system=self.app.console.color_system,
        )
        signatures = tuple(_timeline_signature(event) for event in entries)
        render_options: tuple[object, ...] = (render_width,)
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
            appended = entries[len(previous_signatures) :]
            for index, event in enumerate(appended):
                log.write(
                    self._render_timeline_line(event, render_width, row_console),
                    width=render_width,
                    expand=True,
                    shrink=True,
                    scroll_end=should_follow and index == len(appended) - 1,
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
        for index, event in enumerate(entries):
            log.write(
                self._render_timeline_line(event, render_width, row_console),
                width=render_width,
                expand=True,
                shrink=True,
                scroll_end=should_follow and index == len(entries) - 1,
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

    @staticmethod
    def _render_timeline_line(event: object, width: int, console: Console) -> Text:
        lines = console.render_lines(
            _timeline_line(event),
            console.options.update_width(width),
            pad=False,
        )
        rendered = Text()
        for line_index, line in enumerate(lines):
            for segment in line:
                if not segment.control:
                    rendered.append(segment.text, style=segment.style)
            if line_index < len(lines) - 1:
                rendered.append("\n")
        return rendered

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
        self._timeline_render_options = ("force-reflow",)
        self._resize_follow = follow and was_at_end
        self._resize_scroll_y = scroll_y
        self.show_session(
            session,
            instance,
            follow=follow,
        )




class CodexDeckApp(App[MonitorSnapshot]):
    """Persistent multi-panel Textual monitor."""

    CSS_PATH = "codexdeck.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = APP_BINDINGS
    STARTUP_FRAME_INTERVAL = STARTUP_FRAME_INTERVAL
    STARTUP_DURATION = STARTUP_DURATION

    def __init__(
        self,
        engine: MonitorEngine,
        snapshot: MonitorSnapshot,
        *,
        use_color: bool = True,
        flat: bool = False,
        sampling: bool = True,
        startup_animation: bool = False,
        preferences: CodexDeckPreferences | None = None,
        preferences_file: Path | None = None,
        prepare_on_start: bool = False,
    ) -> None:
        super().__init__(ansi_color=use_color)
        self.register_theme(CODEXDECK_BLUE_THEME)
        preferences = preferences or CodexDeckPreferences(
            startup_animation=startup_animation,
        )
        self.engine = engine
        self.snapshot = snapshot
        self.preferences = preferences
        self._flat_override = flat
        self.grouped = preferences.group_sessions and not flat
        self.show_hidden = preferences.show_hidden_sessions
        self.sampling = sampling
        self.startup_animation_enabled = preferences.startup_animation
        self.notifications_enabled = preferences.notifications
        self.default_tab = preferences.default_tab
        self.preferences_file = preferences_file
        self.prepare_on_start = prepare_on_start
        self.collapsed: set[str] = set()
        self.selected_key = ""
        self.selected_session: SessionHealth | None = None
        self.follow = preferences.follow_output
        self.zoom_mode = ""
        self.compact = False
        self.compact_detail = False
        self.rebuilding = False
        self.navigation_dirty = False
        self.sampling_coordinator = SamplingCoordinator.starting_at(
            self.engine.interval,
            time.monotonic(),
        )
        self._resize_timer = None
        self._resize_was_at_end = True
        self._resize_scroll_y = 0.0
        self._resize_reflow_attempts = 0
        self._terminal_reflow_attempts = 0
        self._activity_tab_was_at_end = True
        self._activity_tab_scroll_y = 0.0
        self._collector_error = ""
        self._status_message = ""
        self._status_message_until = 0.0
        self._header_signature: tuple[object, ...] | None = None
        self._status_line_value = ""
        self._startup_frame = 0
        self._startup_visible = self.startup_animation_enabled
        self._startup_animation_complete = not self.startup_animation_enabled
        self._startup_data_ready = not prepare_on_start
        self._initial_preparing = prepare_on_start
        self._startup_interval = None
        self._startup_timer = None
        self.theme = preferences.theme

    def compose(self) -> ComposeResult:
        yield Static(id="app-header")
        yield Static("终端尺寸过小\n至少需要 50 × 20", id="too-small")
        with Horizontal(id="workspace"):
            with Vertical(id="navigation"):
                yield Input(placeholder="搜索会话、模型或错误", id="search")
                yield ListView(id="session-list")
            yield SessionInspector(id="inspector")
        yield Static("READY", id="status-line")
        yield ShortcutFooter(id="shortcut-footer")
        yield StartupOverlay(id="startup-overlay")

    async def on_mount(self) -> None:
        overlay = self.query_one(StartupOverlay)
        if self._startup_visible:
            self._render_startup()
            self._startup_interval = self.set_interval(
                self.STARTUP_FRAME_INTERVAL,
                self._advance_startup,
            )
            self._startup_timer = self.set_timer(
                self.STARTUP_DURATION,
                self._complete_startup_animation,
            )
        else:
            overlay.display = False
        self._select_default_tab(self.default_tab)
        self._update_header()
        self._update_status_line()
        self._update_shortcut_footer()
        await self._rebuild_navigation()
        self.query_one("#session-list", ListView).focus()
        self.set_interval(TUI_CLOCK_INTERVAL, self._clock_tick)
        if self.sampling:
            self.set_interval(TUI_EVENT_POLL_INTERVAL, self._poll_live_events)
        if self.prepare_on_start:
            if self.sampling_coordinator.begin_initial():
                self._initial_sample_worker()

    def _render_startup(self) -> None:
        if not self._startup_visible:
            return
        compact = self.size.width < 96 or self.size.height < 28
        try:
            overlay = self.query_one(StartupOverlay)
        except NoMatches:
            self._stop_startup()
            return
        overlay.update(startup_renderable(self._startup_frame, compact=compact))

    def _advance_startup(self) -> None:
        if not self._startup_visible:
            return
        self._startup_frame += 1
        self._render_startup()

    def _complete_startup_animation(self) -> None:
        self._startup_animation_complete = True
        if self._startup_data_ready:
            self._dismiss_startup()

    def _dismiss_startup(self) -> None:
        if not self._startup_visible:
            return
        self._stop_startup()
        try:
            self.query_one(StartupOverlay).display = False
        except NoMatches:
            return

    def _stop_startup(self) -> None:
        self._startup_visible = False
        if self._startup_interval is not None:
            self._startup_interval.stop()
            self._startup_interval = None
        if self._startup_timer is not None:
            self._startup_timer.stop()
            self._startup_timer = None

    async def _clock_tick(self) -> None:
        """Refresh display-only ages without invoking any collector."""

        if self.rebuilding:
            return
        try:
            self._update_header()
            self._update_status_line()
        except NoMatches:
            return
        await self._rebuild_navigation()
        if self.selected_session is not None:
            try:
                self.query_one(SessionInspector).refresh_session_header(self.selected_session)
            except NoMatches:
                return

    def on_resize(self, event: events.Resize) -> None:
        self._render_startup()
        if self.is_mounted and self.selected_session and self._resize_timer is None:
            try:
                log = self.query_one("#activity-panel", RichLog)
            except NoMatches:
                return
            self._resize_was_at_end = log.is_vertical_scroll_end
            self._resize_scroll_y = log.scroll_y
        self.compact = event.size.width < 96
        if self.compact and self.zoom_mode:
            self._set_zoom("")
        too_small = event.size.width < 50 or event.size.height < 20
        self.screen.set_class(self.compact, "compact")
        self.screen.set_class(self.compact and self.compact_detail, "detail-open")
        self.screen.set_class(too_small, "too-small")
        self._update_shortcut_footer()
        if not self.is_mounted or not self.selected_session:
            return
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.05, self._reflow_activity_after_resize)

    def _reflow_activity_after_resize(self) -> None:
        self._resize_timer = None
        try:
            active_tab = self.query_one("#detail-tabs", Tabs).active
        except NoMatches:
            return
        if active_tab == "terminal-tab":
            self._terminal_reflow_attempts = 0
            self._reflow_terminal_after_layout()
            return
        self._resize_reflow_attempts = 0
        self._reflow_activity_after_layout()

    def _reflow_activity_after_layout(self) -> None:
        try:
            log = self.query_one("#activity-panel", RichLog)
        except NoMatches:
            return
        if log.size.width < 24:
            if self._resize_reflow_attempts < 3:
                self._resize_reflow_attempts += 1
                self.set_timer(0.02, self._reflow_activity_after_layout)
            return
        self._reflow_selected_activity(
            was_at_end=self._resize_was_at_end,
            scroll_y=self._resize_scroll_y,
        )

    def _reflow_selected_activity(
        self,
        *,
        was_at_end: bool | None = None,
        scroll_y: float | None = None,
    ) -> None:
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
            log = self.query_one("#activity-panel", RichLog)
        except NoMatches:
            return
        if log.size.width <= 1:
            return
        inspector.reflow_activity(
            self.selected_session,
            instance,
            follow=self.follow,
            was_at_end=log.is_vertical_scroll_end if was_at_end is None else was_at_end,
            scroll_y=log.scroll_y if scroll_y is None else scroll_y,
        )

    def _reflow_terminal_after_layout(self) -> None:
        try:
            panel = self.query_one(TerminalPanel)
        except NoMatches:
            return
        if panel.reflow(follow=self.follow):
            return
        if self._terminal_reflow_attempts < 3:
            self._terminal_reflow_attempts += 1
            self.set_timer(0.02, self._reflow_terminal_after_layout)

    def _update_header(self) -> None:
        visible = [session for session in self.snapshot.sessions if session_is_visible(session)]
        hidden = len(self.snapshot.sessions) - len(visible)
        queue = attention_queue(visible)
        issues = sum(
            bool(item.current_failure)
            or bool(item.attention_request)
            or bool(item.alert)
            or item.network.state.value == "STALLED"
            or item.silence.state
            in {SilenceState.STALL_SUSPECT, SilenceState.OBSERVER_BLIND}
            for item in visible
        )
        signature = (len(visible), hidden, issues, len(queue), self.show_hidden)
        if signature == self._header_signature:
            return
        self._header_signature = signature
        text = Text(" CODEXDECK", style="bold #38bdf8")
        text.append(
            f"   SESSIONS {len(visible)}   ATTENTION {len(queue)}"
            f"   HIDDEN {hidden}   ISSUES {issues}"
            f"   VIEW {'ALL' if self.show_hidden else 'ACTIVE'}",
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

    def _update_status_line(self) -> None:
        value = self._live_status()
        if value == self._status_line_value:
            return
        self._status_line_value = value
        self.query_one("#status-line", Static).update(value)

    def _set_status_message(self, message: str, duration: float = 3.0) -> None:
        self._status_message = message
        self._status_message_until = time.monotonic() + duration
        if self.is_mounted:
            self._update_status_line()

    def _footer_context(self) -> str:
        focused = self.focused
        focused_ids = {
            node.id
            for node in (focused.ancestors_with_self if focused is not None else ())
            if node.id
        }
        if "search" in focused_ids or "terminal-search" in focused_ids:
            return "search"
        if "navigation" in focused_ids:
            return "navigation"
        active = self.query_one("#detail-tabs", Tabs).active
        if active == "terminal-tab":
            return "terminal-list" if "terminal-list" in focused_ids else "terminal-output"
        if active == "diagnosis-tab":
            return "diagnosis"
        return "activity"

    def _update_shortcut_footer(self) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one(ShortcutFooter).show_context(
                self._footer_context(), compact=self.size.width < 80
            )
        except NoMatches:
            return

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._update_shortcut_footer()

    async def _rebuild_navigation(self) -> None:
        if self.rebuilding:
            self.navigation_dirty = True
            return
        self.rebuilding = True
        self.navigation_dirty = False
        try:
            list_view = self.query_one("#session-list", ListView)
            query = self.query_one("#search", Input).value.strip()
        except NoMatches:
            # A final timer tick may arrive after Textual starts unmounting the screen.
            self.rebuilding = False
            return
        items: list[NavigationItem] = []
        for instance in self.snapshot.instances:
            sessions = [
                item
                for item in instance.sessions
                if (self.show_hidden or session_is_visible(item))
                and matches_session(item, query)
            ]
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
                        not session_is_visible(item),
                        not bool(item.attention_request),
                        not bool(item.current_failure),
                        item.silence.state != SilenceState.STALL_SUSPECT,
                        item.silence.state != SilenceState.OBSERVER_BLIND,
                        item.alert_level != "严重",
                        item.process.identity.start_time,
                    ),
                ):
                    hidden_label = session_hidden_label(session)
                    marker, color = session_marker(session)
                    if hidden_label:
                        marker, color = "○", STATE_COLORS["muted"]
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
                    label.append(
                        title_text,
                        style="#94a3b8" if hidden_label else "#f8fafc",
                    )
                    if hidden_label:
                        operation_category = hidden_label
                        operation_label = session_status(session)
                        operation_detail = operation_label
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
        changed = session is not self.selected_session
        self.selected_session = session
        if changed:
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
            if self.query_one("#detail-tabs", Tabs).active == "activity-tab":
                self.call_after_refresh(self._reflow_selected_activity)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        name = event.tab.id.removesuffix("-tab") if event.tab.id else "activity"
        if name != "activity":
            log = self.query_one("#activity-panel", RichLog)
            if log.size.width > 1:
                self._activity_tab_was_at_end = log.is_vertical_scroll_end or (
                    self.follow and log.scroll_y == 0 and not log.has_focus
                )
                self._activity_tab_scroll_y = log.scroll_y
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"
        if name == "activity":
            self.call_after_refresh(self._reflow_activity_after_tab_show)
        elif name == "terminal":
            self._terminal_reflow_attempts = 0
            self.call_after_refresh(self._reflow_terminal_after_layout)
        self._update_shortcut_footer()

    def _reflow_activity_after_tab_show(self) -> None:
        self._reflow_selected_activity(
            was_at_end=self._activity_tab_was_at_end,
            scroll_y=self._activity_tab_scroll_y,
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            await self._rebuild_navigation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.add_class("settled")
        self.query_one("#session-list", ListView).focus()

    def action_request_quit(self) -> None:
        self.exit(self.snapshot)

    def action_help(self) -> None:
        self.push_screen(
            ControlsScreen(
                version=VERSION,
            )
        )

    def action_settings(self) -> None:
        self.push_screen(
            SettingsScreen(
                preferences=self.preferences,
                version=VERSION,
            ),
            self._save_settings,
        )

    def _save_settings(self, preferences: CodexDeckPreferences | None) -> None:
        if preferences is None:
            return
        if self.preferences_file is not None:
            try:
                save_preferences(preferences, self.preferences_file)
            except OSError as error:
                self._set_status_message(f"SETTINGS ERROR · {error}")
                return
        self.preferences = preferences
        self.startup_animation_enabled = preferences.startup_animation
        self.notifications_enabled = preferences.notifications
        self.grouped = preferences.group_sessions and not self._flat_override
        self.show_hidden = preferences.show_hidden_sessions
        self.follow = preferences.follow_output
        self.default_tab = preferences.default_tab
        self.theme = preferences.theme
        self._select_default_tab(preferences.default_tab)
        self.query_one("#activity-panel", RichLog).auto_scroll = self.follow
        self.query_one("#terminal-output", TerminalLog).auto_scroll = self.follow
        self._update_header()
        self._update_status_line()
        self._show_selected_session()
        self.call_later(self._rebuild_navigation)
        self._set_status_message("SETTINGS SAVED")

    def _select_default_tab(self, name: str) -> None:
        if name not in {"activity", "diagnosis", "terminal"}:
            name = "activity"
        self.query_one("#detail-tabs", Tabs).active = f"{name}-tab"
        self.query_one("#detail-content", ContentSwitcher).current = f"{name}-panel"

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

    async def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._update_header()
        self._set_status_message(
            "SHOWING ALL SESSIONS" if self.show_hidden else "ACTIVE SESSIONS ONLY"
        )
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
        self._update_shortcut_footer()

    def action_cycle_theme(self) -> None:
        themes = ("codexdeck-blue", "textual-dark", "textual-light")
        current = themes.index(self.theme) if self.theme in themes else 0
        self.theme = themes[(current + 1) % len(themes)]
        label = {
            "codexdeck-blue": "CLASSIC BLUE",
            "textual-dark": "DARK",
            "textual-light": "LIGHT",
        }[self.theme]
        self._set_status_message(f"THEME · {label}")

    def _zoom_area(self) -> str:
        focused = self.focused
        if focused is not None and any(
            node.id == "navigation" for node in focused.ancestors_with_self
        ):
            return "navigation"
        return "inspector"

    def _set_zoom(self, mode: str) -> None:
        log = self.query_one("#activity-panel", RichLog)
        self._resize_was_at_end = log.is_vertical_scroll_end
        self._resize_scroll_y = log.scroll_y
        self.zoom_mode = mode
        self.screen.set_class(mode == "navigation", "zoom-navigation")
        self.screen.set_class(mode == "inspector", "zoom-inspector")
        if mode != "navigation":
            self.call_after_refresh(self._reflow_activity_after_resize)

    def action_toggle_zoom(self) -> None:
        if self.compact:
            self._set_status_message("ALREADY SINGLE PANE")
            return
        if self.zoom_mode:
            self._set_zoom("")
            self._set_status_message("ZOOM · RESTORED")
            return
        target = self._zoom_area()
        self._set_zoom(target)
        label = "SESSIONS" if target == "navigation" else "INSPECTOR"
        self._set_status_message(f"ZOOM · {label}")

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
        self._update_status_line()
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
        queue = attention_queue(
            item
            for item in self.snapshot.sessions
            if self.show_hidden or session_is_visible(item)
        )
        if not queue:
            self._set_status_message("ATTENTION QUEUE · EMPTY")
            return
        identities = [item.session for item in queue]
        current = (
            self.selected_session.session_identity if self.selected_session else None
        )
        index = (
            (identities.index(current) + 1) % len(identities)
            if current in identities
            else 0
        )
        target = f"session:{queue[index].session.storage_key}"
        list_view = self.query_one("#session-list", ListView)
        for position, item in enumerate(list_view.children):
            if isinstance(item, NavigationItem) and item.key_value == target:
                list_view.index = position
                item.scroll_visible()
                self._select_item(item)
                self.action_show_tab("diagnosis")
                self._set_status_message(
                    f"ATTENTION {index + 1}/{len(queue)} · {queue[index].category.upper()}"
                )
                break

    def action_back(self) -> None:
        if self.zoom_mode:
            self._set_zoom("")
            self._set_status_message("ZOOM · RESTORED")
            return
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
        if not self.sampling:
            return
        if self.sampling_coordinator.begin_manual(time.monotonic()):
            self._start_sample(full=True)

    def _poll_live_events(self) -> None:
        if not self.sampling:
            return
        full = self.sampling_coordinator.begin_due(time.monotonic())
        if full is not None:
            self._start_sample(full=full)

    def _start_sample(self, *, full: bool) -> None:
        self._sample_worker(full, self.snapshot)

    @work(thread=True, group="snapshot")
    def _sample_worker(self, full: bool, current: MonitorSnapshot) -> None:
        try:
            snapshot = self.engine.sample() if full else self.engine.refresh_events(current)
        except Exception as error:  # UI worker must report rather than tear down terminal state.
            self.post_message(SampleCompleted(None, str(error)))
            return
        self.post_message(SampleCompleted(snapshot))

    @work(thread=True, group="snapshot")
    def _initial_sample_worker(self) -> None:
        try:
            self.engine.baseline()
            snapshot = self.engine.sample()
        except Exception as error:
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
        self.sampling_coordinator.finish()
        if error:
            self._show_collector_error(error)
        elif snapshot is not None and snapshot is not self.snapshot:
            self._collector_error = ""
            self._apply_snapshot(snapshot)
        else:
            had_error = bool(self._collector_error)
            self._collector_error = ""
            if had_error:
                self._update_status_line()
        if self._initial_preparing:
            self._initial_preparing = False
            self._startup_data_ready = True
            if self._startup_animation_complete:
                self._dismiss_startup()

    def _show_collector_error(self, message: str) -> None:
        if message == self._collector_error:
            return
        self._collector_error = message
        self._update_status_line()

    def _notify_transitions(self, previous: MonitorSnapshot, current: MonitorSnapshot) -> None:
        if not self.notifications_enabled:
            return
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
            if self.show_hidden or session_is_visible(session)
        }
        self.collapsed.intersection_update(active)
        self._update_metrics()
        self._update_status_line()
        self.call_later(self._rebuild_navigation)


def run_textual_tui(
    engine: MonitorEngine,
    use_color: bool,
    flat: bool,
) -> MonitorSnapshot:
    """Start Textual and prepare the initial snapshot behind the optional startup layer."""
    preference_file = preferences_path()
    preferences = load_preferences(preference_file)
    if preferences.startup_animation:
        snapshot = MonitorSnapshot("", engine.interval, [])
        prepare_on_start = True
    else:
        engine.baseline()
        snapshot = engine.sample()
        prepare_on_start = False
    app = CodexDeckApp(
        engine,
        snapshot,
        use_color=use_color,
        flat=flat,
        startup_animation=preferences.startup_animation,
        preferences=preferences,
        preferences_file=preference_file,
        prepare_on_start=prepare_on_start,
    )
    return app.run() or snapshot

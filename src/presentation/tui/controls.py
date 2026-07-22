"""CodexDeck-owned shortcut reference and contextual footer."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Label, Select, Static, Switch

from preferences import CodexDeckPreferences


@dataclass(frozen=True)
class ShortcutSpec:
    """One application shortcut and its user-facing documentation."""

    key: str
    action: str
    label: str
    section: str
    detail: str
    show: bool = False


SHORTCUTS = (
    ShortcutSpec("q", "request_quit", "q", "系统", "退出 CodexDeck，返回原来的终端。", True),
    ShortcutSpec(
        "ctrl+c",
        "request_quit",
        "Ctrl+C",
        "系统",
        "立即退出 CodexDeck；作用与 q 相同，用于终端中的通用中断习惯。",
    ),
    ShortcutSpec(
        "question_mark",
        "help",
        "?",
        "系统",
        "打开这份 CodexDeck 控制说明；面板中再次按 ?、Esc 或 Enter 关闭。",
        True,
    ),
    ShortcutSpec(
        "s",
        "settings",
        "s",
        "系统",
        "打开 CodexDeck 设置。",
        True,
    ),
    ShortcutSpec(
        "slash",
        "search",
        "/",
        "搜索与跟随",
        "搜索当前区域：会话视图过滤会话，Terminal 中只搜索已保留的当前进程输出。",
        True,
    ),
    ShortcutSpec(
        "r",
        "sample_now",
        "r",
        "系统",
        "立即执行完整采样，包括进程、SQLite、rollout、socket 和终端状态。",
        True,
    ),
    ShortcutSpec(
        "g",
        "toggle_grouped",
        "g",
        "视图",
        "在按真实工作区分组和扁平会话列表之间切换；只影响当前运行。",
        True,
    ),
    ShortcutSpec(
        "h",
        "toggle_hidden",
        "h",
        "视图",
        "切换仅活跃会话和全部会话；全部视图包含退出或已转入后台的会话。",
        True,
    ),
    ShortcutSpec(
        "right_square_bracket",
        "next_anomaly",
        "]",
        "导航",
        "按优先级进入下一个待处理项，并打开对应会话的 Diagnosis。",
        True,
    ),
    ShortcutSpec("1", "show_tab('activity')", "1", "视图", "打开 Activity 语义时间线。"),
    ShortcutSpec("2", "show_tab('diagnosis')", "2", "视图", "打开 Diagnosis 结论与异常详情。"),
    ShortcutSpec("3", "show_tab('terminal')", "3", "视图", "打开当前运行后台任务的只读终端。"),
    ShortcutSpec(
        "f",
        "toggle_follow",
        "f",
        "搜索与跟随",
        "开关 Activity 与 Terminal 自动跟随；离开末尾后需先滚动到底部再开启。",
        True,
    ),
    ShortcutSpec(
        "n",
        "next_match",
        "n",
        "搜索与跟随",
        "Terminal 搜索时跳到下一个匹配，抵达末项后循环。",
    ),
    ShortcutSpec(
        "shift+n",
        "previous_match",
        "Shift+N",
        "搜索与跟随",
        "Terminal 搜索时跳到上一个匹配。",
    ),
    ShortcutSpec("j", "cursor_down", "j", "导航", "向下移动选择；日志获得焦点时向下滚动。"),
    ShortcutSpec("k", "cursor_up", "k", "导航", "向上移动选择；日志获得焦点时向上滚动。"),
    ShortcutSpec(
        "escape",
        "back",
        "Esc",
        "导航",
        "取消搜索、退出放大、从终端输出返回任务列表，或在窄屏返回会话列表。",
    ),
    ShortcutSpec(
        "t",
        "cycle_theme",
        "t",
        "视图",
        "在经典蓝色、深色和浅色主题之间临时切换。",
        True,
    ),
    ShortcutSpec(
        "z",
        "toggle_zoom",
        "z",
        "视图",
        "放大当前会话列表或 Inspector 页面；再次按 z 或 Esc 恢复。",
        True,
    ),
)


APP_BINDINGS = [
    Binding(spec.key, spec.action, spec.label, show=spec.show) for spec in SHORTCUTS
]


def keyboard_reference() -> str:
    """Build the detailed control reference from the binding source of truth."""

    sections = ("导航", "视图", "搜索与跟随", "系统")
    lines = ["CODEXDECK CONTROLS", ""]
    for section in sections:
        lines.append(section.upper())
        for spec in SHORTCUTS:
            if spec.section == section:
                lines.append(f"  {spec.label:<10} {spec.detail}")
        if section == "导航":
            lines.extend(
                (
                    "  ↑ / ↓      与 j / k 相同，用于移动选择或滚动当前日志。",
                    "  Enter      展开工作区、进入窄屏详情，或从终端任务列表进入输出。",
                    "  Tab        移动到下一个可操作区域；Shift+Tab 反向移动。",
                    "  PgUp/PgDn  按页滚动当前列表或日志；Home/End 跳到首尾。",
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip()


FOOTER_HINTS = {
    "navigation": (
        ("↑↓", "选择", "down"),
        ("Enter", "打开", "enter"),
        ("/", "搜索", "slash"),
        ("]", "异常", "right_square_bracket"),
        ("s", "设置", "s"),
        ("?", "帮助", "question_mark"),
    ),
    "activity": (
        ("1", "Activity", "1"),
        ("2", "Diagnosis", "2"),
        ("3", "Terminal", "3"),
        ("f", "跟随", "f"),
        ("z", "放大", "z"),
        ("?", "帮助", "question_mark"),
    ),
    "diagnosis": (
        ("1", "Activity", "1"),
        ("2", "Diagnosis", "2"),
        ("3", "Terminal", "3"),
        ("Enter", "展开", "enter"),
        ("z", "放大", "z"),
        ("?", "帮助", "question_mark"),
    ),
    "terminal-list": (
        ("↑↓", "任务", "down"),
        ("Enter", "输出", "enter"),
        ("/", "搜索", "slash"),
        ("f", "跟随", "f"),
        ("z", "放大", "z"),
        ("?", "帮助", "question_mark"),
    ),
    "terminal-output": (
        ("jk", "滚动", "j"),
        ("/", "搜索", "slash"),
        ("n/N", "匹配", "n"),
        ("f", "跟随", "f"),
        ("Esc", "返回", "escape"),
        ("?", "帮助", "question_mark"),
    ),
    "search": (
        ("Enter", "确认", "enter"),
        ("Esc", "清除", "escape"),
        ("Tab", "切换焦点", "tab"),
        ("?", "帮助", "question_mark"),
    ),
}

FOOTER_CONTEXT_LABELS = {
    "navigation": "SESSIONS",
    "activity": "ACTIVITY",
    "diagnosis": "DIAGNOSIS",
    "terminal-list": "TASKS",
    "terminal-output": "OUTPUT",
    "search": "SEARCH",
}


class ShortcutKey(Static):
    """One clickable footer shortcut."""

    ALLOW_SELECT = False

    def __init__(
        self,
        key: str,
        description: str,
        trigger: str,
        *,
        classes: str = "",
    ) -> None:
        super().__init__(classes=f"shortcut-key {classes}")
        self.key = key
        self.description = description
        self.trigger = trigger
        self.tooltip = f"{key} · {description}"

    def render(self) -> Text:
        label = Text()
        if self.has_class("shortcut-quit"):
            label.append(self.key, style="bold #fecdd3")
            label.append(f" {self.description}", style="#fda4af")
        else:
            label.append(self.key, style="bold #d6f3ff")
            label.append(f" {self.description}", style="#aabbd0")
        return label

    def on_mouse_down(self) -> None:
        self.app.simulate_key(self.trigger)


class ShortcutFooter(Horizontal):
    """A clickable, context-aware footer without command-palette chrome."""

    ALLOW_SELECT = False
    context = reactive("navigation", recompose=True)
    compact = reactive(False, recompose=True)

    def compose(self) -> ComposeResult:
        yield Static(
            FOOTER_CONTEXT_LABELS.get(self.context, "CODEXDECK"),
            classes="shortcut-mode",
        )
        hints = FOOTER_HINTS.get(self.context, FOOTER_HINTS["navigation"])
        if self.compact:
            hints = hints[:3]
        for key, description, trigger in hints:
            yield ShortcutKey(key, description, trigger)
        yield ShortcutKey("q", "退出", "q", classes="shortcut-quit")

    def show_context(self, context: str, *, compact: bool) -> None:
        self.context = context
        self.compact = compact


REFERENCE_EXTRAS = (
    ShortcutSpec("up_down", "", "↑ / ↓", "导航", "与 j / k 相同，用于移动选择或滚动当前日志。"),
    ShortcutSpec(
        "enter",
        "",
        "Enter",
        "导航",
        "展开工作区、进入窄屏详情，或从终端任务列表进入输出。",
    ),
    ShortcutSpec(
        "tab",
        "",
        "Tab",
        "导航",
        "移动到下一个可操作区域；Shift+Tab 反向移动。",
    ),
    ShortcutSpec(
        "page",
        "",
        "PgUp/PgDn",
        "导航",
        "按页滚动当前列表或日志；Home/End 跳到首尾。",
    ),
)


REFERENCE_ORDER = {
    "导航": ("↑ / ↓", "j", "k", "Enter", "Tab", "Esc", "]", "PgUp/PgDn"),
    "视图": ("1", "2", "3", "g", "h", "z", "t"),
    "搜索与跟随": ("/", "f", "n", "Shift+N"),
    "系统": ("r", "s", "?", "q"),
}


def reference_specs(section: str) -> tuple[ShortcutSpec, ...]:
    """Return controls in the order users encounter them in the interface."""

    available = {
        spec.label: spec
        for spec in (*SHORTCUTS, *REFERENCE_EXTRAS)
        if spec.section == section
    }
    return tuple(available[label] for label in REFERENCE_ORDER[section])


class ControlsScroll(VerticalScroll):
    """Keyboard-scrollable body for the control reference."""

    can_focus = True
    BINDINGS = [
        Binding("j", "scroll_down", "向下", show=False),
        Binding("k", "scroll_up", "向上", show=False),
    ]


class ControlSection(Vertical):
    """A visually grouped set of keyboard actions."""

    def __init__(self, number: int, title: str, specs: tuple[ShortcutSpec, ...]) -> None:
        super().__init__(classes="control-section")
        self.number = number
        self.title = title
        self.specs = specs

    def compose(self) -> ComposeResult:
        heading = Text()
        heading.append(f"{self.number:02d}", style="bold #38bdf8")
        heading.append(f"  {self.title}", style="bold #e2e8f0")
        yield Static(heading, classes="control-section-title")
        for spec in self.specs:
            with Horizontal(classes="control-row"):
                yield Static(spec.label, classes="control-key")
                yield Static(spec.detail, classes="control-detail")


class ControlsScreen(ModalScreen[None]):
    """Detailed CodexDeck-specific keyboard and runtime reference."""

    BINDINGS = [Binding("escape,question_mark,enter", "dismiss", "关闭")]

    def __init__(self, *, version: str) -> None:
        super().__init__()
        self.version = version

    def compose(self) -> ComposeResult:
        with Container(id="controls-dialog"):
            with Horizontal(id="controls-header"):
                yield Label("CODEXDECK", id="controls-brand")
                yield Label("CONTROL REFERENCE", id="controls-title")
                yield Label(f"v{self.version}", id="controls-version")
            yield Static(
                "当前区域的常用操作会出现在底部；这里保留完整键盘地图和运行边界。",
                id="controls-subtitle",
            )
            with ControlsScroll(id="controls-scroll"):
                sections = ("导航", "视图", "搜索与跟随", "系统")
                for number, section in enumerate(sections, 1):
                    yield ControlSection(number, section.upper(), reference_specs(section))
            with Horizontal(id="controls-hint"):
                yield Label("↑↓ / jk  滚动", classes="controls-hint-left")
                yield Label("? / Esc / Enter  关闭", classes="controls-hint-right")

    def on_mount(self) -> None:
        self._update_size_class(self.app.size.width)
        self.query_one(ControlsScroll).focus()

    def on_resize(self, event: events.Resize) -> None:
        self._update_size_class(event.size.width)

    def _update_size_class(self, width: int) -> None:
        self.set_class(width < 72, "narrow")


class SettingsScreen(ModalScreen[CodexDeckPreferences]):
    """Persistent settings surface for CodexDeck-owned preferences."""

    BINDINGS = [
        Binding("escape", "cancel", "放弃"),
        Binding("s", "save", "保存"),
    ]

    def __init__(self, *, preferences: CodexDeckPreferences, version: str) -> None:
        super().__init__()
        self.preferences = preferences
        self.version = version

    def compose(self) -> ComposeResult:
        with Container(id="settings-dialog"):
            with Horizontal(id="settings-header"):
                yield Label("CODEXDECK", id="settings-brand")
                yield Label("SETTINGS", id="settings-title")
                yield Label(f"v{self.version}", id="settings-version")
            yield Static(
                "设置仅保存到 CodexDeck 自己的配置目录，Codex 配置保持原样。",
                id="settings-subtitle",
            )
            with VerticalScroll(id="settings-scroll"):
                yield Static("启动体验", classes="setting-section")
                yield self._switch_row(
                    "启动动画",
                    "下次启动时完整播放品牌动画，同时在后台准备首个快照",
                    "startup-animation-switch",
                    self.preferences.startup_animation,
                )

                yield Static("默认视图", classes="setting-section")
                yield self._switch_row(
                    "按工作区分组",
                    "按真实工作目录组织会话；命令行 --flat 仍可临时覆盖",
                    "group-sessions-switch",
                    self.preferences.group_sessions,
                )
                yield self._switch_row(
                    "显示隐藏会话",
                    "启动后同时显示已退出或已确认转入后台的会话",
                    "show-hidden-switch",
                    self.preferences.show_hidden_sessions,
                )
                with Horizontal(classes="setting-row"):
                    with Vertical(classes="setting-copy"):
                        yield Static("默认 Inspector 页面", classes="setting-label")
                        yield Static("选择会话后优先显示的详情页面", classes="setting-detail")
                    yield Select(
                        (("Activity", "activity"), ("Diagnosis", "diagnosis"), ("Terminal", "terminal")),
                        value=self.preferences.default_tab,
                        allow_blank=False,
                        id="default-tab-select",
                    )

                yield Static("阅读与提醒", classes="setting-section")
                yield self._switch_row(
                    "自动跟随新内容",
                    "启动时让 Activity 与 Terminal 跟随最新内容",
                    "follow-output-switch",
                    self.preferences.follow_output,
                )
                yield self._switch_row(
                    "状态通知",
                    "显示等待操作、失败、停顿、compact 和恢复通知",
                    "notifications-switch",
                    self.preferences.notifications,
                )
                with Horizontal(classes="setting-row"):
                    with Vertical(classes="setting-copy"):
                        yield Static("界面主题", classes="setting-label")
                        yield Static("选择经典蓝色、深色或浅色主题", classes="setting-detail")
                    yield Select(
                        (
                            ("经典蓝色", "codexdeck-blue"),
                            ("深色", "textual-dark"),
                            ("浅色", "textual-light"),
                        ),
                        value=self.preferences.theme,
                        allow_blank=False,
                        id="theme-select",
                    )
            yield Static("Esc  放弃修改    s  保存设置", id="settings-hint")

    @staticmethod
    def _switch_row(
        label: str,
        detail: str,
        switch_id: str,
        value: bool,
    ) -> Horizontal:
        row = Horizontal(classes="setting-row")
        row.compose_add_child(
            Vertical(
                Static(label, classes="setting-label"),
                Static(detail, classes="setting-detail"),
                classes="setting-copy",
            )
        )
        row.compose_add_child(Switch(value=value, id=switch_id))
        return row

    def on_mount(self) -> None:
        self.set_class(self.app.size.width < 64, "narrow")
        self.query_one("#startup-animation-switch", Switch).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 64, "narrow")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        theme = self.query_one("#theme-select", Select).value
        default_tab = self.query_one("#default-tab-select", Select).value
        self.dismiss(
            CodexDeckPreferences(
                startup_animation=self.query_one(
                    "#startup-animation-switch", Switch
                ).value,
                group_sessions=self.query_one("#group-sessions-switch", Switch).value,
                show_hidden_sessions=self.query_one("#show-hidden-switch", Switch).value,
                follow_output=self.query_one("#follow-output-switch", Switch).value,
                notifications=self.query_one("#notifications-switch", Switch).value,
                theme=str(theme),
                default_tab=str(default_tab),
            )
        )

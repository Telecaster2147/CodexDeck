"""Plain-text output and interactive terminal user interface."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import sys
import termios
import textwrap
import time
import tty
from dataclasses import asdict
from datetime import datetime
from typing import Sequence

from .activity import ActivityTracker, SseHealthTracker
from .collectors import compact_path, parse_endpoint
from .config import (
    ACTIVITY_LABELS,
    ALT_SCREEN_ENTER,
    ALT_SCREEN_LEAVE,
    ANSI,
    CURSOR_HIDE,
    CURSOR_SHOW,
    ERASE_LINE,
    INVERSE,
    SCREEN_HOME_CLEAR,
    STATE_ACTIVE,
    STATE_AUXILIARY,
    STATE_DISCONNECTED,
    STATE_HEALTHY_IDLE,
    STATE_NETWORK_STALL,
    STATE_NO_OUTBOUND,
    STATE_UPSTREAM_WAIT,
    STATUS_TEXT,
)
from .models import ConversationActivity, ProcessAssessment, ProcessInfo, SseHealth
from .monitoring import LiveSampler
from .utils import format_duration


def colorize(text: str, health: str, enabled: bool) -> str:
    if not enabled:
        return text
    color = {
        STATE_ACTIVE: ANSI["green"],
        STATE_HEALTHY_IDLE: ANSI["green"],
        STATE_UPSTREAM_WAIT: ANSI["yellow"],
        STATE_NETWORK_STALL: ANSI["red"],
        STATE_DISCONNECTED: ANSI["red"],
        STATE_NO_OUTBOUND: ANSI["dim"],
        STATE_AUXILIARY: ANSI["dim"],
    }.get(health, "")
    return f"{color}{text}{ANSI['reset']}" if color else text


def friendly_command(process: ProcessInfo) -> str:
    parts = process.args.split()
    if not parts:
        return process.command
    executable_index = 0
    if process.command in {"node", "nodejs"} and len(parts) > 1:
        executable_index = 1
    tail = parts[executable_index + 1 :]
    return " ".join(["codex", *tail]) if tail else "codex"


def short_session_id(session_id: str) -> str:
    if len(session_id) <= 16:
        return session_id or "-"
    return f"{session_id[:8]}…{session_id[-4:]}"


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value}B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f}K"
    return f"{value / (1024 * 1024):.1f}M"


def fit(text: str, width: int) -> str:
    text = " ".join(text.split())
    if not text:
        return "-"
    return textwrap.shorten(text, width=max(8, width), placeholder="…")


def print_wrapped(label: str, text: str, width: int, max_lines: int = 2) -> None:
    available = max(20, width - len(label) - 4)
    lines = textwrap.wrap(" ".join(text.split()), width=available) or ["-"]
    for index, line in enumerate(lines[:max_lines]):
        prefix = label if index == 0 else " " * len(label)
        suffix = "…" if index == max_lines - 1 and len(lines) > max_lines else ""
        print(f"│ {prefix}{line}{suffix}")


def render_sse_health(health: SseHealth, width: int, use_color: bool) -> None:
    lookback = format_duration(health.lookback_seconds)
    if not health.available:
        print(f"[SSE 日志检测不可用] {health.error}")
        print("─" * width)
        return

    if health.has_sse_timeout:
        active_note = ""
        if health.active_session_idle_timeouts:
            active_note = f"，其中 {health.active_session_idle_timeouts} 次属于当前会话"
        age = (
            f"，最近一次 {format_duration(health.last_idle_timeout_age_seconds)} 前"
            if health.last_idle_timeout_age_seconds is not None
            else ""
        )
        heading = "[SSE_TIMEOUT] 远程压缩流近期故障"
        if use_color:
            heading = f"{ANSI['bold']}{ANSI['red']}{heading}{ANSI['reset']}"
        print(heading)
        print(
            "最近 "
            f"{lookback} 检测到 {health.recent_idle_timeouts} 次 "
            f"'idle timeout waiting for SSE'{active_note}{age}。"
        )
        print(
            "这是 Codex 运行时记录的真实压缩流失败；该请求可能已由自动重试恢复，"
            "因此它不单独表示当前仍在阻塞。"
        )
    elif health.recent_auto_compactions:
        heading = "[SSE 观察] 最近有远程上下文压缩"
        if use_color:
            heading = f"{ANSI['yellow']}{heading}{ANSI['reset']}"
        active_note = (
            f"，当前会话 {health.active_session_auto_compactions} 次"
            if health.active_session_auto_compactions
            else ""
        )
        print(f"{heading}：最近 {lookback} 共 {health.recent_auto_compactions} 次{active_note}。")
    else:
        heading = "[SSE 正常]"
        if use_color:
            heading = f"{ANSI['green']}{heading}{ANSI['reset']}"
        print(f"{heading} 最近 {lookback} 未发现远程压缩 SSE 空闲超时。")

    if health.recent_websocket_401s:
        print(
            "[认证提醒] "
            f"最近 {lookback} 还有 {health.recent_websocket_401s} 次官方 Responses WebSocket 401；"
            "请核对 provider 与认证配置。"
        )
    print("─" * width)


def render_text(
    assessments: Sequence[ProcessAssessment],
    sse_health: SseHealth,
    interval: float,
    use_color: bool,
    show_auxiliary: bool,
) -> None:
    visible = [
        item
        for item in assessments
        if item.process.role != "app-server"
        and (item.process.role == "session" or show_auxiliary)
    ]
    terminal_width = shutil.get_terminal_size((120, 32)).columns
    width = max(88, min(terminal_width, 150))
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Codex Session & Network Monitor  {timestamp}")
    print(f"采样 {interval:g}s  |  进程 {len(assessments)}  |  会话/服务 {len(visible)}")
    print("═" * width)
    render_sse_health(sse_health, width, use_color)
    if not visible:
        print("未发现符合条件的 Codex 进程。")
        return

    for item in visible:
        process = item.process
        label = STATUS_TEXT[item.health]
        title = process.session_title or (
            "VS Code Codex App Server" if process.role == "app-server" else f"Codex PID {process.pid}"
        )
        title_room = max(20, width - 38)
        title = fit(title, title_room)
        colored_title = f"{ANSI['bold']}{ANSI['cyan']}{title}{ANSI['reset']}" if use_color else title
        print(f"┌─ {colored_title}  [{colorize(label, item.health, use_color)}]")
        config_bits = [
            f"PID {process.pid}",
            f"session {short_session_id(process.session_id)}",
            f"role {process.role}",
            f"CPU {process.cpu_percent:.1f}%",
            f"age {format_duration(process.elapsed_seconds)}",
        ]
        if process.model:
            model = process.model
            if process.reasoning_effort:
                model += f"/{process.reasoning_effort}"
            config_bits.insert(2, model)
        print(f"│ {' | '.join(config_bits)}")
        print(
            f"│ 目录 {compact_path(process.cwd)}  |  命令 {friendly_command(process)}"
        )
        if process.current_task:
            print_wrapped("当前：", process.current_task, width, max_lines=2)
        elif process.role == "app-server":
            print("│ 当前：VS Code 后台 Codex 服务")
        print(f"│ 网络卡死：{item.network_hang}  |  wait={process.wait_channel}")
        print_wrapped("判断：", item.reason, width, max_lines=2)
        for connection in item.connections:
            delta = connection.sent_delta + connection.received_delta
            idle = "未知" if connection.idle_seconds is None else f"{connection.idle_seconds:.1f}s"
            _, local_port = parse_endpoint(connection.local)
            route_name = {"proxy": "代理", "external": "外网", "lan": "局域网"}.get(
                connection.route, connection.route
            )
            print(
                f"│ ↳ {route_name:<3} :{local_port or '-'} → {connection.peer}  "
                f"{connection.state}  Q={connection.recv_q}/{connection.send_q}  "
                f"Δ={human_bytes(delta)}  idle={idle}  "
                f"{colorize(STATUS_TEXT[connection.health], connection.health, use_color)}"
            )
        print("└" + "─" * (width - 1))


def assessment_to_dict(item: ProcessAssessment) -> dict[str, object]:
    data = asdict(item)
    data["status_text"] = STATUS_TEXT[item.health]
    return data


STYLE_CODES = {
    "": "",
    "bold": ANSI["bold"],
    "cyan": ANSI["cyan"],
    "red": ANSI["bold"] + ANSI["red"],
    "dim": ANSI["dim"],
    "selected": INVERSE,
}


def clip_line(text: str, width: int) -> str:
    clean = " ".join(text.replace("\t", " ").splitlines())
    if len(clean) <= width:
        return clean
    if width <= 1:
        return clean[:width]
    return clean[: width - 1] + "…"


def styled(text: str, style: str, use_color: bool) -> str:
    code = STYLE_CODES.get(style, "") if use_color else ""
    return f"{code}{text}{ANSI['reset']}" if code else text


def emit_frame(
    rows: Sequence[tuple[str, str]],
    width: int,
    height: int,
    use_color: bool,
) -> None:
    visible = list(rows[:height])
    if len(visible) < height:
        visible.extend([("", "")] * (height - len(visible)))
    output = [SCREEN_HOME_CLEAR]
    for text, style in visible:
        output.append(ERASE_LINE + styled(clip_line(text, width), style, use_color) + "\r\n")
    sys.stdout.write("".join(output))
    sys.stdout.flush()


def visible_cli_assessments(
    assessments: Sequence[ProcessAssessment],
    show_auxiliary: bool = False,
) -> list[ProcessAssessment]:
    return [
        item
        for item in assessments
        if item.process.role != "app-server"
        and (item.process.role == "session" or show_auxiliary)
    ]


def activity_sort_key(
    item: ProcessAssessment,
    activities: dict[str, ConversationActivity],
) -> tuple[int, float, int]:
    activity = activities.get(item.process.session_id)
    if activity and activity.alert:
        rank = 500 if activity.alert_level == "严重" else 450
    elif activity and activity.compacting:
        rank = 400
    elif activity and activity.phase in {
        ACTIVITY_LABELS["REASONING"],
        ACTIVITY_LABELS["MESSAGE"],
        ACTIVITY_LABELS["TOOL_BUILD"],
        ACTIVITY_LABELS["TOOL_CALL"],
    }:
        rank = 300
    elif item.health == STATE_ACTIVE:
        rank = 250
    elif item.health in {STATE_UPSTREAM_WAIT, STATE_NETWORK_STALL}:
        rank = 200
    else:
        rank = 100
    last_at = activity.phase_since if activity and activity.phase_since else 0.0
    return (-rank, -last_at, item.process.pid)


def conversation_title(process: ProcessInfo) -> str:
    return process.session_title or fit(process.current_task, 52) or f"Codex {process.pid}"


def age_text(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return format_duration(max(0, int(time.time() - timestamp)))


def compact_banner(process: ProcessInfo, activity: ConversationActivity) -> str:
    usage = ""
    if activity.token_used is not None and activity.token_limit:
        usage = f" | {activity.token_used // 1000}K / {activity.token_limit // 1000}K"
    return (
        f"[COMPACTING] {conversation_title(process)} | {activity.compact_mode}压缩 | "
        f"{activity.compact_phase} | {format_duration(activity.compact_age_seconds)}{usage}"
    )


def main_rows(
    assessments: Sequence[ProcessAssessment],
    activities: dict[str, ConversationActivity],
    selected_id: str,
    list_offset: int,
    width: int,
    height: int,
) -> tuple[list[tuple[str, str]], list[ProcessAssessment]]:
    items = sorted(
        visible_cli_assessments(assessments),
        key=lambda item: activity_sort_key(item, activities),
    )
    now_text = datetime.now().astimezone().strftime("%F %T")
    alerts = [
        (item, activities.get(item.process.session_id))
        for item in items
        if activities.get(item.process.session_id)
        and activities[item.process.session_id].alert
    ]
    compacts = [
        (item, activities.get(item.process.session_id))
        for item in items
        if activities.get(item.process.session_id)
        and activities[item.process.session_id].compacting
    ]
    rows: list[tuple[str, str]] = [
        (f"CodexNet 2.0  {now_text}  对话 {len(items)}  告警 {len(alerts)}", "bold"),
    ]
    for item, activity in compacts[:2]:
        assert activity is not None
        rows.append((compact_banner(item.process, activity), "red"))
    if alerts:
        item, activity = alerts[0]
        assert activity is not None
        suffix = f"；另有 {len(alerts) - 1} 条" if len(alerts) > 1 else ""
        rows.append(
            (
                f"[{activity.alert}] {conversation_title(item.process)} | "
                f"{activity.alert_level} {format_duration(activity.alert_age_seconds)}{suffix}",
                "red",
            )
        )
    rows.append(("─" * width, "dim"))

    if width >= 110:
        rows.append(("   PID     对话                           状态                    活动静默    网络", "bold"))
    elif width >= 78:
        rows.append(("   PID     对话                         状态                    静默", "bold"))
    else:
        rows.append(("   PID     对话                 状态", "bold"))

    detail_height = 8
    footer_height = 2
    list_height = max(3, height - len(rows) - detail_height - footer_height)
    if not items:
        rows.append(("未发现 CLI Codex 对话；VS Code app-server 已按配置隐藏。", "dim"))
    else:
        index = next(
            (idx for idx, item in enumerate(items) if item.process.session_id == selected_id),
            0,
        )
        if index < list_offset:
            list_offset = index
        if index >= list_offset + list_height:
            list_offset = index - list_height + 1
        for item in items[list_offset : list_offset + list_height]:
            process = item.process
            activity = activities.get(process.session_id, ConversationActivity(process.session_id, process.pid))
            marker = ">" if process.session_id == selected_id else " "
            if activity.alert:
                status = activity.alert
            elif activity.compacting:
                status = "COMPACTING"
            else:
                status = activity.phase
            silent = age_text(activity.last_meaningful_at)
            title = conversation_title(process)
            if width >= 110:
                line = (
                    f"{marker} {process.pid:<7} {title:<30.30} {status:<23.23} "
                    f"{silent:<10} {STATUS_TEXT[item.health]}"
                )
            elif width >= 78:
                line = f"{marker} {process.pid:<7} {title:<28.28} {status:<23.23} {silent:<8}"
            else:
                line = f"{marker} {process.pid:<7} {title:<20.20} {status}"
            style = "selected" if marker == ">" else ("red" if activity.alert else "")
            rows.append((line, style))

    rows.append(("─" * width, "dim"))
    selected = next(
        (item for item in items if item.process.session_id == selected_id),
        items[0] if items else None,
    )
    if selected:
        process = selected.process
        activity = activities.get(process.session_id, ConversationActivity(process.session_id, process.pid))
        model = process.model + (f"/{process.reasoning_effort}" if process.reasoning_effort else "")
        rows.extend(
            [
                (f"选中  {conversation_title(process)}  PID {process.pid}  {short_session_id(process.session_id)}", "cyan"),
                (f"目录  {compact_path(process.cwd)}  模型 {model or '-'}", ""),
                (f"上游  {activity.phase}  已持续 {age_text(activity.phase_since)}", "red" if activity.alert else ""),
                (
                    f"事件  实际输出 {age_text(activity.last_meaningful_at)} 前 | "
                    f"keepalive {age_text(activity.last_keepalive_at)} 前",
                    "",
                ),
                (f"网络  {STATUS_TEXT[selected.health]} | {selected.reason}", ""),
            ]
        )
        if activity.alert:
            rows.append((f"判断  {activity.alert_reason}", "red"))
        elif activity.compacting:
            rows.append((compact_banner(process, activity), "red"))
        else:
            rows.append(("判断  当前未触发阶段停顿告警。", "dim"))
    return rows, items


def event_rows(
    process: ProcessInfo,
    activity: ConversationActivity,
    width: int,
    height: int,
    scroll: int,
    query: str,
    alerts_only: bool,
    cleared_before: float,
) -> tuple[list[tuple[str, str]], int, int]:
    events = [
        event
        for event in activity.events
        if not (
            event.timestamp <= cleared_before and event.kind in {"ALERT", "RECOVERED"}
        )
    ]
    if alerts_only:
        events = [
            event
            for event in events
            if event.kind in {"ALERT", "RECOVERED", "SSE_TIMEOUT", "COMPACT_FAIL", "RESPONSE_FAIL"}
        ]
    if query:
        needle = query.casefold()
        events = [
            event
            for event in events
            if needle in f"{event.kind} {event.summary} {event.detail}".casefold()
        ]
    header = [
        (f"日志  {conversation_title(process)}  PID {process.pid}  最近 15 分钟", "bold"),
        (
            f"当前  {activity.alert or activity.phase}"
            + (f" | 搜索: {query}" if query else "")
            + (" | 仅异常" if alerts_only else ""),
            "red" if activity.alert else "cyan",
        ),
        ("─" * width, "dim"),
    ]
    page_size = max(1, height - len(header) - 2)
    max_scroll = max(0, len(events) - page_size)
    scroll = max(0, min(scroll, max_scroll))
    start = max(0, len(events) - page_size - scroll)
    page = events[start : start + page_size]
    rows = list(header)
    if not page:
        rows.append(("没有符合条件的关键事件。", "dim"))
    for event in page:
        stamp = datetime.fromtimestamp(event.timestamp).astimezone().strftime("%H:%M:%S")
        detail = f" | {event.detail}" if event.detail else ""
        style = "red" if event.kind in {"ALERT", "SSE_TIMEOUT", "COMPACT_FAIL", "RESPONSE_FAIL"} else ""
        rows.append((f"{stamp}  {event.kind:<17} {event.summary}{detail}", style))
    return rows, scroll, max_scroll


class RawTerminal:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved: list[object] | None = None

    def __enter__(self) -> "RawTerminal":
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(ALT_SCREEN_ENTER + CURSOR_HIDE + SCREEN_HOME_CLEAR)
        sys.stdout.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write(CURSOR_SHOW + ALT_SCREEN_LEAVE)
        sys.stdout.flush()

    def key(self, timeout: float | None) -> str:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return ""
        first = os.read(self.fd, 1)
        if first == b"\x1b":
            suffix = b""
            while select.select([self.fd], [], [], 0.01)[0]:
                suffix += os.read(self.fd, 1)
            return {
                b"[A": "UP",
                b"[B": "DOWN",
                b"[5~": "PGUP",
                b"[6~": "PGDN",
                b"[H": "HOME",
                b"[F": "END",
            }.get(suffix, "ESC")
        if first in {b"\r", b"\n"}:
            return "ENTER"
        if first in {b"\x7f", b"\x08"}:
            return "BACKSPACE"
        if first == b"\t":
            return "TAB"
        if first == b"\x03":
            raise KeyboardInterrupt
        return first.decode("utf-8", errors="ignore")


def prompt_search(terminal: RawTerminal, width: int, height: int, initial: str) -> str | None:
    value = initial
    while True:
        rows = [("", "")] * max(0, height - 2)
        rows.extend(
            [
                ("输入搜索词，Enter 确认，Esc 取消", "bold"),
                (f"/ {value}", "cyan"),
            ]
        )
        emit_frame(rows, width, height, True)
        key = terminal.key(None)
        if key == "ENTER":
            return value
        if key == "ESC":
            return None
        if key == "BACKSPACE":
            value = value[:-1]
        elif len(key) == 1 and key.isprintable():
            value += key


def help_rows(width: int) -> list[tuple[str, str]]:
    return [
        ("CodexNet 快捷键", "bold"),
        ("", ""),
        ("↑/↓ 或 j/k   选择对话或滚动日志", ""),
        ("Enter / l    打开选中对话的关键事件日志", ""),
        ("Esc          返回主界面", ""),
        ("PgUp/PgDn   日志翻页；g/G 到首/尾", ""),
        ("f            开启或关闭日志实时跟随", ""),
        ("/            搜索最近 15 分钟关键事件", ""),
        ("Tab          切换全部事件/仅异常事件", ""),
        ("c            清除已结束的历史告警标记", ""),
        ("r            立即刷新（只读，不操作 Codex）", ""),
        ("?            显示或关闭帮助", ""),
        ("q            退出", ""),
        ("", ""),
        ("按 ?、Esc 或 q 关闭帮助", "dim"),
        ("─" * width, "dim"),
    ]


def run_interactive(
    args: argparse.Namespace,
    use_color: bool,
    selected_pids: set[int] | None,
    sse_tracker: SseHealthTracker,
) -> tuple[list[ProcessAssessment], SseHealth, dict[str, ConversationActivity]]:
    sampler = LiveSampler(args.idle_threshold, selected_pids, sse_tracker)
    tracker = ActivityTracker(args.event_lookback)
    assessments: list[ProcessAssessment] = []
    sse_health = SseHealth(True, int(args.sse_lookback))
    activities: dict[str, ConversationActivity] = {}
    selected_id = ""
    mode = "main"
    show_help = False
    list_offset = 0
    log_scroll = 0
    follow = True
    query = ""
    alerts_only = False
    cleared_before: dict[str, float] = {}
    next_sample = 0.0

    with RawTerminal() as terminal:
        while True:
            now = time.monotonic()
            if now >= next_sample:
                assessments, sse_health = sampler.sample()
                cli_processes = [
                    item.process
                    for item in visible_cli_assessments(assessments, args.all)
                    if item.process.role == "session"
                ]
                activities = tracker.update(cli_processes)
                ids = [process.session_id for process in cli_processes if process.session_id]
                if selected_id not in ids:
                    selected_id = ids[0] if ids else ""
                next_sample = time.monotonic() + args.interval

            width, height = shutil.get_terminal_size((120, 32))
            width, height = max(48, width), max(12, height)
            items = sorted(
                visible_cli_assessments(assessments, args.all),
                key=lambda item: activity_sort_key(item, activities),
            )
            selected = next(
                (item for item in items if item.process.session_id == selected_id),
                items[0] if items else None,
            )

            if show_help:
                rows = help_rows(width)
                rows.extend([("", "")] * max(0, height - len(rows) - 2))
                rows.extend([("帮助页", "dim"), ("? / Esc 关闭  q 退出", "bold")])
            elif mode == "log" and selected:
                activity = activities.get(
                    selected_id, ConversationActivity(selected_id, selected.process.pid)
                )
                if follow:
                    log_scroll = 0
                rows, log_scroll, _ = event_rows(
                    selected.process,
                    activity,
                    width,
                    height,
                    log_scroll,
                    query,
                    alerts_only,
                    cleared_before.get(selected_id, 0.0),
                )
                rows.extend([("", "")] * max(0, height - len(rows) - 2))
                follow_text = "跟随:开" if follow else "跟随:关"
                rows.extend(
                    [
                        (
                            f"状态 {activity.alert or activity.phase} | {follow_text} | "
                            f"事件 {len(activity.events)}",
                            "red" if activity.alert else "dim",
                        ),
                        ("↑↓/PgUp滚动  f跟随  /搜索  Tab异常  c清除  Esc返回  ?帮助  q退出", "bold"),
                    ]
                )
            else:
                rows, items = main_rows(
                    assessments, activities, selected_id, list_offset, width, height
                )
                rows.extend([("", "")] * max(0, height - len(rows) - 2))
                active = activities.get(selected_id)
                status = active.alert if active and active.alert else (active.phase if active else "等待采样")
                rows.extend(
                    [
                        (f"状态 {status} | 只读监控 | 每 {args.interval:g}s 刷新", "red" if active and active.alert else "dim"),
                        ("↑↓选择  Enter/l日志  c清除  r刷新  ?帮助  q退出", "bold"),
                    ]
                )
            emit_frame(rows, width, height, use_color)

            timeout = max(0.0, next_sample - time.monotonic())
            key = terminal.key(timeout)
            if not key:
                continue
            if show_help:
                if key == "q":
                    break
                if key in {"?", "ESC"}:
                    show_help = False
                continue
            if key == "q":
                break
            if key == "?":
                show_help = True
                continue
            if key == "r":
                next_sample = 0.0
                continue
            if key == "c":
                if selected_id:
                    cleared_before[selected_id] = time.time()
                continue
            if mode == "main":
                if key in {"UP", "k"} and items:
                    index = next(
                        (idx for idx, item in enumerate(items) if item.process.session_id == selected_id),
                        0,
                    )
                    selected_id = items[max(0, index - 1)].process.session_id
                elif key in {"DOWN", "j"} and items:
                    index = next(
                        (idx for idx, item in enumerate(items) if item.process.session_id == selected_id),
                        0,
                    )
                    selected_id = items[min(len(items) - 1, index + 1)].process.session_id
                elif key in {"ENTER", "l"} and selected:
                    mode = "log"
                    log_scroll = 0
                    follow = True
            else:
                if key == "ESC":
                    mode = "main"
                elif key in {"UP", "k"}:
                    follow = False
                    log_scroll += 1
                elif key in {"DOWN", "j"}:
                    log_scroll = max(0, log_scroll - 1)
                    follow = log_scroll == 0
                elif key == "PGUP":
                    follow = False
                    log_scroll += max(5, height - 7)
                elif key == "PGDN":
                    log_scroll = max(0, log_scroll - max(5, height - 7))
                    follow = log_scroll == 0
                elif key in {"g", "HOME"}:
                    follow = False
                    log_scroll = 10**9
                elif key in {"G", "END"}:
                    follow = True
                    log_scroll = 0
                elif key == "f":
                    follow = not follow
                    if follow:
                        log_scroll = 0
                elif key == "TAB":
                    alerts_only = not alerts_only
                    log_scroll = 0
                elif key == "/":
                    result = prompt_search(terminal, width, height, query)
                    if result is not None:
                        query = result
                        log_scroll = 0

    return assessments, sse_health, activities

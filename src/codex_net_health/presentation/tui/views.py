"""Pure row generation for the grouped TUI and session detail view."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from ...config import ANSI, LIFECYCLE_LABELS, NETWORK_LABELS, RECOVERY_LABELS, VERSION
from ...models import InstanceSnapshot, MonitorSnapshot, SessionHealth
from ...utils import format_duration
from .terminal import cell_width, clip_ansi, visible_width


@dataclass(frozen=True)
class RowRef:
    kind: str
    key: str
    instance_id: str
    session_key: str = ""


def style(text: str, name: str, enabled: bool) -> str:
    return f"{ANSI.get(name, '')}{text}{ANSI['reset']}" if enabled and name else text


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if visible_width(text) <= width else clip_ansi(text, width - 1) + "…"


def _health_style(session: SessionHealth) -> str:
    if session.process_exited:
        return "dim"
    if session.current_failure or session.alert_level == "严重":
        return "red"
    if session.recovery.value in {"RECONNECTING", "TRANSPORT_FALLBACK", "SUSPECT"} or session.alert:
        return "yellow"
    if (
        session.lifecycle.value in {"GENERATING", "RUNNING_TOOL"}
        or session.network.state.value == "ACTIVE"
    ):
        return "green"
    return "cyan"


def _session_line(session: SessionHealth, width: int, selected: bool, color: bool) -> str:
    marker = "›" if selected else " "
    status = LIFECYCLE_LABELS[session.lifecycle.value]
    recovery = RECOVERY_LABELS[session.recovery.value]
    if recovery:
        status = recovery
    if session.process_exited:
        status = "进程已退出"
    if session.alert:
        status = "严重停顿" if session.alert_level == "严重" else "等待警告"
    title = session.process.session_title or session.process.current_task or session.session_id[:8]
    now = time.time()
    age = format_duration(max(0, now - (session.phase_since or now)))
    failure = session.current_failure.message if session.current_failure else session.alert_reason
    if width >= 110:
        body = f"{marker} │ {status:<10} {age:>7}  {session.process.model or '-':<13}  {title}"
    elif width >= 76:
        body = f"{marker} │ {status:<10} {age:>7}  {title}"
    else:
        body = f"{marker} │ {status:<10} {title}"
    if failure:
        body = f"{body}  |  {failure}"
    return style(fit(body, width), "bold" if selected else _health_style(session), color)


def _matches(session: SessionHealth, query: str) -> bool:
    if not query:
        return True
    failure_info = session.current_failure or session.latest_failure
    failure = failure_info.message if failure_info else ""
    values = (
        session.session_id,
        session.process.session_title,
        session.process.current_task,
        session.process.model,
        failure,
    )
    return query.casefold() in " ".join(values).casefold()


def main_view(
    snapshot: MonitorSnapshot,
    width: int,
    height: int,
    selected_key: str,
    collapsed: set[str],
    grouped: bool,
    show_auxiliary: bool,
    color: bool,
    query: str = "",
    search_active: bool = False,
    follow: bool = True,
) -> tuple[list[str], list[RowRef]]:
    summary = snapshot.summary()
    title = style(f" CODEX NET HEALTH  v{VERSION} ", "bold", color)
    stats = (
        f"实例 {summary['instances']}  会话 {summary['sessions']}  "
        f"失败 {summary['current_failures']}  告警 {summary['alerts']}  "
        f"阻塞 {summary['network_stalls']}  "
        f"采集 {snapshot.collection_duration_seconds * 1000:.0f}ms"
    )
    if snapshot.process_data_stale_age_seconds is not None:
        stats += f"  进程数据延迟 {snapshot.process_data_stale_age_seconds:.1f}s"
    if snapshot.socket_data_stale_age_seconds is not None:
        stats += f"  TCP 数据延迟 {snapshot.socket_data_stale_age_seconds:.1f}s"
    title_width = visible_width(f" CODEX NET HEALTH  v{VERSION} ")
    lines = [
        title + " " + fit(stats, max(0, width - title_width - 1)),
        "─" * width,
    ]
    refs: list[RowRef] = []
    instances = snapshot.instances
    if not instances:
        lines.extend(["", style("  当前没有运行中的 Codex 会话", "dim", color)])
    for instance in instances:
        matching_sessions = [
            session for session in instance.sessions if _matches(session, query)
        ]
        if query and not matching_sessions:
            continue
        if grouped:
            open_group = instance.instance_id not in collapsed
            marker = "▼" if open_group else "▶"
            failures = sum(bool(session.current_failure) for session in matching_sessions)
            group_key = f"group:{instance.instance_id}"
            selected = selected_key == group_key
            db = (
                ""
                if instance.display_sqlite_home == instance.display_codex_home
                else f"  DB {instance.display_sqlite_home}"
            )
            label = (
                f"{marker} {instance.display_codex_home}  "
                f"{len(matching_sessions)} 会话{db}"
            )
            if failures:
                label += f"  {failures} 失败"
            if instance.diagnostics:
                label += "  数据不完整"
            if instance.unknown_event_types:
                label += f"  未映射事件 {sum(instance.unknown_event_types.values())}"
            if instance.rollout_context_truncated:
                label += "  时间线截断"
            lines.append(
                style(
                    fit(("› " if selected else "  ") + label, width),
                    "inverse" if selected else "bold",
                    color,
                )
            )
            refs.append(RowRef("group", group_key, instance.instance_id))
            if not open_group:
                continue
        sessions = sorted(
            matching_sessions,
            key=lambda item: (
                not bool(item.current_failure),
                item.alert_level != "严重",
                item.process.identity.start_time,
                item.process.pid,
            ),
        )
        for session in sessions:
            session_key = f"session:{session.key}"
            lines.append(_session_line(session, width, selected_key == session_key, color))
            refs.append(RowRef("session", session_key, instance.instance_id, session.key))
        if show_auxiliary:
            for process in (item for item in instance.processes if item.role != "session"):
                description = (
                    f"    · PID {process.pid}  {process.role}  {process.command}"
                )
                lines.append(style(fit(description, width), "dim", color))
    if search_active:
        footer = f"/ 搜索: {query}█   Enter 确认   Esc 清除"
    else:
        mode = "跟随" if follow else "暂停"
        footer = (
            f"↑↓/jk 选择  Enter 详情  / 搜索  Tab 异常  f {mode}  "
            "g 分组  a 进程  ? 帮助  q 退出"
        )
    available = max(0, height - 2)
    lines = lines[:available]
    lines.extend([""] * max(0, available - len(lines)))
    lines.append("─" * width)
    lines.append(style(fit(footer, width), "dim", color))
    return lines, refs


def _wrap_field(label: str, value: str, width: int) -> list[str]:
    prefix = f"{label}  "
    indent = " " * visible_width(prefix)
    remaining = value
    lines: list[str] = []
    current_prefix = prefix
    while remaining:
        available = max(1, width - visible_width(current_prefix))
        used = 0
        count = 0
        for character in remaining:
            size = cell_width(character)
            if used + size > available:
                break
            used += size
            count += 1
        count = max(1, count)
        lines.append(current_prefix + remaining[:count])
        remaining = remaining[count:]
        current_prefix = indent
    return lines or [prefix]


def detail_view(
    session: SessionHealth,
    width: int,
    height: int,
    color: bool,
    follow: bool = True,
    event_scroll: int = 0,
    instance: InstanceSnapshot | None = None,
    lookback_seconds: int = 0,
) -> list[str]:
    title = session.process.session_title or session.process.current_task or session.session_id
    lines = [
        style(f" {fit(title, max(1, width - 2))} ", "bold", color),
        "─" * width,
        f"状态  {LIFECYCLE_LABELS[session.lifecycle.value]}",
        f"恢复  {RECOVERY_LABELS[session.recovery.value] or '-'}",
        f"网络  {NETWORK_LABELS[session.network.state.value]}  {session.network.reason}",
        f"实例  {session.instance_id}   PID {session.process.pid}",
    ]
    if instance:
        lines.extend(
            [
                f"CODEX_HOME   {instance.paths.codex_home}",
                f"SQLITE_HOME  {instance.paths.sqlite_home}",
            ]
        )
        lines.extend(f"数据  {message}" for message in instance.diagnostics)
        if instance.rollout_context_truncated:
            lines.append("时间线  较早上下文未加载（启动读取采用有界尾部）")
        if instance.unknown_event_types:
            unknown = ", ".join(
                f"{name}={count}"
                for name, count in instance.unknown_event_types.items()
            )
            lines.extend(_wrap_field("未映射事件", unknown, width))
    if session.process_exited:
        lines.append(style("进程  已退出（保留最近时间线）", "dim", color))
    failure = session.current_failure or session.latest_failure
    if failure:
        failure_heading = "当前失败" if session.current_failure else "最近失败（已非当前）"
        lines.extend(
            [
                "",
                style(failure_heading, "red", color),
                style(f"失败类型  {failure.category}", "red", color),
            ]
        )
        lines.extend(
            style(line, "red", color)
            for line in _wrap_field("错误消息", failure.message, width)
        )
        if failure.additional_details:
            lines.extend(_wrap_field("附加详情", failure.additional_details, width))
        if failure.turn_id:
            lines.append(f"Turn ID   {failure.turn_id}")
        stamp = (
            datetime.fromtimestamp(failure.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            if failure.timestamp
            else "-"
        )
        lines.append(f"来源  {failure.source}   时间 {stamp}")
    if session.network.connections:
        lines.extend(["", style("TCP 证据", "bold", color)])
        for connection in session.network.connections:
            lines.append(
                f"{connection.route} {connection.peer}  {connection.health.value}  "
                f"收 {connection.received_delta} 发 {connection.sent_delta} "
                f"重传 {connection.retrans_delta}"
            )
    timeline = "事件时间线"
    if lookback_seconds:
        timeline += f"（最近 {format_duration(lookback_seconds)}）"
    header_lines = lines + ["", style(timeline, "bold", color)]
    event_lines: list[str] = []
    for event in session.events:
        stamp = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
        detail = f"  {event.detail}" if event.detail else ""
        event_lines.append(f"{stamp}  {event.summary}{detail}")
    available = max(0, height - 2)
    if follow:
        event_capacity = max(0, available - len(header_lines))
        body = header_lines + event_lines[-event_capacity:] if event_capacity else header_lines
        body = body[:available]
    else:
        all_lines = header_lines + event_lines
        maximum_scroll = max(0, len(all_lines) - available)
        event_scroll = min(event_scroll, maximum_scroll)
        body = all_lines[event_scroll : event_scroll + available]
    lines = [fit(line, width) for line in body]
    lines.extend([""] * max(0, available - len(lines)))
    follow_label = "自动跟随" if follow else f"已暂停 · 偏移 {event_scroll}"
    lines.extend(
        [
            "─" * width,
            style(
                fit(
                    f"↑↓/jk 浏览   f {follow_label}   Esc/Enter 返回   q 退出",
                    width,
                ),
                "dim",
                color,
            ),
        ]
    )
    return lines


def help_view(width: int, height: int, color: bool) -> list[str]:
    lines = [
        style(" CODEX NET HEALTH · 快捷键 ", "bold", color),
        "─" * width,
        "↑ / ↓ 或 j / k   移动选择；详情中浏览较早事件",
        "Enter             折叠实例或打开会话详情",
        "/                 按标题、任务、模型、会话 ID 或错误搜索",
        "Tab               跳到下一个异常会话",
        "f                 切换事件自动跟随",
        "g                 切换 CODEX_HOME 分组和扁平视图",
        "a                 显示或隐藏启动器与辅助进程",
        "?                 打开或关闭本帮助",
        "q                 退出监视器",
    ]
    available = max(0, height - 2)
    lines = [fit(line, width) for line in lines[:available]]
    lines.extend([""] * max(0, available - len(lines)))
    lines.extend(["─" * width, style("? / Esc / Enter 返回", "dim", color)])
    return lines


def find_session(snapshot: MonitorSnapshot, key: str) -> SessionHealth | None:
    return next((session for session in snapshot.sessions if session.key == key), None)

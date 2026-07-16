"""Pure row generation for the grouped TUI and session detail view."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from config import ANSI, LIFECYCLE_LABELS, NETWORK_LABELS, RECOVERY_LABELS, VERSION
from models import AlertStatus, InstanceSnapshot, MonitorSnapshot, SessionHealth
from utils import format_duration
from .terminal import cell_width, clip_ansi, visible_width


@dataclass(frozen=True)
class RowRef:
    kind: str
    key: str
    instance_id: str
    session_key: str = ""


@dataclass(frozen=True)
class ViewLayout:
    lines: list[str]
    refs: list[RowRef]
    all_refs: list[RowRef]
    top: int
    max_top: int
    body_height: int
    position: str = ""


def style(text: str, name: str, enabled: bool) -> str:
    return f"{ANSI.get(name, '')}{text}{ANSI['reset']}" if enabled and name else text


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if visible_width(text) <= width else clip_ansi(text, width - 1) + "…"


def pad(text: str, width: int) -> str:
    clipped = fit(text, width)
    return clipped + " " * max(0, width - visible_width(clipped))


def _left_right(left: str, right: str, width: int) -> str:
    if visible_width(right) >= width:
        return fit(right, width)
    left_width = max(0, width - visible_width(right) - 1)
    return pad(fit(left, left_width), left_width) + " " + right


def _session_line(session: SessionHealth, width: int, selected: bool, color: bool) -> str:
    marker = "▶" if selected else " "
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
    severity = "! " if session.current_failure or session.alert_level == "严重" else ""
    if width >= 96:
        left = (
            f"{marker} │ {severity}{title}  ·  {session.process.model or '-'}  ·  {age}"
        )
    elif width >= 64:
        left = f"{marker} │ {severity}{title}  ·  {age}"
    else:
        left = f"{marker} │ {severity}{title}"
    if failure:
        left = f"{left}  ·  {failure}"
    state_label = f"已选中 · {status}" if selected else status
    body = _left_right(left, f"[{state_label}]", width)
    if selected:
        return style(pad(body, width), "inverse", color)
    if session.process_exited:
        return style(body, "dim", color)
    return body


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


def main_layout(
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
    list_top: int = 0,
) -> ViewLayout:
    summary = snapshot.summary()
    clock = snapshot.generated_at.split("T", 1)[-1][:8]
    header_left = f" CODEX NET HEALTH  v{VERSION}"
    header_right = f"LIVE  {clock} "
    header = _left_right(header_left, header_right, width)
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
    header_lines = [style(pad(header, width), "inverse", color)]
    if height >= 4:
        header_lines.append(fit("  " + stats, width))
    if height >= 6:
        header_lines.append("─" * width)
    body_rows: list[tuple[str, RowRef | None]] = []
    instances = snapshot.instances
    if not instances:
        body_rows.append(("  当前没有运行中的 Codex 会话", None))
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
            body_rows.append(
                (
                    style(
                        fit(("› " if selected else "  ") + label, width),
                        "inverse" if selected else "bold",
                        color,
                    ),
                    RowRef("group", group_key, instance.instance_id),
                )
            )
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
            body_rows.append(
                (
                    _session_line(session, width, selected_key == session_key, color),
                    RowRef("session", session_key, instance.instance_id, session.key),
                )
            )
        if show_auxiliary:
            for process in (item for item in instance.processes if item.role != "session"):
                description = (
                    f"    · PID {process.pid}  {process.role}  {process.command}"
                )
                body_rows.append((style(fit(description, width), "dim", color), None))
    if search_active:
        footer = f"/ 搜索: {query}█   Enter 确认   Esc 清除"
    else:
        mode = "跟随" if follow else "暂停"
        footer = (
            f"↑↓/jk 选择  Enter 详情  / 搜索  Tab 异常  f {mode}  "
            "g 分组  a 进程  ? 帮助  q 退出"
        )
    footer_lines = ["─" * width, fit(footer, width)] if height >= 3 else []
    body_height = max(0, height - len(header_lines) - len(footer_lines))
    all_refs = [ref for _, ref in body_rows if ref is not None]
    selected_row = next(
        (index for index, (_, ref) in enumerate(body_rows) if ref and ref.key == selected_key),
        None,
    )
    max_top = max(0, len(body_rows) - body_height)
    top = min(max(0, list_top), max_top)
    if selected_row is not None and body_height:
        if selected_row < top:
            top = selected_row
        elif selected_row >= top + body_height:
            top = selected_row - body_height + 1
    visible_rows = body_rows[top : top + body_height]
    lines = header_lines + [line for line, _ in visible_rows]
    lines.extend([""] * max(0, height - len(footer_lines) - len(lines)))
    lines.extend(footer_lines)
    visible_refs = [ref for _, ref in visible_rows if ref is not None]
    end = min(len(body_rows), top + body_height)
    position = f"{top + 1 if body_rows else 0}-{end}/{len(body_rows)}"
    return ViewLayout(lines, visible_refs, all_refs, top, max_top, body_height, position)


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
    list_top: int = 0,
) -> tuple[list[str], list[RowRef]]:
    layout = main_layout(
        snapshot, width, height, selected_key, collapsed, grouped,
        show_auxiliary, color, query, search_active, follow, list_top,
    )
    return layout.lines, layout.refs


def home_layout(
    instance: InstanceSnapshot,
    width: int,
    height: int,
    selected_key: str,
    color: bool,
    list_top: int = 0,
    query: str = "",
) -> ViewLayout:
    sessions = [
        session for session in instance.sessions if _matches(session, query)
    ]
    failures = sum(bool(session.current_failure) for session in sessions)
    alerts = sum(bool(session.alert) for session in sessions)
    token_used = sum((getattr(session, "token_used", None) or 0) for session in sessions)
    token_known = any(getattr(session, "token_used", None) is not None for session in sessions)
    title = _left_right(
        f" HOME  {instance.display_codex_home}",
        f"{len(sessions)} sessions ",
        width,
    )
    summary = f" 失败 {failures}  告警 {alerts}"
    if token_known:
        summary += f"  token {token_used}"
    if instance.diagnostics:
        summary += f"  数据不完整 {len(instance.diagnostics)}"
    header = [style(pad(title, width), "inverse", color)]
    if height >= 4:
        header.append(fit(" " + summary, width))
    if height >= 6:
        header.append("─" * width)
    footer = [
        "─" * width,
        fit("↑↓ 选择  Enter 会话  c 对比 Home  / 搜索  Esc 返回", width),
    ] if height >= 3 else []
    body_height = max(0, height - len(header) - len(footer))
    rows: list[tuple[str, RowRef]] = []
    for session in sessions:
        key = f"session:{session.key}"
        rows.append(
            (
                _session_line(session, width, selected_key == key, color),
                RowRef("session", key, instance.instance_id, session.key),
            )
        )
    all_refs = [ref for _, ref in rows]
    selected_row = next(
        (i for i, (_, ref) in enumerate(rows) if ref.key == selected_key),
        None,
    )
    max_top = max(0, len(rows) - body_height)
    top = min(max(0, list_top), max_top)
    if selected_row is not None and body_height:
        top = min(top, selected_row)
        if selected_row >= top + body_height:
            top = selected_row - body_height + 1
    visible = rows[top : top + body_height]
    lines = header + [line for line, _ in visible]
    lines.extend([""] * max(0, height - len(footer) - len(lines)))
    lines.extend(footer)
    end = min(len(rows), top + body_height)
    return ViewLayout(
        lines,
        [ref for _, ref in visible],
        all_refs,
        top,
        max_top,
        body_height,
        f"{top + 1 if rows else 0}-{end}/{len(rows)}",
    )


def compare_layout(
    snapshot: MonitorSnapshot,
    width: int,
    height: int,
    color: bool,
    top: int = 0,
) -> ViewLayout:
    header = [style(pad(" HOME COMPARE", width), "inverse", color)]
    if height >= 5:
        header.append(
            fit(
                " HOME                         活跃 失败 重连 阻塞 数据     TTFT   LIMIT"
                if width >= 80
                else " HOME                         状态",
                width,
            )
        )
        header.append("─" * width)
    rows: list[str] = []
    for instance in snapshot.instances:
        sessions = instance.sessions
        active = sum(not item.process_exited for item in sessions)
        failed = sum(bool(item.current_failure) for item in sessions)
        reconnecting = sum(
            item.recovery.value in {"SUSPECT", "RECONNECTING", "TRANSPORT_FALLBACK"}
            for item in sessions
        )
        stalled = sum(item.network.state.value == "STALLED" for item in sessions)
        ttfts = [
            turn.time_to_first_token_seconds
            for session in sessions
            for turn in getattr(session, "turns", [])
            if turn.time_to_first_token_seconds is not None
        ]
        ttft = f"{sum(ttfts) / len(ttfts):.1f}s" if ttfts else "-"
        limit_values = [
            window.used_percent
            for session in sessions
            for limits in [getattr(session, "rate_limits", None)]
            for window in [getattr(limits, "primary", None)]
            if window is not None and window.used_percent is not None
        ]
        limit = f"{max(limit_values):.0f}%" if limit_values else "-"
        data = "DEGRADED" if instance.diagnostics else "OK"
        if width >= 80:
            line = (
                f" {instance.display_codex_home:<28} {active:>4} {failed:>4} "
                f"{reconnecting:>4} {stalled:>4} {data:<8} {ttft:>6} {limit:>7}"
            )
        else:
            status = f"[A{active} E{failed} R{reconnecting} S{stalled} {data}]"
            line = _left_right(f" {instance.display_codex_home}", status, width)
        rows.append(fit(line, width))
    if not rows:
        rows.append(" 当前没有可比较的 Codex Home")
    footer = [
        "─" * width,
        fit("↑↓/PgUp/PgDn 浏览  c/Esc 返回", width),
    ] if height >= 3 else []
    body_height = max(0, height - len(header) - len(footer))
    max_top = max(0, len(rows) - body_height)
    top = min(max(0, top), max_top)
    visible = rows[top : top + body_height]
    lines = header + visible
    lines.extend([""] * max(0, height - len(footer) - len(lines)))
    lines.extend(footer)
    end = min(len(rows), top + body_height)
    return ViewLayout(
        lines, [], [], top, max_top, body_height,
        f"{top + 1 if rows else 0}-{end}/{len(rows)}",
    )


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


def _event_style(kind: str) -> str:
    if kind in {"TURN_FAILED", "OPERATION_ERROR", "ALERT_ESCALATED"}:
        return "red"
    if kind in {
        "WARNING", "RECONNECTING", "TRANSPORT_FALLBACK", "TURN_ABORTED",
        "ALERT_OPENED", "ALERT_ACKNOWLEDGED",
    }:
        return "yellow"
    if kind in {
        "RECOVERED", "TURN_COMPLETED", "TOOL_COMPLETED", "COMPACT_COMPLETED",
        "ALERT_RESOLVED",
    }:
        return "green"
    if kind in {"TOOL_RUNNING", "COMPACTING"}:
        return "magenta"
    if kind in {"TURN_STARTED", "REQUEST_SENT", "RESPONSE_STARTED", "MODEL_PROGRESS"}:
        return "cyan"
    return "dim"


def _network_style(session: SessionHealth) -> str:
    if session.network.state.value in {"STALLED", "CLOSED"}:
        return "red"
    if session.network.state.value in {"SUSPECT", "UNKNOWN"}:
        return "yellow"
    if session.network.state.value == "ACTIVE":
        return "green"
    return "cyan"


def _detail_fixed(
    session: SessionHealth,
    width: int,
    color: bool,
) -> list[str]:
    title = session.process.session_title or session.process.current_task or session.session_id
    lifecycle = LIFECYCLE_LABELS[session.lifecycle.value]
    recovery = RECOVERY_LABELS[session.recovery.value]
    if recovery:
        lifecycle = f"{lifecycle} · {recovery}"
    network_label = NETWORK_LABELS[session.network.state.value]
    connection_count = len(session.network.connections)
    network_text = f" 网络  {network_label}"
    if session.network.reason:
        network_text += f" · {session.network.reason}"
    if connection_count:
        network_text += f" · {connection_count} 条连接"
    return [
        style(pad(f" {title}", width), "inverse", color),
        style(fit(network_text, width), _network_style(session), color),
        fit(
            f" 状态  {lifecycle}   PID {session.process.pid}   "
            f"模型 {session.process.model or '-'}",
            width,
        ),
        "─" * width,
    ]


def _value(item: object, *names: str, default: object = "-") -> object:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return default


def _severity(kind: str) -> tuple[str, str]:
    if kind in {"TURN_FAILED", "OPERATION_ERROR", "ALERT_ESCALATED"}:
        return "ERR", "red"
    if kind in {
        "WARNING", "RECONNECTING", "TRANSPORT_FALLBACK", "TURN_ABORTED",
        "ALERT_OPENED", "ALERT_ACKNOWLEDGED",
    }:
        return "WARN", "yellow"
    if kind in {
        "RECOVERED", "TURN_COMPLETED", "TOOL_COMPLETED", "COMPACT_COMPLETED",
        "ALERT_RESOLVED",
    }:
        return "OK", "green"
    return "INFO", _event_style(kind)


def _event_lines(event: object, width: int, color: bool) -> list[str]:
    timestamp = float(_value(event, "timestamp", default=0.0) or 0.0)
    stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    kind = str(_value(event, "kind", default=""))
    label, color_name = _severity(kind)
    summary = str(_value(event, "summary", default=kind or "事件"))
    detail = str(_value(event, "detail", default=""))
    text = summary + (f" · {detail}" if detail else "")
    prefix_plain = f"{stamp}  {label:<5} "
    available = max(1, width - visible_width(prefix_plain))
    chunks: list[str] = []
    remaining = text
    while remaining:
        used = 0
        count = 0
        for character in remaining:
            size = cell_width(character)
            if used + size > available:
                break
            used += size
            count += 1
        count = max(1, count)
        chunks.append(remaining[:count])
        remaining = remaining[count:]
    chunks = chunks or [""]
    first_prefix = (
        style(stamp, "dim", color)
        + "  "
        + style(f"{label:<5}", color_name, color)
        + " "
    )
    lines = [first_prefix + style(chunks[0], color_name, color)]
    indent = " " * visible_width(prefix_plain)
    lines.extend(indent + chunk for chunk in chunks[1:])
    return lines


def _timeline_content(
    session: SessionHealth, width: int, color: bool, lookback_seconds: int
) -> list[str]:
    heading = "Timeline"
    if lookback_seconds:
        heading += f" · 最近 {format_duration(lookback_seconds)}"
    lines = [style(heading, "bold", color)]
    entries: list[object] = list(session.events)
    status_labels = {
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
                    "summary": status_labels[transition.status],
                    "detail": transition.reason or alert.reason,
                }
            )
    for event in sorted(entries, key=lambda item: float(_value(item, "timestamp", default=0))):
        lines.extend(_event_lines(event, width, color))
    failure = session.current_failure or session.latest_failure
    if failure:
        stamp = failure.timestamp or time.time()
        synthetic = {
            "timestamp": stamp,
            "kind": "TURN_FAILED",
            "summary": "模型调用失败",
            "detail": failure.message,
        }
        lines.extend(_event_lines(synthetic, width, color))
    if not entries and not failure:
        lines.append("暂无事件")
    return lines


def _turn_content(session: SessionHealth, width: int, color: bool) -> list[str]:
    turns = (
        getattr(session, "turns", None)
        or getattr(session, "turn_summaries", None)
        or []
    )
    lines = [style("Turns", "bold", color)]
    for index, turn in enumerate(turns, start=1):
        turn_id = str(_value(turn, "turn_id", "id", default=f"#{index}"))
        status = str(_value(turn, "status", "result", default="unknown"))
        duration = _value(turn, "duration_seconds", "duration", default=None)
        ttft = _value(
            turn,
            "time_to_first_token_seconds",
            "ttft_seconds",
            "time_to_first_token",
            default=None,
        )
        usage = _value(turn, "token_usage", default=None)
        tokens = (
            _value(usage, "total_tokens", default=None)
            if usage is not None
            else _value(turn, "total_tokens", "token_total", default=None)
        )
        tools = _value(turn, "tool_count", default=None)
        pieces = [turn_id[:16], status]
        if duration is not None:
            pieces.append(f"耗时 {float(duration):.1f}s")
        if ttft is not None:
            pieces.append(f"TTFT {float(ttft):.1f}s")
        if tokens is not None:
            pieces.append(f"token {tokens}")
        if tools is not None:
            pieces.append(f"工具 {tools}")
        lines.append(fit("  ".join(pieces), width))
    if not turns:
        lines.append("暂无 Turn 摘要；旧数据源可能未提供 turn 边界")
    return lines


def _evidence_content(
    session: SessionHealth,
    width: int,
    color: bool,
    instance: InstanceSnapshot | None,
) -> list[str]:
    content_lines = [style("Evidence", "bold", color), f"实例  {session.instance_id}"]
    if instance:
        content_lines.extend(
            [
                f"CODEX_HOME   {instance.paths.codex_home}",
                f"SQLITE_HOME  {instance.paths.sqlite_home}",
            ]
        )
        content_lines.extend(f"数据  {message}" for message in instance.diagnostics)
        if instance.rollout_context_truncated:
            content_lines.append("时间线  较早上下文未加载（启动读取采用有界尾部）")
        if instance.unknown_event_types:
            unknown = ", ".join(
                f"{name}={count}"
                for name, count in instance.unknown_event_types.items()
            )
            content_lines.extend(_wrap_field("未映射事件", unknown, width))
    if session.process_exited:
        content_lines.append(style("进程  已退出（保留最近时间线）", "dim", color))
    if session.alerts:
        content_lines.extend(["", style("Alerts", "bold", color)])
        for alert in session.alerts:
            status = alert.status.value
            marker = "ACTIVE" if alert.active else "RESOLVED"
            content_lines.append(
                fit(f"{marker}  {alert.kind}  [{status}]  {alert.reason}", width)
            )
    failure = session.current_failure or session.latest_failure
    if failure:
        failure_heading = "当前失败" if session.current_failure else "最近失败（已非当前）"
        content_lines.extend(
            [
                "",
                style(failure_heading, "red", color),
                style(f"失败类型  {failure.category}", "red", color),
            ]
        )
        content_lines.extend(
            style(line, "red", color)
            for line in _wrap_field("错误消息", failure.message, width)
        )
        if failure.additional_details:
            content_lines.extend(_wrap_field("附加详情", failure.additional_details, width))
        if failure.turn_id:
            content_lines.append(f"Turn ID   {failure.turn_id}")
        stamp = (
            datetime.fromtimestamp(failure.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            if failure.timestamp
            else "-"
        )
        content_lines.append(f"来源  {failure.source}   时间 {stamp}")
    if session.network.connections:
        content_lines.extend(["", style("TCP 证据", "bold", color)])
        for connection in session.network.connections:
            content_lines.append(
                f"{connection.route} {connection.peer}  {connection.health.value}  "
                f"收 {connection.received_delta} 发 {connection.sent_delta} "
                f"重传 {connection.retrans_delta}"
            )
    usage = getattr(session, "token_usage", None)
    if usage:
        content_lines.extend(["", style("Capacity", "bold", color)])
        context = _value(usage, "context_tokens", default=None)
        window = _value(usage, "context_window", default=None)
        percent = getattr(usage, "context_percent", None)
        if context is not None or window is not None:
            suffix = f" ({percent:.1f}%)" if percent is not None else ""
            content_lines.append(f"context  {context or '-'} / {window or '-'}{suffix}")
        total = _value(usage, "total_tokens", default=None)
        if total is not None:
            content_lines.append(f"turn token  {total}")
    limits = getattr(session, "rate_limits", None)
    if limits:
        reached = _value(limits, "reached", default=None)
        content_lines.append(f"rate limit  {'REACHED' if reached else 'available'}")
        for name in ("primary", "secondary"):
            window = _value(limits, name, default=None)
            used = _value(window, "used_percent", default=None) if window else None
            if used is not None:
                content_lines.append(f"{name}  {float(used):.1f}%")
    agent_tree = (
        getattr(session, "agent_tree", None)
        or getattr(session, "subagents", None)
        or getattr(session, "agents", None)
    )
    if agent_tree:
        content_lines.extend(["", style("Subagents", "bold", color)])

        def append_agent(node: object, depth: int = 0) -> None:
            path = str(_value(node, "path", "agent_path", "nickname", default="agent"))
            status = str(_value(node, "status", default="unknown"))
            content_lines.append(f"{'  ' * depth}+- {path} [{status}]")
            for child in _value(node, "children", default=[]) or []:
                append_agent(child, depth + 1)

        for node in agent_tree:
            append_agent(node, int(_value(node, "depth", default=0) or 0))
    collector = getattr(instance, "collector_health", None) if instance else None
    if collector:
        content_lines.extend(["", style("Collector", "bold", color)])
        values = collector.values() if isinstance(collector, dict) else collector
        for item in values:
            name = str(_value(item, "name", "source", default="collector"))
            error = str(_value(item, "error", default=""))
            failures = int(_value(item, "consecutive_failures", default=0) or 0)
            exceeded = bool(_value(item, "budget_exceeded", default=False))
            status = "ERR" if error or failures else "SLOW" if exceeded else "OK"
            duration = _value(item, "duration_seconds", default=None)
            suffix = f" {float(duration) * 1000:.0f}ms" if duration is not None else ""
            content_lines.append(f"{name}  {status}{suffix}")
    return content_lines


def detail_layout(
    session: SessionHealth,
    width: int,
    height: int,
    color: bool,
    mode: str = "timeline",
    follow: bool = True,
    scroll: int = 0,
    instance: InstanceSnapshot | None = None,
    lookback_seconds: int = 0,
) -> ViewLayout:
    fixed = _detail_fixed(session, width, color)
    if height <= 3:
        fixed = fixed[:2]
        footer_height = 1
    elif height <= 5:
        fixed = fixed[:3]
        footer_height = 2
    else:
        footer_height = 2
    body_height = max(0, height - len(fixed) - footer_height)
    if mode == "turns":
        content = _turn_content(session, width, color)
    elif mode == "evidence":
        content = _evidence_content(session, width, color, instance)
    else:
        mode = "timeline"
        content = _timeline_content(session, width, color, lookback_seconds)
    max_top = max(0, len(content) - body_height)
    top = max_top if follow and mode == "timeline" else min(max(0, scroll), max_top)
    body = content[top : top + body_height]
    lines = [fit(line, width) for line in fixed + body]
    if footer_height == 2:
        lines.extend([""] * max(0, height - 2 - len(lines)))
        end = min(len(content), top + body_height)
        position = f"{top + 1 if content else 0}-{end}/{len(content)}"
        state = "FOLLOW" if follow and mode == "timeline" else f"PAUSED · 偏移 {top}"
        lines.extend([
            "─" * width,
            fit(f"[{mode.title()} · {state} · {position}]  1/2/3 切换  x 确认告警", width),
        ])
    else:
        end = min(len(content), top + body_height)
        position = f"{top + 1 if content else 0}-{end}/{len(content)}"
        lines.extend([""] * max(0, height - len(lines)))
    return ViewLayout(lines, [], [], top, max_top, body_height, position)


def detail_scroll_limit(
    session: SessionHealth,
    width: int,
    height: int,
    instance: InstanceSnapshot | None = None,
    lookback_seconds: int = 0,
    mode: str = "timeline",
) -> int:
    return detail_layout(
        session, width, height, False, mode, False, 0, instance, lookback_seconds
    ).max_top


def detail_view(
    session: SessionHealth,
    width: int,
    height: int,
    color: bool,
    follow: bool = True,
    event_scroll: int = 0,
    instance: InstanceSnapshot | None = None,
    lookback_seconds: int = 0,
    mode: str = "timeline",
) -> list[str]:
    return detail_layout(
        session, width, height, color, mode, follow, event_scroll,
        instance, lookback_seconds,
    ).lines


def help_view(width: int, height: int, color: bool) -> list[str]:
    lines = [
        style(" CODEX NET HEALTH · 快捷键 ", "bold", color),
        "─" * width,
        "↑ / ↓ 或 j / k   移动一行",
        "PgUp / PgDn       移动一页；Home / End 到首尾",
        "Enter             进入 Home 或会话详情",
        "Space             折叠或展开 Overview 实例",
        "1 / 2 / 3         Timeline / Turns / Evidence",
        "/                 按标题、任务、模型、会话 ID 或错误搜索",
        "Tab               跳到下一个异常会话",
        "f                 Timeline 切换自动跟随",
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

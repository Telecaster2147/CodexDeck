"""Readable non-interactive snapshot output."""

from __future__ import annotations

import time

from config import LIFECYCLE_LABELS, NETWORK_LABELS, RECOVERY_LABELS
from models import AgentNode, MonitorSnapshot, SessionHealth, TokenUsageSummary
from utils import format_duration


def _session_status(session: SessionHealth) -> str:
    if session.process_exited:
        return "进程已退出"
    if session.attention_request:
        return f"等待用户操作 / {session.attention.value}"
    lifecycle = LIFECYCLE_LABELS[session.lifecycle.value]
    recovery = RECOVERY_LABELS[session.recovery.value]
    return f"{lifecycle} / {recovery}" if recovery else lifecycle


def _number(value: int | float | None) -> str:
    if value is None:
        return "?"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return str(int(value))


def _token_parts(label: str, usage: TokenUsageSummary | None) -> list[str]:
    if usage is None:
        return []
    values = (
        ("in", usage.input_tokens),
        ("cached", usage.cached_input_tokens),
        ("out", usage.output_tokens),
        ("reasoning", usage.reasoning_output_tokens),
        ("total", usage.total_tokens),
    )
    known = [f"{name} {_number(value)}" for name, value in values if value is not None]
    return [f"{label} " + " / ".join(known)] if known else []


def _agent_counts(nodes: list[AgentNode]) -> tuple[int, dict[str, int]]:
    counts: dict[str, int] = {}
    total = 0
    pending = list(nodes)
    while pending:
        node = pending.pop()
        total += 1
        counts[node.status] = counts.get(node.status, 0) + 1
        pending.extend(node.children)
    return total, counts


def _session_metrics(session: SessionHealth) -> list[str]:
    lines: list[str] = []
    if session.turns:
        turn = session.turns[-1]
        parts = [f"Turn {turn.turn_id[:12]}", turn.status]
        if turn.duration_seconds is not None:
            parts.append(f"耗时 {format_duration(turn.duration_seconds)}")
        if turn.time_to_first_token_seconds is not None:
            parts.append(f"TTFT {turn.time_to_first_token_seconds:.2f}s")
        if turn.tool_count:
            tool = f"工具 {turn.tool_count}"
            if turn.tool_duration_seconds is not None:
                tool += f" / {format_duration(turn.tool_duration_seconds)}"
            parts.append(tool)
        if turn.reconnect_count or turn.fallback_count:
            parts.append(f"重连 {turn.reconnect_count} / fallback {turn.fallback_count}")
        lines.append("    " + " | ".join(parts))
    elif session.tool_executions:
        running = sum(tool.status == "running" for tool in session.tool_executions)
        lines.append(f"    工具：{len(session.tool_executions)} 个，运行中 {running} 个")

    if session.terminal_sessions:
        running_terminals = sum(
            terminal.status == "running" for terminal in session.terminal_sessions
        )
        capabilities = ", ".join(
            sorted({terminal.capability.value for terminal in session.terminal_sessions})
        )
        dropped = sum(terminal.dropped_bytes for terminal in session.terminal_sessions)
        detail = (
            f"    Terminal：{len(session.terminal_sessions)} 个，运行中 {running_terminals} 个"
            f" | {capabilities}"
        )
        if dropped:
            detail += f" | dropped {dropped} B"
        lines.append(detail)

    token_parts = _token_parts("本 Turn", session.token_usage)
    token_parts.extend(_token_parts("累计", session.cumulative_token_usage))
    if token_parts:
        context = session.token_usage or session.cumulative_token_usage
        context_part = ""
        if context and context.context_tokens is not None and context.context_window:
            context_part = (
                f"；上下文 {_number(context.context_tokens)}/{_number(context.context_window)}"
                f" ({context.context_percent:.1f}%)"
            )
        lines.append("    Token：" + "；".join(token_parts) + context_part)

    limits = session.rate_limits
    if limits is not None:
        rate_parts = []
        if limits.primary and limits.primary.used_percent is not None:
            rate_parts.append(f"primary {limits.primary.used_percent:.1f}%")
        if limits.secondary and limits.secondary.used_percent is not None:
            rate_parts.append(f"secondary {limits.secondary.used_percent:.1f}%")
        if limits.credits is not None:
            rate_parts.append(f"credits {_number(limits.credits)}")
        if limits.reached is not None:
            rate_parts.append("已触限" if limits.reached else "未触限")
        if limits.reason:
            rate_parts.append(limits.reason)
        if rate_parts:
            lines.append("    Rate limit：" + " | ".join(rate_parts))

    agent_total, agent_statuses = _agent_counts(session.agents)
    if agent_total:
        statuses = " / ".join(f"{name} {count}" for name, count in sorted(agent_statuses.items()))
        lines.append(f"    Subagent：{agent_total} 个 | {statuses}")
    return lines


def render_text(snapshot: MonitorSnapshot, show_auxiliary: bool = False) -> str:
    summary = snapshot.summary()
    lines = [
        f"Codex Net Health  {snapshot.generated_at}  刷新 {snapshot.interval_seconds:g}s",
        (
            f"实例 {summary['instances']}  会话 {summary['sessions']}  "
            f"待操作 {summary['action_required']}  失败 {summary['current_failures']}  "
            f"阻塞 {summary['network_stalls']}"
        ),
    ]
    if snapshot.diagnostics:
        lines.extend(f"[采集] {message}" for message in snapshot.diagnostics)
    if not snapshot.instances:
        lines.append("当前没有运行中的 Codex 会话。")
        return "\n".join(lines)
    for instance in snapshot.instances:
        suffix = (
            ""
            if instance.display_codex_home == instance.display_sqlite_home
            else f"  DB {instance.display_sqlite_home}"
        )
        lines.append("")
        lines.append(f"[{instance.display_codex_home}]{suffix}")
        lines.extend(f"  数据不完整：{message}" for message in instance.diagnostics)
        if instance.unknown_event_types:
            total = sum(instance.unknown_event_types.values())
            lines.append(f"  未映射协议事件：{total} 条")
        if instance.rollout_context_truncated:
            lines.append("  时间线：较早上下文未加载（仅启动于有界尾部）")
        if not instance.sessions:
            lines.append("  没有可关联的活动会话")
        for session in instance.sessions:
            process = session.process
            title = (
                process.session_title
                or process.current_task
                or process.session_id[:8]
                or f"PID {process.pid}"
            )
            lines.append(
                f"  PID {process.pid:<6} {_session_status(session):<18} "
                f"{NETWORK_LABELS[session.network.state.value]:<10} {title}"
            )
            if session.current_failure:
                lines.append(
                    f"    失败：{session.current_failure.category} | {session.current_failure.message}"
                )
                if session.current_failure.additional_details:
                    lines.append(f"    详情：{session.current_failure.additional_details}")
            elif session.attention_request:
                lines.append(
                    f"    操作：{session.attention.value} | "
                    f"{session.attention_request.detail or session.attention_request.summary}"
                )
            elif session.alert:
                lines.append(
                    f"    {session.alert_level}：{session.alert_reason} "
                    f"({format_duration(session.alert_age_seconds)})"
                )
            elif session.network.reason:
                lines.append(f"    网络：{session.network.reason}")
            if session.observation.last_semantic_at is not None:
                semantic_age = max(0.0, time.time() - session.observation.last_semantic_at)
                evidence_age = (
                    max(0.0, time.time() - session.observation.last_evidence_at)
                    if session.observation.last_evidence_at is not None
                    else None
                )
                observation = (
                    f"    观察：{session.silence.state.value} | "
                    f"语义静默 {format_duration(semantic_age)}"
                )
                if evidence_age is not None:
                    observation += (
                        f" | 最近证据 {session.observation.last_evidence_source or '未知'} "
                        f"{format_duration(evidence_age)}前"
                    )
                lines.append(observation)
            if session.compactions:
                compact = session.compactions[-1]
                compact_line = f"    Compact：{compact.status} | {compact.trigger or 'unknown'}"
                if compact.duration_seconds is not None:
                    compact_line += f" | {compact.duration_seconds:.1f}s"
                if compact.reconstructed:
                    compact_line += " | reconstructed"
                lines.append(compact_line)
            for connection in session.network.connections:
                if not connection.tls_server_name and not connection.tls_alpn_protocols:
                    continue
                details = []
                if connection.tls_server_name:
                    details.append(f"SNI {connection.tls_server_name}")
                if connection.tls_alpn_protocols:
                    details.append(f"ALPN {', '.join(connection.tls_alpn_protocols)}")
                if connection.tls_versions:
                    details.append(f"TLS {', '.join(connection.tls_versions)}")
                lines.append(f"    TLS：{'; '.join(details)}")
            lines.extend(_session_metrics(session))
        if show_auxiliary:
            auxiliaries = [process for process in instance.processes if process.role != "session"]
            if auxiliaries:
                lines.append("  辅助进程")
                lines.extend(
                    f"    PID {process.pid:<6} {process.role:<10} {process.command}"
                    for process in auxiliaries
                )
    return "\n".join(lines)

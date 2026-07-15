"""Readable non-interactive snapshot output."""

from __future__ import annotations

from ..config import LIFECYCLE_LABELS, NETWORK_LABELS, RECOVERY_LABELS
from ..models import MonitorSnapshot, SessionHealth
from ..utils import format_duration


def _session_status(session: SessionHealth) -> str:
    if session.process_exited:
        return "进程已退出"
    lifecycle = LIFECYCLE_LABELS[session.lifecycle.value]
    recovery = RECOVERY_LABELS[session.recovery.value]
    return f"{lifecycle} / {recovery}" if recovery else lifecycle


def render_text(snapshot: MonitorSnapshot, show_auxiliary: bool = False) -> str:
    summary = snapshot.summary()
    lines = [
        f"Codex Net Health  {snapshot.generated_at}  刷新 {snapshot.interval_seconds:g}s",
        (
            f"实例 {summary['instances']}  会话 {summary['sessions']}  "
            f"失败 {summary['current_failures']}  阻塞 {summary['network_stalls']}"
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
            elif session.alert:
                lines.append(
                    f"    {session.alert_level}：{session.alert_reason} "
                    f"({format_duration(session.alert_age_seconds)})"
                )
            elif session.network.reason:
                lines.append(f"    网络：{session.network.reason}")
        if show_auxiliary:
            auxiliaries = [process for process in instance.processes if process.role != "session"]
            if auxiliaries:
                lines.append("  辅助进程")
                lines.extend(
                    f"    PID {process.pid:<6} {process.role:<10} {process.command}"
                    for process in auxiliaries
                )
    return "\n".join(lines)

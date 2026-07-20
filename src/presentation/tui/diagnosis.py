"""Concise current-problem diagnosis rendering for the Textual inspector."""

from __future__ import annotations

import time

from rich.console import Group
from rich.text import Text

from config import NETWORK_LABELS
from models import InstanceSnapshot, SessionHealth
from presentation.projection import instance_quality_issues
from presentation.tui.theme import STATE_COLORS
from utils import format_duration


def _diagnosis_renderable(
    session: SessionHealth,
    instance: InstanceSnapshot | None,
) -> object:
    confidence_labels = {"high": "高", "medium": "中", "low": "低"}

    def confidence_label(value: object) -> str:
        raw = str(getattr(value, "value", value) or "").lower()
        return confidence_labels.get(raw, raw or "未知")

    blocks: list[object] = []
    now = time.time()

    findings = session.diagnosis[:3]
    if findings:
        for finding in findings:
            color = STATE_COLORS.get(finding.severity, STATE_COLORS["info"])
            conclusion = Text("诊断结论\n", style="bold #64748b")
            conclusion.append(finding.conclusion, style=f"bold {color}")
            if finding.reason:
                conclusion.append(f"\n原因  {finding.reason}", style="#e2e8f0")
            provenance = finding.provenance
            freshness = (
                f"{finding.freshness_seconds:.1f}s"
                if finding.freshness_seconds is not None
                else "未知"
            )
            mode = "推导" if provenance.derived else "直接"
            conclusion.append(
                f"\n证据属性  {mode} · 置信度 {confidence_label(provenance.confidence)}"
                f" · 来源 {provenance.source or '未知'} · 新鲜度 {freshness}",
                style="#94a3b8",
            )
            for evidence in finding.evidence[:3]:
                conclusion.append(f"\n  • {evidence}", style="#cbd5e1")
            if finding.action:
                conclusion.append(f"\n建议  {finding.action}", style="#fbbf24")
            blocks.append(conclusion)
    else:
        conclusion = Text("诊断结论\n", style="bold #64748b")
        conclusion.append("当前未发现需要关注的问题", style="bold #4ade80")
        blocks.append(conclusion)

    observation = session.observation
    freshness_parts = []
    for label, timestamp in (
        ("rollout", observation.rollout_probe_at),
        ("process", observation.process_probe_at),
        ("network", observation.network_probe_at),
        ("log", observation.log_probe_at),
    ):
        freshness = (
            format_duration(max(0, now - timestamp)) if timestamp is not None else "-"
        )
        freshness_parts.append(f"{label} {freshness}")

    quality_issues: list[str] = []
    if observation.collector_stale:
        quality_issues.append(observation.collector_stale_reason or "会话采集证据已陈旧")
    if instance:
        quality_issues.extend(instance_quality_issues(instance))

    unparsed_events = [event for event in session.events if event.unparsed]
    incomplete_events = [
        event for event in session.events if not event.complete and not event.unparsed
    ]
    for event in unparsed_events[-3:]:
        payload = event.unparsed
        quality_issues.append(
            f"未解析 {payload.source_type} · {payload.length} chars · {payload.sha256[:10]}"
        )
    if incomplete_events:
        quality_issues.append(f"不完整协议事件 × {len(incomplete_events)}")

    quality = Text("\n数据质量\n", style="bold #64748b")
    quality.append("新鲜度  " + " · ".join(freshness_parts), style="#94a3b8")
    if quality_issues:
        for issue in quality_issues[:3]:
            quality.append(f"\n  ! {issue}", style="#fbbf24")
        remaining = len(quality_issues) - 3
        if remaining > 0:
            quality.append(f"\n  … 另有 {remaining} 项，使用 doctor 查看", style="#64748b")
    else:
        quality.append("\n采集源未发现降级", style="#4ade80")
    blocks.append(quality)

    if session.network.state.value not in {"IDLE", "ACTIVE"}:
        network = Text("\n网络摘要\n", style="bold #64748b")
        network.append(
            f"{NETWORK_LABELS[session.network.state.value]} · "
            f"{session.network.reason or '无附加说明'}",
            style="#fbbf24",
        )
        if session.network.connections:
            abnormal = [
                connection
                for connection in session.network.connections
                if connection.health.value not in {"IDLE", "ACTIVE"}
            ]
            packet_evidence = any(
                connection.tls_server_name
                or connection.tls_alpn_protocols
                or connection.tls_versions
                for connection in session.network.connections
            )
            network.append(
                f"\n异常连接 {len(abnormal)} · packet 辅助证据 "
                f"{'有' if packet_evidence else '无'}",
                style="#94a3b8",
            )
        blocks.append(network)

    if session.turns:
        turn = session.turns[-1]
        meaningful = any(
            value is not None
            for value in (
                turn.duration_seconds,
                turn.time_to_first_token_seconds,
                turn.tool_duration_seconds,
                turn.recovery_duration_seconds,
            )
        ) or bool(turn.longest_tool)
        if meaningful:
            bottleneck = Text("\n最近 Turn 瓶颈\n", style="bold #64748b")
            parts = []
            if turn.duration_seconds is not None:
                parts.append(f"总耗时 {turn.duration_seconds:.2f}s")
            if turn.time_to_first_token_seconds is not None:
                parts.append(f"TTFT {turn.time_to_first_token_seconds:.2f}s")
            if turn.tool_duration_seconds is not None:
                parts.append(f"工具 {turn.tool_duration_seconds:.2f}s")
            bottleneck.append(" · ".join(parts) or "暂无耗时数据")
            if turn.longest_tool:
                bottleneck.append(
                    f"\n最慢工具  {turn.longest_tool.display_name} · "
                    f"{turn.longest_tool.duration_seconds or 0:.2f}s"
                )
            if turn.reconnect_count or turn.fallback_count or turn.compact_count:
                bottleneck.append(
                    f"\n恢复事件  reconnect {turn.reconnect_count} · "
                    f"fallback {turn.fallback_count} · compact {turn.compact_count}",
                    style="#94a3b8",
                )
            blocks.append(bottleneck)

    capacity_warnings: list[str] = []
    if session.rate_limits and session.rate_limits.reached:
        capacity_warnings.append(session.rate_limits.reason or "速率限制已触达")
    if (
        session.token_usage
        and session.token_usage.context_percent is not None
        and session.token_usage.context_percent >= 85
    ):
        capacity_warnings.append(
            f"上下文使用 {session.token_usage.context_percent:.1f}%"
        )
    if capacity_warnings:
        capacity = Text("\n容量提醒\n", style="bold #64748b")
        capacity.append(" · ".join(capacity_warnings), style="#fbbf24")
        blocks.append(capacity)

    return Group(*blocks)

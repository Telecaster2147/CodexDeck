"""Concise current-problem diagnosis rendering for the Textual inspector."""

from __future__ import annotations

import time

from rich.console import Group
from rich.text import Text

from config import NETWORK_LABELS
from diagnostics import diagnostic_text
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
            completeness = "完整" if provenance.complete else "不完整"
            conclusion.append(
                f"\n证据属性  {mode} · 置信度 {confidence_label(provenance.confidence)}"
                f" · 来源 {provenance.source or '未知'} · 新鲜度 {freshness}"
                f" · 完整度 {completeness}",
                style="#94a3b8",
            )
            for evidence in finding.evidence[:3]:
                conclusion.append(f"\n主要证据  {evidence}", style="#cbd5e1")
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
        freshness = format_duration(max(0, now - timestamp)) if timestamp is not None else "-"
        freshness_parts.append(f"{label} {freshness}")

    quality_issues: list[str] = []
    if observation.collector_stale:
        quality_issues.append(observation.collector_stale_reason or "会话采集证据已陈旧")
    if instance:
        quality_issues.extend(instance_quality_issues(instance))

    unparsed_events = [event for event in session.events if event.unparsed]
    for event in unparsed_events[-3:]:
        payload = event.unparsed
        quality_issues.append(
            f"未解析 {payload.source_type} · {payload.length} chars · {payload.sha256[:10]}"
        )

    quality = Text("\n数据质量\n", style="bold #64748b")
    quality.append("新鲜度  " + " · ".join(freshness_parts), style="#94a3b8")
    if quality_issues:
        for issue in quality_issues[:3]:
            quality.append(f"\n  ! {issue}", style="#fbbf24")
        remaining = len(quality_issues) - 3
        if remaining > 0:
            quality.append(f"\n  … 另有 {remaining} 项，可展开异常详情", style="#64748b")
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
                f"\n异常连接 {len(abnormal)} · packet 辅助证据 {'有' if packet_evidence else '无'}",
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
        capacity_warnings.append(f"上下文使用 {session.token_usage.context_percent:.1f}%")
    if capacity_warnings:
        capacity = Text("\n容量提醒\n", style="bold #64748b")
        capacity.append(" · ".join(capacity_warnings), style="#fbbf24")
        blocks.append(capacity)

    return Group(*blocks)


def _diagnosis_details_renderable(
    session: SessionHealth,
    instance: InstanceSnapshot | None,
) -> tuple[int, object]:
    """Render complete, redacted diagnostic evidence behind a collapsed disclosure."""

    blocks: list[object] = []
    count = 0

    for finding in session.diagnosis:
        if finding.severity.lower() == "info":
            continue
        count += 1
        detail = Text(f"{finding.severity.upper()}  {finding.conclusion}", style="bold #fbbf24")
        if finding.reason:
            detail.append(f"\n原因  {finding.reason}", style="#e2e8f0")
        for evidence in finding.evidence:
            detail.append(f"\n证据  {evidence}", style="#cbd5e1")
        provenance = finding.provenance
        detail.append(
            f"\n来源  {provenance.source or '未知'} · 置信度 {provenance.confidence.value}"
            f" · {'推导' if provenance.derived else '直接'}"
            f" · {'完整' if provenance.complete else '不完整'}",
            style="#94a3b8",
        )
        if finding.action:
            detail.append(f"\n建议  {finding.action}", style="#fbbf24")
        blocks.append(detail)

    if session.observation.collector_stale:
        count += 1
        blocks.append(
            Text(
                "采集证据陈旧\n"
                + (session.observation.collector_stale_reason or "会话采集证据已陈旧"),
                style="#fbbf24",
            )
        )

    if instance:
        for message in instance.diagnostics:
            count += 1
            blocks.append(Text(f"实例诊断\n{diagnostic_text(message)}", style="#fbbf24"))
        rollout_activity = next(
            (
                item
                for item in instance.rollout_activity
                if item.get("path") == session.process.rollout_path
                and (
                    item.get("backlog_bytes")
                    or item.get("gap_count")
                    or item.get("metadata_backfill_dropped")
                    or item.get("terminal_parser_evictions")
                    or item.get("stream_uncertain")
                )
            ),
            None,
        )
        if rollout_activity:
            count += 1
            blocks.append(
                Text(
                    "Rollout 入口"
                    f"\n积压  {rollout_activity.get('backlog_bytes', 0)} bytes"
                    f"\n积压记录下界  {rollout_activity.get('backlog_records_lower_bound', 0)}"
                    f"\n积压年龄  {rollout_activity.get('backlog_age_seconds')}"
                    f"\n预算耗尽  {'是' if rollout_activity.get('budget_exceeded') else '否'}"
                    f"\n缺口  {rollout_activity.get('gap_count', 0)}"
                    f"\n跳过  {rollout_activity.get('skipped_bytes', 0)} bytes"
                    f"\n原因  {rollout_activity.get('gap_reason') or '-'}"
                    f"\n元数据回填丢弃  "
                    f"{rollout_activity.get('metadata_backfill_dropped', 0)}"
                    f"\n回填原因  "
                    f"{rollout_activity.get('metadata_backfill_reason') or '-'}"
                    f"\n终端解析关联淘汰  "
                    f"{rollout_activity.get('terminal_parser_evictions', 0)}"
                    f"\n解析淘汰原因  "
                    f"{rollout_activity.get('terminal_parser_eviction_reason') or '-'}"
                    f"\nGeneration  {rollout_activity.get('generation', 0)}"
                    f"\n流不确定  "
                    f"{'是' if rollout_activity.get('stream_uncertain') else '否'}"
                    f"\n流原因  "
                    f"{rollout_activity.get('stream_uncertainty_reason') or '-'}",
                    style="#fbbf24",
                )
            )
        for name, source in (
            ("TUI session log", instance.tui_session_log),
            ("Compact hook", instance.hook_events),
        ):
            if (
                not source.backlog_bytes
                and not source.gap_count
                and not source.stream_uncertain
                and source.source_authenticity.value == "high"
            ):
                continue
            count += 1
            blocks.append(
                Text(
                    f"{name} 入口"
                    f"\n积压  {source.backlog_bytes} bytes"
                    f"\n积压记录下界  {source.backlog_records_lower_bound}"
                    f"\n积压年龄  {source.backlog_age_seconds}"
                    f"\n预算耗尽  {'是' if source.budget_exceeded else '否'}"
                    f"\n缺口  {source.gap_count}"
                    f"\n跳过  {source.skipped_bytes} bytes"
                    f"\n原因  {source.gap_reason or '-'}"
                    f"\nGeneration  {source.generation}"
                    f"\n流不确定  {'是' if source.stream_uncertain else '否'}"
                    f"\n流原因  {source.stream_uncertainty_reason or '-'}"
                    f"\n真实性  {source.source_authenticity.value}"
                    f"\n身份绑定  {source.identity_binding.value}"
                    f"\n语义置信  {source.semantic_confidence.value}",
                    style="#fbbf24",
                )
            )
        for collector in instance.collector_health:
            if not collector.error and collector.stale_age_seconds is None:
                continue
            count += 1
            collector_detail = Text(f"采集器  {collector.name}", style="bold #fbbf24")
            collector_detail.append(
                f"\n错误  {collector.error or '-'}"
                f"\n连续失败  {collector.consecutive_failures}"
                f"\n耗时  {collector.duration_seconds:.3f}s"
                f"\n陈旧  {collector.stale_age_seconds if collector.stale_age_seconds is not None else '-'}"
                f"\n超出预算  {'是' if collector.budget_exceeded else '否'}",
                style="#cbd5e1",
            )
            blocks.append(collector_detail)
        family_counters = instance.protocol_family_counters
        dropped_families = family_counters.get(
            "unknown_dropped_family_count", 0
        ) + family_counters.get("shape_dropped_family_count", 0)
        if dropped_families:
            count += 1
            blocks.append(
                Text(
                    "协议族计数已截断"
                    f"\n容量  {family_counters.get('max_families_per_path', 0)} / rollout"
                    f"\n未知 other  {family_counters.get('unknown_other', 0)}"
                    f"\nshape other  {family_counters.get('shape_other', 0)}"
                    f"\n溢出观测  {dropped_families}",
                    style="#fbbf24",
                )
            )
        for event_type, event_count in instance.unknown_event_types.items():
            if any(
                event.unparsed and event.unparsed.source_type == event_type
                for event in session.events
            ):
                continue
            count += 1
            blocks.append(Text(f"未知协议类型\n{event_type} × {event_count}", style="#fbbf24"))

    for event in (event for event in session.events if event.unparsed):
        count += 1
        payload = event.unparsed
        protocol = Text(f"未知协议  {payload.source_type}", style="bold #fbbf24")
        protocol.append(
            f"\n来源时间  {event.presentation_timestamp:.3f}"
            f"\n裁决时间  {event.decision_timestamp:.3f}"
            f"\n来源  {event.source_id or event.source}"
            f"\n长度  {payload.length} chars"
            f"\nSHA-256  {payload.sha256}",
            style="#94a3b8",
        )
        full_payload = str(event.metadata.get("diagnostic_payload") or payload.preview)
        dropped_chars = int(event.metadata.get("diagnostic_payload_dropped_chars") or 0)
        payload_label = (
            f"脱敏 payload（已截断，省略 {dropped_chars} chars）"
            if dropped_chars
            else "完整脱敏 payload"
        )
        protocol.append(f"\n{payload_label}\n{full_payload}", style="#cbd5e1")
        blocks.append(protocol)

    for connection in session.network.connections:
        if connection.health.value in {"IDLE", "ACTIVE"}:
            continue
        count += 1
        blocks.append(
            Text(
                f"异常连接  {connection.local} -> {connection.peer}"
                f"\n状态  {connection.state} / {connection.health.value}"
                f"\n原因  {connection.reason}"
                f"\n队列  recv {connection.recv_q} / send {connection.send_q}"
                f"\n增量  sent {connection.sent_delta} / recv {connection.received_delta}"
                f" / ack {connection.acked_delta} / retrans {connection.retrans_delta}",
                style="#fbbf24",
            )
        )

    if not blocks:
        return 0, Text("当前没有异常详情", style="#64748b")
    return count, Group(*blocks)

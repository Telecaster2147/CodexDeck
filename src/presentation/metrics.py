"""Low-cardinality Prometheus text rendering for monitor snapshots."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from diagnostics import snapshot_diagnostics
from models import MonitorSnapshot
from utils import redact_sensitive


METRIC_FAMILIES = (
    "codexdeck_collection_duration_seconds",
    "codexdeck_observer_lag_seconds",
    "codexdeck_snapshot_age_seconds",
    "codexdeck_observer_skipped_ticks_total",
    "codexdeck_observer_degraded",
    "codexdeck_diagnostics",
    "codexdeck_temporal_source_age_seconds",
    "codexdeck_temporal_coherent",
    "codexdeck_instances",
    "codexdeck_sessions",
    "codexdeck_network_sessions",
    "codexdeck_silence_sessions",
    "codexdeck_state_axis_completeness",
    "codexdeck_compact_active",
    "codexdeck_terminal_sessions",
    "codexdeck_terminal_dropped_bytes",
    "codexdeck_terminal_upstream_truncated",
    "codexdeck_terminal_association_operations",
    "codexdeck_terminal_association_coverage",
    "codexdeck_terminal_association_unresolved_rate",
    "codexdeck_terminal_association_precision",
    "codexdeck_compact_total",
    "codexdeck_compact_duration_seconds_sum",
    "codexdeck_alerts",
    "codexdeck_attention_sessions",
    "codexdeck_snapshot_events",
    "codexdeck_tokens",
    "codexdeck_collector_healthy",
    "codexdeck_command_bytes",
    "codexdeck_command_records",
    "codexdeck_command_complete",
    "codexdeck_ingress_backlog_bytes",
    "codexdeck_ingress_gap_total",
    "codexdeck_ingress_backlog_records_lower_bound",
    "codexdeck_ingress_budget_exceeded",
    "codexdeck_history_queue_depth",
    "codexdeck_history_dropped_samples_total",
    "codexdeck_history_coalesced_samples_total",
    "codexdeck_history_persisted_samples_total",
    "codexdeck_history_writer_lag_seconds",
    "codexdeck_history_stats_age_seconds",
    "codexdeck_history_writer_healthy",
)


def _escape(value: object) -> str:
    return (
        redact_sensitive(str(value)).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    )


def _sample(name: str, value: int | float, **labels: object) -> str:
    suffix = ""
    if labels:
        rendered = ",".join(f'{key}="{_escape(labels[key])}"' for key in sorted(labels))
        suffix = "{" + rendered + "}"
    return f"{name}{suffix} {value}"


def _family(name: str, help_text: str, metric_type: str, samples: Iterable[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", *samples]


def render_prometheus(snapshot: MonitorSnapshot) -> str:
    """Render one snapshot without session, process, error, or endpoint labels."""

    lines: list[str] = []
    lines.extend(
        _family(
            "codexdeck_collection_duration_seconds",
            "Duration of the latest monitor collection.",
            "gauge",
            [
                _sample(
                    "codexdeck_collection_duration_seconds", snapshot.collection_duration_seconds
                )
            ],
        )
    )
    lines.extend(
        _family(
            "codexdeck_observer_lag_seconds",
            "Scheduling lag of the latest published observer sample.",
            "gauge",
            [_sample("codexdeck_observer_lag_seconds", snapshot.observer.scheduling_lag_seconds)],
        )
    )
    lines.extend(
        _family(
            "codexdeck_snapshot_age_seconds",
            "Age of the latest successful observer sample at publication.",
            "gauge",
            [_sample("codexdeck_snapshot_age_seconds", snapshot.observer.snapshot_age_seconds)],
        )
    )
    lines.extend(
        _family(
            "codexdeck_observer_skipped_ticks_total",
            "Sampling ticks skipped while one worker was in flight.",
            "counter",
            [_sample("codexdeck_observer_skipped_ticks_total", snapshot.observer.skipped_ticks)],
        )
    )
    lines.extend(
        _family(
            "codexdeck_observer_degraded",
            "Whether observer scheduling or freshness is degraded.",
            "gauge",
            [_sample("codexdeck_observer_degraded", int(snapshot.observer.degraded))],
        )
    )
    diagnostic_counts = Counter(
        (item.code, item.severity, item.domain, item.recovery_state)
        for item in snapshot_diagnostics(snapshot)
    )
    lines.extend(
        _family(
            "codexdeck_diagnostics",
            "Active bounded diagnostics by stable code and severity.",
            "gauge",
            [
                _sample(
                    "codexdeck_diagnostics",
                    count,
                    code=code,
                    severity=severity,
                    domain=domain,
                    recovery=recovery,
                )
                for (code, severity, domain, recovery), count in sorted(
                    diagnostic_counts.items()
                )
            ],
        )
    )
    lines.extend(
        _family(
            "codexdeck_temporal_source_age_seconds",
            "Age of each source observation retained in the composite snapshot.",
            "gauge",
            [
                _sample(
                    "codexdeck_temporal_source_age_seconds",
                    source.stale_age_seconds or 0.0,
                    source=source.source,
                    complete=str(source.complete).lower(),
                )
                for source in snapshot.temporal.sources
            ],
        )
    )
    lines.extend(
        _family(
            "codexdeck_temporal_coherent",
            "Whether source observation windows satisfy the maximum skew contract.",
            "gauge",
            [_sample("codexdeck_temporal_coherent", int(snapshot.temporal.coherent))],
        )
    )
    lines.extend(
        _family(
            "codexdeck_instances",
            "Discovered Codex instances.",
            "gauge",
            [_sample("codexdeck_instances", len(snapshot.instances))],
        )
    )

    session_samples: list[str] = []
    network_samples: list[str] = []
    alert_samples: list[str] = []
    attention_samples: list[str] = []
    event_samples: list[str] = []
    token_samples: list[str] = []
    collector_samples: list[str] = []
    command_byte_samples: list[str] = []
    command_record_samples: list[str] = []
    command_complete_samples: list[str] = []
    silence_samples: list[str] = []
    completeness_samples: list[str] = []
    compact_active_samples: list[str] = []
    compact_terminal_samples: list[str] = []
    compact_duration_samples: list[str] = []
    terminal_samples: list[str] = []
    terminal_dropped_samples: list[str] = []
    terminal_truncated_samples: list[str] = []
    terminal_association_samples: list[str] = []
    terminal_coverage_samples: list[str] = []
    terminal_unresolved_rate_samples: list[str] = []
    terminal_precision_samples: list[str] = []
    ingress_backlog_samples: list[str] = []
    ingress_backlog_record_samples: list[str] = []
    ingress_gap_samples: list[str] = []
    ingress_budget_samples: list[str] = []
    for instance in snapshot.instances:
        lifecycle = Counter(session.lifecycle.value for session in instance.sessions)
        network = Counter(session.network.state.value for session in instance.sessions)
        alerts = Counter(
            (session.alert_level or "active") for session in instance.sessions if session.alert
        )
        attention = Counter(
            session.attention.value for session in instance.sessions if session.attention_request
        )
        silence = Counter(session.silence.state.value for session in instance.sessions)
        events = Counter(
            event.kind.upper() for session in instance.sessions for event in session.events
        )
        for state, count in sorted(lifecycle.items()):
            session_samples.append(
                _sample("codexdeck_sessions", count, instance=instance.instance_id, state=state)
            )
        for state, count in sorted(network.items()):
            network_samples.append(
                _sample(
                    "codexdeck_network_sessions",
                    count,
                    instance=instance.instance_id,
                    state=state,
                )
            )
        for category, count in sorted(alerts.items()):
            alert_samples.append(
                _sample(
                    "codexdeck_alerts",
                    count,
                    instance=instance.instance_id,
                    category=category,
                )
            )
        for state, count in sorted(attention.items()):
            attention_samples.append(
                _sample(
                    "codexdeck_attention_sessions",
                    count,
                    instance=instance.instance_id,
                    state=state,
                )
            )
        for state, count in sorted(silence.items()):
            silence_samples.append(
                _sample(
                    "codexdeck_silence_sessions",
                    count,
                    instance=instance.instance_id,
                    state=state,
                )
            )
        completeness_counts: Counter[tuple[str, str]] = Counter()
        for session in instance.sessions:
            for axis in (
                session.completeness.lifecycle,
                session.completeness.attention,
                session.completeness.failure_recovery,
                session.completeness.terminal_ownership,
                session.completeness.network,
                session.completeness.silence,
            ):
                completeness_counts[(axis.axis, "complete" if axis.complete else "incomplete")] += 1
        for (axis, status), count in sorted(completeness_counts.items()):
            completeness_samples.append(
                _sample(
                    "codexdeck_state_axis_completeness",
                    count,
                    instance=instance.instance_id,
                    axis=axis,
                    status=status,
                )
            )
        terminals = Counter(
            terminal.capability.value
            for session in instance.sessions
            for terminal in session.terminal_sessions
        )
        for capability, count in sorted(terminals.items()):
            terminal_samples.append(
                _sample(
                    "codexdeck_terminal_sessions",
                    count,
                    instance=instance.instance_id,
                    capability=capability,
                )
            )
        terminal_dropped_samples.append(
            _sample(
                "codexdeck_terminal_dropped_bytes",
                sum(
                    terminal.dropped_bytes
                    for session in instance.sessions
                    for terminal in session.terminal_sessions
                ),
                instance=instance.instance_id,
            )
        )
        terminal_truncated_samples.append(
            _sample(
                "codexdeck_terminal_upstream_truncated",
                sum(
                    terminal.upstream_truncated
                    for session in instance.sessions
                    for terminal in session.terminal_sessions
                ),
                instance=instance.instance_id,
            )
        )
        association_totals: Counter[str] = Counter()
        eligible = 0
        associated = 0
        labeled_correct = 0
        labeled_incorrect = 0
        for session in instance.sessions:
            association = session.terminal_association
            eligible += association.eligible_operations
            associated += association.associated_operations
            labeled_correct += association.labeled_correct
            labeled_incorrect += association.labeled_incorrect
            for status in ("confirmed", "ambiguous", "conflicting", "unresolved", "dropped"):
                association_totals[status] += int(getattr(association, status))
        for status, count in sorted(association_totals.items()):
            terminal_association_samples.append(
                _sample(
                    "codexdeck_terminal_association_operations",
                    count,
                    instance=instance.instance_id,
                    status=status,
                )
            )
        if eligible:
            terminal_coverage_samples.append(
                _sample(
                    "codexdeck_terminal_association_coverage",
                    associated / eligible,
                    instance=instance.instance_id,
                )
            )
            unresolved = association_totals["unresolved"] + association_totals["conflicting"]
            terminal_unresolved_rate_samples.append(
                _sample(
                    "codexdeck_terminal_association_unresolved_rate",
                    unresolved / eligible,
                    instance=instance.instance_id,
                )
            )
        labeled = labeled_correct + labeled_incorrect
        if labeled:
            terminal_precision_samples.append(
                _sample(
                    "codexdeck_terminal_association_precision",
                    labeled_correct / labeled,
                    instance=instance.instance_id,
                )
            )
        active_compacts: Counter[str] = Counter()
        terminal_compacts: Counter[tuple[str, str]] = Counter()
        duration_totals: Counter[tuple[str, str]] = Counter()
        for session in instance.sessions:
            for compact in session.compactions:
                trigger = compact.trigger or "unknown"
                if compact.status in {"requested", "candidate", "running"}:
                    active_compacts[trigger] += 1
                elif compact.status in {"completed", "failed", "aborted"}:
                    terminal_compacts[(trigger, compact.status)] += 1
                    if compact.duration_seconds is not None:
                        duration_totals[(trigger, compact.status)] += compact.duration_seconds
        for trigger, count in sorted(active_compacts.items()):
            compact_active_samples.append(
                _sample(
                    "codexdeck_compact_active",
                    count,
                    instance=instance.instance_id,
                    trigger=trigger,
                )
            )
        for (trigger, status), count in sorted(terminal_compacts.items()):
            compact_terminal_samples.append(
                _sample(
                    "codexdeck_compact_total",
                    count,
                    instance=instance.instance_id,
                    trigger=trigger,
                    status=status,
                )
            )
        for (trigger, status), total in sorted(duration_totals.items()):
            compact_duration_samples.append(
                _sample(
                    "codexdeck_compact_duration_seconds_sum",
                    total,
                    instance=instance.instance_id,
                    trigger=trigger,
                    status=status,
                )
            )
        for event_type, count in sorted(events.items()):
            event_samples.append(
                _sample(
                    "codexdeck_snapshot_events",
                    count,
                    instance=instance.instance_id,
                    event_type=event_type,
                )
            )

        token_totals: Counter[str] = Counter()
        for session in instance.sessions:
            usage = session.cumulative_token_usage or session.token_usage
            if usage is None:
                continue
            for category, value in (
                ("input", usage.input_tokens),
                ("cached_input", usage.cached_input_tokens),
                ("output", usage.output_tokens),
                ("reasoning_output", usage.reasoning_output_tokens),
                ("total", usage.total_tokens),
            ):
                if value is not None:
                    token_totals[category] += value
        for category, value in sorted(token_totals.items()):
            token_samples.append(
                _sample(
                    "codexdeck_tokens",
                    value,
                    instance=instance.instance_id,
                    category=category,
                )
            )

        for collector in instance.collector_health:
            category = (collector.name or "unknown").split(":", 1)[0]
            collector_samples.append(
                _sample(
                    "codexdeck_collector_healthy",
                    int(not collector.error and not collector.budget_exceeded),
                    instance=instance.instance_id,
                    category=category,
                )
            )
        rollout_activities = instance.rollout_activity
        ingress_sources = (
            (
                "rollout",
                sum(int(item.get("backlog_bytes") or 0) for item in rollout_activities),
                sum(
                    int(item.get("backlog_records_lower_bound") or 0) for item in rollout_activities
                ),
                sum(int(item.get("gap_count") or 0) for item in rollout_activities),
                any(bool(item.get("budget_exceeded")) for item in rollout_activities),
            ),
            (
                "tui_session_log",
                instance.tui_session_log.backlog_bytes,
                instance.tui_session_log.backlog_records_lower_bound,
                instance.tui_session_log.gap_count,
                instance.tui_session_log.budget_exceeded,
            ),
            (
                "hook_events",
                instance.hook_events.backlog_bytes,
                instance.hook_events.backlog_records_lower_bound,
                instance.hook_events.gap_count,
                instance.hook_events.budget_exceeded,
            ),
        )
        for source, backlog, backlog_records, gaps, exceeded in ingress_sources:
            ingress_backlog_samples.append(
                _sample(
                    "codexdeck_ingress_backlog_bytes",
                    backlog,
                    instance=instance.instance_id,
                    source=source,
                )
            )
            ingress_backlog_record_samples.append(
                _sample(
                    "codexdeck_ingress_backlog_records_lower_bound",
                    backlog_records,
                    instance=instance.instance_id,
                    source=source,
                )
            )
            ingress_gap_samples.append(
                _sample(
                    "codexdeck_ingress_gap_total",
                    gaps,
                    instance=instance.instance_id,
                    source=source,
                )
            )
            ingress_budget_samples.append(
                _sample(
                    "codexdeck_ingress_budget_exceeded",
                    int(exceeded),
                    instance=instance.instance_id,
                    source=source,
                )
            )

    for collector in snapshot.collector_health:
        command = collector.command
        if command is None:
            continue
        category = (collector.name or "unknown").split(":", 1)[0]
        command_complete_samples.append(
            _sample(
                "codexdeck_command_complete",
                int(command.complete),
                category=category,
            )
        )
        for stream, disposition, value in (
            ("stdout", "read", command.stdout_bytes_read),
            ("stdout", "retained", command.stdout_bytes_retained),
            ("stdout", "filtered", command.stdout_bytes_filtered),
            ("stderr", "read", command.stderr_bytes_read),
            ("stderr", "retained", command.stderr_bytes_retained),
        ):
            command_byte_samples.append(
                _sample(
                    "codexdeck_command_bytes",
                    value,
                    category=category,
                    stream=stream,
                    disposition=disposition,
                )
            )
        for disposition, value in (
            ("retained", command.records_retained),
            ("filtered", command.records_filtered),
            ("dropped", command.records_dropped),
        ):
            command_record_samples.append(
                _sample(
                    "codexdeck_command_records",
                    value,
                    category=category,
                    disposition=disposition,
                )
            )

    lines.extend(
        _family("codexdeck_sessions", "Sessions by lifecycle state.", "gauge", session_samples)
    )
    lines.extend(
        _family(
            "codexdeck_network_sessions",
            "Sessions by network state.",
            "gauge",
            network_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_silence_sessions",
            "Sessions by silence assessment state.",
            "gauge",
            silence_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_state_axis_completeness",
            "Sessions by state-axis evidence completeness.",
            "gauge",
            completeness_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_compact_active",
            "Active compact operations by trigger.",
            "gauge",
            compact_active_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_sessions",
            "Retained terminal sessions by output capability.",
            "gauge",
            terminal_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_dropped_bytes",
            "Terminal transcript bytes dropped by CodexDeck bounds.",
            "gauge",
            terminal_dropped_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_upstream_truncated",
            "Terminal transcripts marked truncated by the upstream source.",
            "gauge",
            terminal_truncated_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_association_operations",
            "Terminal operations in the retained association window by status.",
            "gauge",
            terminal_association_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_association_coverage",
            "Associated terminal operations divided by eligible operations.",
            "gauge",
            terminal_coverage_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_association_unresolved_rate",
            "Unresolved or conflicting terminal operations divided by eligible operations.",
            "gauge",
            terminal_unresolved_rate_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_terminal_association_precision",
            "Correct associations divided by labeled associations when labels exist.",
            "gauge",
            terminal_precision_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_compact_total",
            "Compact terminal operations retained in the snapshot.",
            "counter",
            compact_terminal_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_compact_duration_seconds_sum",
            "Sum of compact operation durations retained in the snapshot.",
            "counter",
            compact_duration_samples,
        )
    )
    lines.extend(_family("codexdeck_alerts", "Active alerts by category.", "gauge", alert_samples))
    lines.extend(
        _family(
            "codexdeck_attention_sessions",
            "Sessions waiting for user action by attention state.",
            "gauge",
            attention_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_snapshot_events",
            "Events retained in the current snapshot by type.",
            "gauge",
            event_samples,
        )
    )
    lines.extend(
        _family("codexdeck_tokens", "Latest aggregate token usage.", "gauge", token_samples)
    )
    lines.extend(
        _family(
            "codexdeck_collector_healthy",
            "Whether the latest instance collector sample is healthy.",
            "gauge",
            collector_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_command_bytes",
            "Bytes read, retained, or filtered by bounded system commands.",
            "gauge",
            command_byte_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_command_records",
            "Records retained, filtered, or dropped by bounded system commands.",
            "gauge",
            command_record_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_command_complete",
            "Whether the latest bounded system command completed without truncation.",
            "gauge",
            command_complete_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_ingress_backlog_bytes",
            "Unconsumed bytes remaining in bounded incremental evidence sources.",
            "gauge",
            ingress_backlog_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_ingress_gap_total",
            "Explicit oversized or bootstrap JSONL gaps by evidence source.",
            "counter",
            ingress_gap_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_ingress_backlog_records_lower_bound",
            "Known minimum unconsumed JSONL records by evidence source.",
            "gauge",
            ingress_backlog_record_samples,
        )
    )
    lines.extend(
        _family(
            "codexdeck_ingress_budget_exceeded",
            "Whether the latest evidence-source quantum left a backlog.",
            "gauge",
            ingress_budget_samples,
        )
    )
    history = snapshot.history
    history_values = (
        (
            "codexdeck_history_queue_depth",
            "Queued history snapshots waiting for the single writer.",
            "gauge",
            history.queue_depth,
        ),
        (
            "codexdeck_history_dropped_samples_total",
            "History samples dropped by the bounded queue.",
            "counter",
            history.dropped_samples,
        ),
        (
            "codexdeck_history_coalesced_samples_total",
            "History samples coalesced by the bounded queue.",
            "counter",
            history.coalesced_samples,
        ),
        (
            "codexdeck_history_persisted_samples_total",
            "History samples persisted by the background writer.",
            "counter",
            history.persisted_samples,
        ),
        (
            "codexdeck_history_writer_lag_seconds",
            "Lag of the latest completed history write.",
            "gauge",
            history.writer_lag_seconds or 0.0,
        ),
        (
            "codexdeck_history_stats_age_seconds",
            "Age of cached history window statistics.",
            "gauge",
            history.stats_age_seconds or 0.0,
        ),
        (
            "codexdeck_history_writer_healthy",
            "Whether the optional history writer is enabled and healthy.",
            "gauge",
            int(history.enabled and not history.error and not history.shutdown_timed_out),
        ),
    )
    for name, help_text, metric_type, value in history_values:
        lines.extend(_family(name, help_text, metric_type, [_sample(name, value)]))
    return "\n".join(lines) + "\n"


class PrometheusMetrics:
    """Small adapter suitable for textfile or one-shot CLI integration."""

    def render(self, snapshot: MonitorSnapshot) -> str:
        return render_prometheus(snapshot)

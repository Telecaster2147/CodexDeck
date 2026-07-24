"""Runtime health tracking for independent monitor collectors."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable

from models import (
    CollectorHealth,
    CommandExecutionSummary,
    Diagnostic,
    DiagnosticParameter,
    MonitorSnapshot,
)
from utils import contains_invisible_text


MAX_DIAGNOSTICS = 128


def _parameters(**values: str | int | float | bool | None) -> tuple[DiagnosticParameter, ...]:
    return tuple(DiagnosticParameter(name, value) for name, value in sorted(values.items()))


def make_diagnostic(
    code: str,
    *,
    severity: str = "warning",
    domain: str = "observer",
    source: str = "codexdeck",
    observed_at: float | None = None,
    message_key: str | None = None,
    recovery_state: str = "active",
    **parameters: str | int | float | bool | None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        domain=domain,
        source=source,
        observed_at=observed_at,
        privacy_class="public_bounded",
        parameters=_parameters(**parameters),
        message_key=message_key or code.casefold(),
        recovery_state=recovery_state,
    )


def normalize_diagnostic(value: Diagnostic | str, *, source: str) -> Diagnostic:
    if isinstance(value, Diagnostic):
        return value
    encoded = str(value).encode("utf-8", "replace")
    return make_diagnostic(
        "SOURCE_REPORTED_DEGRADED",
        source=source,
        message_key="source_reported_degraded",
        length=len(encoded),
        fingerprint=hashlib.sha256(encoded).hexdigest()[:16],
    )


def _parameter_map(diagnostic: Diagnostic) -> dict[str, object]:
    return {parameter.name: parameter.value for parameter in diagnostic.parameters}


def diagnostic_text(diagnostic: Diagnostic | str | dict[str, object]) -> str:
    if isinstance(diagnostic, dict):
        message_key = str(diagnostic.get("message_key") or diagnostic.get("code") or "diagnostic")
        raw_parameters = diagnostic.get("parameters")
        values = {
            str(item.get("name")): item.get("value")
            for item in raw_parameters
            if isinstance(item, dict)
        } if isinstance(raw_parameters, list) else {}
        code = str(diagnostic.get("code") or "DIAGNOSTIC")
    else:
        item = normalize_diagnostic(diagnostic, source="legacy")
        message_key = item.message_key
        values = _parameter_map(item)
        code = item.code
    templates = {
        "source_reported_degraded": "采集来源报告降级 (fingerprint {fingerprint})",
        "collector_degraded": "采集器 {collector} 降级 ({reason})",
        "observer_degraded": "observer 调度或快照新鲜度降级 ({reason})",
        "discovery_unresolved": "存在 {count} 个未确认 Codex 进程候选",
        "protocol_unknown": "存在 {count} 条未知协议记录",
        "ingress_gap": "增量来源存在显式缺口 ({count})",
        "ingress_backlog": "增量来源仍有 {bytes} bytes 积压",
        "terminal_association_conflict": "terminal 关联存在 {count} 个冲突或未决操作",
        "evidence_incomplete": "会话有 {count} 个状态轴证据不完整",
        "observer_blind": "observer blind：当前会话证据源不可见",
        "identity_collision": "instance surrogate identity collision；已拒绝合并",
        "unicode_invisible": "关键操作文本包含 {count} 个不可见或方向控制字段",
        "temporal_skew": "跨来源观察窗口偏差 {skew}s，超过 {limit}s",
        "adapter_failed": "adapter {adapter} 返回 {status} ({error_code})",
    }
    template = templates.get(message_key, code)
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return code


def snapshot_diagnostics(snapshot: MonitorSnapshot) -> tuple[Diagnostic, ...]:
    facts: list[Diagnostic] = [
        normalize_diagnostic(item, source="snapshot") for item in snapshot.diagnostics
    ]
    for collector in snapshot.collector_health:
        if (
            collector.error
            or collector.consecutive_failures
            or collector.budget_exceeded
            or collector.stale_age_seconds is not None
        ):
            reason = (
                "budget_exceeded"
                if collector.budget_exceeded
                else "stale"
                if collector.stale_age_seconds is not None
                else "failure"
            )
            facts.append(
                make_diagnostic(
                    "COLLECTOR_DEGRADED",
                    source=collector.name,
                    message_key="collector_degraded",
                    collector=collector.name,
                    reason=reason,
                )
            )
    if snapshot.observer.degraded:
        facts.append(
            make_diagnostic(
                "OBSERVER_DEGRADED",
                source="sampling",
                message_key="observer_degraded",
                reason=snapshot.observer.reason,
            )
        )
    if snapshot.discovery.unresolved:
        facts.append(
            make_diagnostic(
                "DISCOVERY_UNRESOLVED",
                source="process_discovery",
                message_key="discovery_unresolved",
                count=snapshot.discovery.unresolved,
            )
        )
    if not snapshot.temporal.coherent:
        facts.append(
            make_diagnostic(
                "TEMPORAL_SKEW",
                source="snapshot_publisher",
                message_key="temporal_skew",
                skew=round(snapshot.temporal.actual_source_skew_seconds, 3),
                limit=round(snapshot.temporal.max_source_skew_seconds, 3),
            )
        )
    for instance in snapshot.instances:
        facts.extend(normalize_diagnostic(item, source="instance") for item in instance.diagnostics)
        for result in instance.adapter_results:
            if result.status.value in {"failed", "incomplete"}:
                facts.append(
                    make_diagnostic(
                        "ADAPTER_FAILED",
                        source=result.source,
                        message_key="adapter_failed",
                        adapter=result.source,
                        status=result.status.value,
                        error_code=result.error_code or "partial_result",
                    )
                )
        unknown = sum(instance.unknown_event_types.values())
        if unknown:
            facts.append(
                make_diagnostic(
                    "PROTOCOL_UNKNOWN",
                    source="rollout",
                    message_key="protocol_unknown",
                    count=unknown,
                )
            )
        gap_count = sum(int(item.get("gap_count", 0) or 0) for item in instance.rollout_activity)
        backlog = sum(int(item.get("backlog_bytes", 0) or 0) for item in instance.rollout_activity)
        if gap_count:
            facts.append(
                make_diagnostic(
                    "INGRESS_GAP",
                    source="rollout",
                    message_key="ingress_gap",
                    count=gap_count,
                )
            )
        if backlog:
            facts.append(
                make_diagnostic(
                    "INGRESS_BACKLOG",
                    severity="info",
                    source="rollout",
                    message_key="ingress_backlog",
                    bytes=backlog,
                )
            )
        for session in instance.sessions:
            failure = session.current_failure or session.latest_failure
            decision_text = (
                session.process.session_title,
                session.process.current_task,
                session.process.cwd,
                session.current_operation.label,
                session.current_operation.detail,
                failure.message if failure else "",
                *(terminal.command for terminal in session.terminal_sessions),
                *(terminal.cwd for terminal in session.terminal_sessions),
            )
            invisible_count = sum(contains_invisible_text(value) for value in decision_text if value)
            if invisible_count:
                facts.append(
                    make_diagnostic(
                        "UNICODE_INVISIBLE",
                        domain="presentation",
                        source="operator_text",
                        message_key="unicode_invisible",
                        count=invisible_count,
                    )
                )
            if session.silence.state.value == "OBSERVER_BLIND":
                facts.append(
                    make_diagnostic(
                        "OBSERVER_BLIND",
                        source="state_machine",
                        message_key="observer_blind",
                    )
                )
            unresolved = (
                session.terminal_association.conflicting
                + session.terminal_association.unresolved
                + session.terminal_association.ambiguous
            )
            if unresolved:
                facts.append(
                    make_diagnostic(
                        "TERMINAL_ASSOCIATION_CONFLICT",
                        source="terminal",
                        message_key="terminal_association_conflict",
                        count=unresolved,
                    )
                )
            incomplete = len(session.completeness.incomplete_axes)
            if incomplete:
                facts.append(
                    make_diagnostic(
                        "EVIDENCE_INCOMPLETE",
                        source="state_machine",
                        message_key="evidence_incomplete",
                        count=incomplete,
                    )
                )
    unique: dict[tuple[str, str, tuple[DiagnosticParameter, ...]], Diagnostic] = {}
    for diagnostic in facts:
        unique[(diagnostic.code, diagnostic.source, diagnostic.parameters)] = diagnostic
    return tuple(unique.values())[:MAX_DIAGNOSTICS]


def observation_degraded(snapshot: MonitorSnapshot) -> bool:
    return any(
        diagnostic.severity in {"warning", "error", "fatal"}
        and diagnostic.recovery_state == "active"
        for diagnostic in snapshot_diagnostics(snapshot)
    )


@dataclass
class _CollectorState:
    last_success_at: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    duration_seconds: float = 0.0
    command: CommandExecutionSummary | None = None


class CollectorTracker:
    """Track collector timings and failures without coupling adapters together."""

    def __init__(
        self,
        budget_seconds: float,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget_seconds = budget_seconds
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.states: dict[str, _CollectorState] = {}

    def record(
        self,
        name: str,
        started: float,
        error: BaseException | str | None = None,
        *,
        command_result: object | None = None,
    ) -> None:
        state = self.states.setdefault(name, _CollectorState())
        state.duration_seconds = max(0.0, self.monotonic() - started)
        source = command_result or getattr(error, "result", None)
        if source is not None:
            state.command = CommandExecutionSummary(
                command_name=str(getattr(source, "command_name", "")),
                complete=bool(getattr(source, "complete", False)),
                reason=str(getattr(source, "reason", ""))[:128],
                exit_code=getattr(source, "exit_code", None),
                duration_seconds=float(getattr(source, "duration_seconds", 0.0)),
                stdout_bytes_read=int(getattr(source, "stdout_bytes_read", 0)),
                stdout_bytes_retained=int(getattr(source, "stdout_bytes_retained", 0)),
                stdout_bytes_filtered=int(getattr(source, "stdout_bytes_filtered", 0)),
                stderr_bytes_read=int(getattr(source, "stderr_bytes_read", 0)),
                stderr_bytes_retained=int(getattr(source, "stderr_bytes_retained", 0)),
                stdout_lines_read=int(getattr(source, "stdout_lines_read", 0)),
                stderr_lines_read=int(getattr(source, "stderr_lines_read", 0)),
                records_retained=int(getattr(source, "records_retained", 0)),
                records_filtered=int(getattr(source, "records_filtered", 0)),
                records_dropped=int(getattr(source, "records_dropped", 0)),
            )
        else:
            state.command = None
        if error is None:
            state.last_success_at = self.wall_clock()
            state.consecutive_failures = 0
            state.error = ""
        else:
            state.consecutive_failures += 1
            state.error = str(error)

    def snapshot(self) -> list[CollectorHealth]:
        now = self.wall_clock()
        return [
            CollectorHealth(
                name=name,
                duration_seconds=state.duration_seconds,
                last_success_at=state.last_success_at,
                stale_age_seconds=(
                    max(0.0, now - state.last_success_at)
                    if state.last_success_at is not None and state.consecutive_failures
                    else None
                ),
                consecutive_failures=state.consecutive_failures,
                error=state.error,
                budget_exceeded=state.duration_seconds > self.budget_seconds,
                command=state.command,
            )
            for name, state in sorted(self.states.items())
        ]

"""Versioned explanations projected from authoritative derived session state."""

from __future__ import annotations

from codexdeck.models import (
    AttentionState,
    AxisReason,
    Confidence,
    DiagnosisFinding,
    EvidenceTimelineEntry,
    LifecycleState,
    NetworkState,
    NormalizedEvent,
    Provenance,
    RecoveryState,
    SessionHealth,
    SilenceState,
)

REASON_SCHEMA_VERSION = 1
MAX_AXIS_REASONS = 16
MAX_EVIDENCE_TIMELINE = 64

EVENT_AXIS = {
    "TURN_STARTED": "lifecycle",
    "REQUEST_SENT": "lifecycle",
    "RESPONSE_STARTED": "lifecycle",
    "MODEL_PROGRESS": "lifecycle",
    "REASONING_SUMMARY": "lifecycle",
    "PLAN_UPDATED": "lifecycle",
    "TOOL_RUNNING": "lifecycle",
    "TOOL_COMPLETED": "lifecycle",
    "COMPACTING": "lifecycle",
    "COMPACT_COMPLETED": "lifecycle",
    "TURN_COMPLETED": "lifecycle",
    "TURN_FAILED": "failure_recovery",
    "TURN_ABORTED": "lifecycle",
    "ACTION_REQUIRED": "attention",
    "ACTION_RESOLVED": "attention",
    "RECONNECTING": "failure_recovery",
    "TRANSPORT_FALLBACK": "failure_recovery",
    "RECOVERED": "failure_recovery",
    "UNPARSED_PAYLOAD": "protocol",
    "PROCESS_EXITED": "lifecycle",
    "SESSION_CLOSED": "lifecycle",
}

EVENT_REASON = {
    "TURN_STARTED": "lifecycle.protocol_turn_started",
    "REQUEST_SENT": "lifecycle.protocol_waiting_response",
    "RESPONSE_STARTED": "lifecycle.protocol_generating",
    "MODEL_PROGRESS": "lifecycle.protocol_generating",
    "REASONING_SUMMARY": "lifecycle.protocol_generating",
    "PLAN_UPDATED": "lifecycle.protocol_generating",
    "TOOL_RUNNING": "lifecycle.protocol_tool_running",
    "TOOL_COMPLETED": "lifecycle.protocol_tool_completed",
    "COMPACTING": "lifecycle.protocol_compacting",
    "COMPACT_COMPLETED": "lifecycle.protocol_compact_completed",
    "TURN_COMPLETED": "lifecycle.protocol_completed",
    "TURN_FAILED": "failure.current_turn",
    "TURN_ABORTED": "lifecycle.protocol_aborted",
    "ACTION_REQUIRED": "attention.interaction_pending",
    "ACTION_RESOLVED": "attention.resolved",
    "RECONNECTING": "recovery.reconnecting",
    "TRANSPORT_FALLBACK": "recovery.transport_fallback",
    "RECOVERED": "recovery.progress_after_reconnect",
    "UNPARSED_PAYLOAD": "unknown.unsupported_protocol_family",
    "PROCESS_EXITED": "lifecycle.process_exited",
    "SESSION_CLOSED": "lifecycle.session_closed",
}

LIFECYCLE_REASON = {
    LifecycleState.IDLE: ("lifecycle.idle", "当前没有权威活动阶段"),
    LifecycleState.STARTING: ("lifecycle.protocol_starting", "协议记录表明 turn 正在开始"),
    LifecycleState.WAITING_RESPONSE: (
        "lifecycle.protocol_waiting_response",
        "请求已发送，正在等待上游响应",
    ),
    LifecycleState.GENERATING: (
        "lifecycle.protocol_generating",
        "协议记录表明模型正在生成",
    ),
    LifecycleState.RUNNING_TOOL: (
        "lifecycle.protocol_tool_running",
        "协议记录表明工具正在执行",
    ),
    LifecycleState.COMPACTING: (
        "lifecycle.protocol_compacting",
        "协议记录表明上下文正在压缩",
    ),
    LifecycleState.COMPLETED: (
        "lifecycle.protocol_completed",
        "结构化 turn terminal 记录确认完成",
    ),
    LifecycleState.FAILED: ("lifecycle.protocol_failed", "结构化失败记录确认当前 turn 失败"),
    LifecycleState.ABORTED: ("lifecycle.protocol_aborted", "结构化记录确认当前 turn 已中止"),
}

ATTENTION_REASON = {
    AttentionState.APPROVAL: ("attention.approval_pending", "等待执行审批"),
    AttentionState.PERMISSIONS: ("attention.permission_required", "等待权限确认"),
    AttentionState.USER_INPUT: ("attention.user_input_required", "等待用户输入"),
    AttentionState.MCP_ELICITATION: ("attention.mcp_elicitation", "等待 MCP 交互输入"),
    AttentionState.AUTH_ELICITATION: ("attention.auth_required", "等待认证输入"),
}

CRITICAL_REASON_PREFIXES = (
    "attention.",
    "failure.",
    "silence.",
    "unknown.",
    "ownership.",
    "network.two_window",
)


def _latest_event(events: list[NormalizedEvent], kinds: set[str]) -> NormalizedEvent | None:
    return next((event for event in reversed(events) if event.kind in kinds), None)


def _evidence_ref(event: NormalizedEvent | None, fallback: str) -> tuple[str, ...]:
    if event is None:
        return (fallback,)
    sequence = f":{event.clock_sequence}" if event.clock_sequence else ""
    return (f"{event.source}:{event.kind}{sequence}",)


def _freshness(event: NormalizedEvent | None, now: float) -> float | None:
    return max(0.0, now - event.decision_timestamp) if event is not None else None


def _reason(
    axis: str,
    code: str,
    text: str,
    evidence: tuple[str, ...],
    confidence: Confidence,
    complete: bool,
    freshness: float | None,
    provenance: Provenance,
) -> AxisReason:
    return AxisReason(
        REASON_SCHEMA_VERSION,
        axis,
        code,
        text,
        evidence[:8],
        confidence,
        complete,
        freshness,
        provenance,
    )


def derive_axis_reasons(
    state: SessionHealth,
    events: list[NormalizedEvent],
    now: float,
) -> tuple[AxisReason, ...]:
    """Explain already-derived axes without deriving a second set of states."""

    reasons: list[AxisReason] = []
    lifecycle_event = _latest_event(events, set(EVENT_AXIS))
    lifecycle_code, lifecycle_text = LIFECYCLE_REASON[state.lifecycle]
    reasons.append(
        _reason(
            "lifecycle",
            lifecycle_code,
            lifecycle_text,
            _evidence_ref(lifecycle_event, "state-machine:lifecycle-baseline"),
            state.lifecycle_confidence,
            state.completeness.lifecycle.complete,
            _freshness(lifecycle_event, now),
            state.lifecycle_provenance,
        )
    )
    if state.attention != AttentionState.NONE:
        event = _latest_event(events, {"ACTION_REQUIRED"})
        code, text = ATTENTION_REASON[state.attention]
        reasons.append(
            _reason(
                "attention",
                code,
                text,
                _evidence_ref(event, "state-machine:attention-baseline"),
                state.attention_confidence,
                state.completeness.attention.complete,
                _freshness(event, now),
                state.attention_provenance,
            )
        )
    if state.current_failure is not None or state.lifecycle == LifecycleState.FAILED:
        event = _latest_event(events, {"TURN_FAILED", "COMPACT_FAILED"})
        reasons.append(
            _reason(
                "failure_recovery",
                "failure.current_turn",
                "当前 turn 有未被更新基线清除的失败",
                _evidence_ref(event, "state-machine:failure-baseline"),
                event.confidence if event else Confidence.MEDIUM,
                state.completeness.failure_recovery.complete,
                _freshness(event, now),
                event.provenance if event else Provenance("state-machine", Confidence.MEDIUM),
            )
        )
    if state.recovery != RecoveryState.NONE:
        event = _latest_event(events, {"RECONNECTING", "TRANSPORT_FALLBACK", "RECOVERED"})
        code = {
            RecoveryState.SUSPECT: "recovery.transport_suspect",
            RecoveryState.RECONNECTING: "recovery.reconnecting",
            RecoveryState.TRANSPORT_FALLBACK: "recovery.transport_fallback",
            RecoveryState.RECOVERED: "recovery.progress_after_reconnect",
        }[state.recovery]
        reasons.append(
            _reason(
                "failure_recovery",
                code,
                "恢复轴由最新恢复事件或支持性网络证据确定",
                _evidence_ref(event, f"network:{state.network.state.value}"),
                event.confidence if event else Confidence.MEDIUM,
                state.completeness.failure_recovery.complete,
                _freshness(event, now),
                event.provenance if event else Provenance("network", Confidence.MEDIUM, True),
            )
        )
    if state.silence.state == SilenceState.STALL_SUSPECT:
        reasons.append(
            _reason(
                "silence",
                "silence.no_progress_with_live_process",
                state.silence.reason,
                ("observation:quiet_full_samples", "process:liveness"),
                state.silence.provenance.confidence,
                state.completeness.silence.complete,
                (
                    max(0.0, now - state.silence.evidence_at)
                    if state.silence.evidence_at is not None
                    else None
                ),
                state.silence.provenance,
            )
        )
    elif state.silence.state == SilenceState.OBSERVER_BLIND:
        reasons.append(
            _reason(
                "silence",
                "silence.observer_blind",
                state.silence.reason,
                ("collector-health:stale",),
                state.silence.provenance.confidence,
                False,
                None,
                state.silence.provenance,
            )
        )
    if state.network.state == NetworkState.STALLED:
        reasons.append(
            _reason(
                "network",
                "network.two_window_no_progress",
                state.network.reason,
                ("socket:two_consecutive_suspect_windows",),
                Confidence.MEDIUM,
                state.completeness.network.complete,
                None,
                Provenance("network", Confidence.MEDIUM, True),
            )
        )
    if state.protocol_uncertain:
        event = _latest_event(events, {"UNPARSED_PAYLOAD"})
        reasons.append(
            _reason(
                "protocol",
                "unknown.unsupported_protocol_family",
                state.protocol_uncertainty_reason,
                _evidence_ref(event, "rollout:unknown-family"),
                Confidence.LOW,
                False,
                _freshness(event, now),
                event.provenance if event else Provenance("rollout", Confidence.LOW, complete=False),
            )
        )
    association = state.terminal_association
    if association.ambiguous or association.conflicting or association.unresolved:
        reasons.append(
            _reason(
                "terminal_ownership",
                "ownership.terminal_conflict",
                "Terminal 关联存在 ambiguous、conflicting 或 unresolved 证据",
                tuple(f"terminal:{name}={value}" for name, value in association.reasons),
                Confidence.LOW,
                state.completeness.terminal_ownership.complete,
                None,
                Provenance("terminal", Confidence.LOW, True, complete=False),
            )
        )
    return tuple(reasons[:MAX_AXIS_REASONS])


def evidence_timeline(
    events: list[NormalizedEvent],
    state: SessionHealth,
) -> tuple[EvidenceTimelineEntry, ...]:
    consequences = {
        "lifecycle": state.lifecycle.value,
        "attention": state.attention.value,
        "failure_recovery": (
            "FAILED" if state.current_failure is not None else state.recovery.value
        ),
        "protocol": "UNCERTAIN" if state.protocol_uncertain else "KNOWN",
    }
    entries: list[EvidenceTimelineEntry] = []
    for event in events:
        axis = EVENT_AXIS.get(event.kind)
        code = EVENT_REASON.get(event.kind)
        if axis is None or code is None:
            continue
        entries.append(
            EvidenceTimelineEntry(
                REASON_SCHEMA_VERSION,
                event.presentation_timestamp,
                event.source,
                event.kind,
                axis,
                code,
                f"{axis}={consequences[axis]}",
                _evidence_ref(event, "")[0],
                event.confidence,
                event.complete,
            )
        )
    return tuple(entries[-MAX_EVIDENCE_TIMELINE:])


def reason_diagnosis(reasons: tuple[AxisReason, ...]) -> list[DiagnosisFinding]:
    return [
        DiagnosisFinding(
            "warning" if reason.confidence != Confidence.HIGH or not reason.complete else "info",
            reason.reason_text,
            reason.reason_code,
            reason.supporting_evidence,
            reason.provenance,
            reason.freshness_seconds,
        )
        for reason in reasons
        if reason.reason_code.startswith(CRITICAL_REASON_PREFIXES)
    ]

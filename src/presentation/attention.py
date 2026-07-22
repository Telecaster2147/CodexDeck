"""Project current session evidence into one ordered attention queue."""

from __future__ import annotations

from collections.abc import Iterable

from models import (
    AttentionItem,
    Confidence,
    LifecycleState,
    NetworkState,
    Provenance,
    SessionHealth,
    SilenceState,
)


ACTIVE_LIFECYCLES = {
    LifecycleState.STARTING,
    LifecycleState.WAITING_RESPONSE,
    LifecycleState.GENERATING,
    LifecycleState.RUNNING_TOOL,
    LifecycleState.COMPACTING,
}

SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _item(
    session: SessionHealth,
    *,
    category: str,
    severity: str,
    summary: str,
    detail: str,
    opened_at: float | None,
    provenance: Provenance,
) -> AttentionItem:
    evidence_at = session.observation.last_evidence_at
    return AttentionItem(
        session=session.session_identity,
        workspace=session.process.cwd,
        category=category,
        severity=severity,
        summary=summary,
        detail=detail,
        opened_at=opened_at,
        last_evidence_at=evidence_at,
        source=provenance.source or session.observation.last_evidence_source,
        confidence=provenance.confidence,
        provenance=provenance,
    )


def session_attention_item(session: SessionHealth) -> AttentionItem | None:
    request = session.attention_request
    if request is not None:
        return _item(
            session,
            category=request.state.value.lower(),
            severity="critical",
            summary=request.summary or "等待用户操作",
            detail=request.detail or request.summary,
            opened_at=request.started_at,
            provenance=request.provenance,
        )
    if session.current_failure is not None:
        failure = session.current_failure
        return _item(
            session,
            category="failure",
            severity="error",
            summary="当前执行失败",
            detail=failure.message,
            opened_at=failure.timestamp,
            provenance=Provenance(failure.source, Confidence.HIGH),
        )
    if session.network.state == NetworkState.STALLED:
        return _item(
            session,
            category="network_stall",
            severity="error",
            summary="网络连接确认停顿",
            detail=session.network.reason,
            opened_at=session.observation.last_network_progress_at,
            provenance=Provenance("network-classifier", Confidence.MEDIUM, derived=True),
        )
    if session.silence.state == SilenceState.STALL_SUSPECT:
        return _item(
            session,
            category="silence_stall",
            severity="warning",
            summary="活跃会话长时间无进展",
            detail=session.silence.reason,
            opened_at=session.silence.silence_started_at,
            provenance=session.silence.provenance,
        )
    if session.alert_level == "严重":
        return _item(
            session,
            category="severe_alert",
            severity="warning",
            summary=session.alert or "严重运行告警",
            detail=session.alert_reason,
            opened_at=session.phase_since,
            provenance=Provenance("state-machine", Confidence.MEDIUM, derived=True),
        )
    if (
        session.silence.state == SilenceState.OBSERVER_BLIND
        and session.lifecycle in ACTIVE_LIFECYCLES
    ):
        return _item(
            session,
            category="observer_blind",
            severity="warning",
            summary="活跃会话存在观测盲区",
            detail=session.silence.reason,
            opened_at=session.silence.silence_started_at,
            provenance=session.silence.provenance,
        )
    return None


def attention_queue(sessions: Iterable[SessionHealth]) -> tuple[AttentionItem, ...]:
    items = [item for session in sessions if (item := session_attention_item(session))]
    return tuple(
        sorted(
            items,
            key=lambda item: (
                SEVERITY_RANK.get(item.severity, 99),
                item.opened_at if item.opened_at is not None else float("inf"),
                item.session.storage_key,
            ),
        )
    )

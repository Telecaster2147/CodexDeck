"""Versioned, redacted bounded session reports."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from models import NormalizedEvent, SessionHealth
from presentation.privacy import public_value
from utils import redact_sensitive


EXPORT_SCHEMA_VERSION = 3
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|_)(?:authorization|cookie|api_?key|secret|password|passwd|"
    r"access_token|refresh_token|auth_token|bearer_token)(?:$|_)"
)
_RECOVERY_KINDS = {"RECONNECTING", "TRANSPORT_FALLBACK", "RECOVERED"}
_ABNORMAL_KINDS = {
    "ACTION_REQUIRED",
    "OPERATION_ERROR",
    "SUBAGENT_ERROR",
    "TURN_FAILED",
    "WARNING",
    "SUSPECT",
    "RECONNECTING",
    "TRANSPORT_FALLBACK",
    "UNPARSED_PAYLOAD",
}
_AXIS_BY_EVENT = {
    "ACTION_REQUIRED": "attention",
    "ACTION_RESOLVED": "attention",
    "RECONNECTING": "recovery",
    "TRANSPORT_FALLBACK": "recovery",
    "RECOVERED": "recovery",
    "REQUEST_SENT": "lifecycle",
    "RESPONSE_STARTED": "lifecycle",
    "MODEL_PROGRESS": "lifecycle",
    "TOOL_RUNNING": "lifecycle",
    "TOOL_COMPLETED": "lifecycle",
    "COMPACTING": "lifecycle",
    "COMPACT_COMPLETED": "lifecycle",
    "TURN_COMPLETED": "lifecycle",
    "TURN_FAILED": "lifecycle",
    "TURN_ABORTED": "lifecycle",
}


def _generated_at(value: str | None) -> str:
    return value or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact(value: Any, field: str = "") -> Any:
    if _SENSITIVE_KEY.search(field):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): _redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, field) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, field) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value)
    return value


def _event_value(event: NormalizedEvent) -> dict[str, Any]:
    value = public_value(event)
    value["provenance"] = public_value(event.provenance)
    return value


def incident_summary(
    session: SessionHealth,
    retained_events: Sequence[NormalizedEvent],
) -> dict[str, Any]:
    """Project a bounded, transcript-free explanation of a session incident."""

    events = sorted(retained_events, key=lambda event: event.timestamp)
    abnormal = [event for event in events if event.kind.upper() in _ABNORMAL_KINDS]
    reliable = [event for event in events if event.provenance.complete and event.provenance.source]
    last_problem_at = abnormal[-1].timestamp if abnormal else None
    recovered_events = [
        event
        for event in events
        if event.kind.upper() == "RECOVERED"
        and (last_problem_at is None or event.timestamp >= last_problem_at)
    ]
    recovered_at = recovered_events[-1].timestamp if recovered_events else None

    if session.attention_request is not None:
        what_happened = session.attention_request.summary or session.attention.value
    elif session.current_failure is not None:
        what_happened = session.current_failure.message or session.current_failure.category
    elif session.network.reason:
        what_happened = session.network.reason
    elif session.alert:
        what_happened = session.alert
    elif abnormal:
        what_happened = abnormal[-1].summary or abnormal[-1].kind
    else:
        what_happened = session.phase

    last_reliable = reliable[-1] if reliable else None
    blind_spots: list[str] = []
    if session.observation.collector_stale:
        blind_spots.append(
            session.observation.collector_stale_reason or "collector evidence is stale"
        )
    if session.silence.state.value == "OBSERVER_BLIND":
        blind_spots.append(session.silence.reason or "observer blind spot")
    if any(not finding.provenance.complete for finding in session.diagnosis):
        blind_spots.append("one or more diagnosis findings use incomplete evidence")

    axis_changes = [
        {
            "timestamp": event.presentation_timestamp,
            "adjudicated_at": event.decision_timestamp,
            "axis": _AXIS_BY_EVENT[event.kind.upper()],
            "event": event.kind,
            "summary": event.summary,
            "source": event.source,
        }
        for event in events
        if event.kind.upper() in _AXIS_BY_EVENT
    ][-32:]

    return {
        "what_happened": what_happened,
        "first_abnormal_at": abnormal[0].presentation_timestamp if abnormal else None,
        "last_reliable_evidence": (
            {
                "timestamp": last_reliable.presentation_timestamp,
                "adjudicated_at": last_reliable.decision_timestamp,
                "event": last_reliable.kind,
                "summary": last_reliable.summary,
                "source": last_reliable.source,
                "confidence": last_reliable.provenance.confidence,
                "complete": last_reliable.provenance.complete,
            }
            if last_reliable is not None
            else None
        ),
        "recovered": recovered_at is not None,
        "recovered_at": recovered_at,
        "requires_interaction": session.attention_request is not None,
        "current_axes": {
            "lifecycle": session.lifecycle,
            "recovery": session.recovery,
            "attention": session.attention,
            "network": session.network.state,
            "silence": session.silence.state,
        },
        "axis_changes": axis_changes,
        "blind_spots": blind_spots,
    }


def session_export(
    session: SessionHealth,
    retained_events: Sequence[NormalizedEvent],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a bounded current report from the state machine's retained events.

    ``retained_events`` is intentionally required: using ``session.events`` here
    would silently export only the UI lookback window.
    """

    events = list(retained_events)
    failures = [event.failure for event in events if event.failure is not None]
    recovery = [event for event in events if event.kind in _RECOVERY_KINDS]
    payload = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "export_type": "bounded_session_report",
        "generated_at": _generated_at(generated_at),
        "incident_summary": incident_summary(session, events),
        "session": {
            "key": session.key,
            "instance_id": session.instance_id,
            "session_id": session.session_id,
            "lifecycle": session.lifecycle,
            "recovery": session.recovery,
            "attention": session.attention,
            "attention_request": session.attention_request,
            "current_operation": session.current_operation,
            "diagnosis": session.diagnosis,
            "observation": session.observation,
            "silence": session.silence,
            "phase": session.phase,
            "phase_since": session.phase_since,
            "process": session.process,
            "alerts": session.alerts,
            "current_failure": session.current_failure,
            "latest_failure": session.latest_failure,
            "token_usage": session.token_usage,
            "cumulative_token_usage": session.cumulative_token_usage,
            "rate_limits": session.rate_limits,
            "protocol_capabilities": session.protocol_capabilities,
        },
        "turns": session.turns,
        "compactions": session.compactions,
        "tool_executions": session.tool_executions,
        "agents": session.agents,
        "retry_recovery": [_event_value(event) for event in recovery],
        "failures": failures,
        "tcp_evidence": session.network,
        "events": [_event_value(event) for event in events],
        "retention": {
            "event_count": len(events),
            "complete_within_retention_limit": True,
            "source": "SessionStateMachine.retained_events",
        },
    }
    return _redact(public_value(payload))


def render_export_json(payload: dict[str, Any], *, pretty: bool = True) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )

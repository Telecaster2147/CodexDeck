"""Versioned, redacted incident and session-review exports."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from models import AlertStatus, NormalizedEvent, SessionHealth, json_value
from utils import redact_sensitive, strip_transcript_bodies


EXPORT_SCHEMA_VERSION = 1
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|_)(?:authorization|cookie|api_?key|secret|password|passwd|"
    r"access_token|refresh_token|auth_token|bearer_token)(?:$|_)"
)
_RECOVERY_KINDS = {"RECONNECTING", "TRANSPORT_FALLBACK", "RECOVERED"}


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
    value = json_value(event)
    value["provenance"] = json_value(event.provenance)
    return value


def session_export(
    session: SessionHealth,
    retained_events: Sequence[NormalizedEvent],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a review artifact from the state machine's complete retained history.

    ``retained_events`` is intentionally required: using ``session.events`` here
    would silently export only the UI lookback window.
    """

    events = list(retained_events)
    failures = [event.failure for event in events if event.failure is not None]
    recovery = [event for event in events if event.kind in _RECOVERY_KINDS]
    payload = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "export_type": "session_review",
        "generated_at": _generated_at(generated_at),
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
    return _redact(strip_transcript_bodies(json_value(payload)))


def current_incidents_export(
    sessions: Iterable[SessionHealth],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a compact inventory of unresolved incidents across sessions."""

    incidents: list[dict[str, Any]] = []
    for session in sessions:
        active_alerts = [
            item for item in session.alerts if item.status != AlertStatus.RESOLVED
        ]
        if (
            not active_alerts
            and session.current_failure is None
            and session.attention_request is None
            and not session.alert
            and session.network.state.value not in {"SUSPECT", "STALLED"}
            and session.silence.state.value
            not in {"STALL_SUSPECT", "OBSERVER_BLIND"}
        ):
            continue
        incidents.append(
            {
                "session_key": session.key,
                "instance_id": session.instance_id,
                "session_id": session.session_id,
                "lifecycle": session.lifecycle,
                "recovery": session.recovery,
                "attention": session.attention,
                "attention_request": session.attention_request,
                "current_operation": session.current_operation,
                "observation": session.observation,
                "silence": session.silence,
                "network": session.network,
                "alerts": active_alerts,
                "current_failure": session.current_failure,
            }
        )
    payload = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "export_type": "current_incidents",
        "generated_at": _generated_at(generated_at),
        "incident_count": len(incidents),
        "incidents": incidents,
    }
    return _redact(strip_transcript_bodies(json_value(payload)))


def render_export_json(payload: dict[str, Any], *, pretty: bool = True) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )

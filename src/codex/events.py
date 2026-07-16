"""Normalize stable rollout protocol events and optional diagnostic log evidence."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from config import EVENT_LABELS
from models import Confidence, FailureInfo, NormalizedEvent
from utils import message_text, one_line, redact_sensitive
from .state_store import LogRecord


NON_TURN_FAILURES = {"active_turn_not_steerable", "thread_rollback_failed"}


def parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _event(
    timestamp: float,
    kind: str,
    detail: str = "",
    *,
    source: str,
    source_id: str,
    turn_id: str = "",
    confidence: Confidence = Confidence.HIGH,
    failure: FailureInfo | None = None,
    derived: bool = False,
    complete: bool = True,
    metadata: dict[str, Any] | None = None,
) -> NormalizedEvent:
    cleaned = redact_sensitive(detail)
    if kind != "TURN_FAILED" and len(cleaned) > 480:
        cleaned = cleaned[:479] + "…"
    return NormalizedEvent(
        timestamp=timestamp,
        kind=kind,
        summary=EVENT_LABELS.get(kind, kind),
        detail=cleaned,
        source=source,
        confidence=confidence,
        turn_id=turn_id,
        source_id=source_id,
        failure=failure,
        derived=derived,
        complete=complete,
        metadata=metadata or {},
    )


def _number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timing(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source, target in (
        ("started_at", "started_at"),
        ("completed_at", "completed_at"),
        ("start_time", "started_at"),
        ("end_time", "completed_at"),
    ):
        if source in payload:
            metadata[target] = parse_timestamp(payload[source])
    for source, target in (
        ("started_at_ms", "started_at"),
        ("completed_at_ms", "completed_at"),
    ):
        value = _number(payload.get(source))
        if value is not None:
            metadata[target] = float(value) / 1000.0
    duration = _number(payload.get("duration_ms"))
    if duration is not None:
        metadata["duration_seconds"] = float(duration) / 1000.0
    ttft = _number(payload.get("time_to_first_token_ms"))
    if ttft is not None:
        metadata["time_to_first_token_seconds"] = float(ttft) / 1000.0
    return metadata


def _token_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    info = payload.get("info")
    info = info if isinstance(info, dict) else payload
    total = info.get("total_token_usage") or info.get("total_usage") or {}
    last = info.get("last_token_usage") or info.get("last_usage") or {}
    total = total if isinstance(total, dict) else {}
    last = last if isinstance(last, dict) else {}
    context_window = info.get("model_context_window") or info.get("context_window")
    context_tokens = info.get("context_tokens") or last.get("input_tokens")
    metadata = {
        "total_usage": total,
        "last_usage": last,
        "context_window": context_window,
        "context_tokens": context_tokens,
    }
    rate_limits = payload.get("rate_limits") or info.get("rate_limits")
    if isinstance(rate_limits, dict):
        metadata["rate_limits"] = rate_limits
    return metadata


def _tool_metadata(payload: dict[str, Any], item_type: str) -> dict[str, Any]:
    item = payload.get("item")
    item = item if isinstance(item, dict) else {}
    metadata = _timing(payload)
    call_id = payload.get("call_id") or payload.get("id") or item.get("id") or item.get("call_id")
    metadata.update(
        {
            "call_id": str(call_id or ""),
            "category": str(payload.get("tool_type") or item.get("type") or item_type),
            "display_name": str(
                payload.get("name")
                or payload.get("command")
                or item.get("name")
                or item.get("command")
                or item_type
            ),
        }
    )
    exit_code = _number(payload.get("exit_code"))
    if exit_code is not None:
        metadata["exit_code"] = int(exit_code)
    status = payload.get("status") or payload.get("completion_status")
    if status is not None:
        metadata["completion_status"] = str(status)
    return metadata


def _collab_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status")
    status = status.get("status") if isinstance(status, dict) else status
    receivers = payload.get("receiver_thread_ids") or payload.get("new_thread_ids") or []
    if not isinstance(receivers, list):
        receivers = [receivers]
    receiver = (
        payload.get("receiver_thread_id")
        or payload.get("new_thread_id")
        or payload.get("agent_thread_id")
    )
    if receiver:
        receivers.append(receiver)
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or json.dumps(
            error, ensure_ascii=False, separators=(",", ":")
        )
    return {
        "sender_thread_id": str(payload.get("sender_thread_id") or ""),
        "receiver_thread_ids": [str(item) for item in receivers if item],
        "agent_path": str(payload.get("agent_path") or ""),
        "nickname": str(payload.get("agent_nickname") or payload.get("nickname") or ""),
        "role": str(payload.get("agent_role") or payload.get("role") or ""),
        "model": str(payload.get("model") or ""),
        "reasoning_effort": str(payload.get("reasoning_effort") or ""),
        "status": str(status or ""),
        "error": redact_sensitive(str(error or "")),
        **_timing(payload),
    }


def _subagent_activity(
    timestamp: float,
    payload: dict[str, Any],
    source_id: str,
    turn_id: str,
) -> NormalizedEvent:
    activity = str(payload.get("kind") or payload.get("status") or "unknown").lower()
    action = {
        "started": "AGENT_SPAWNED",
        "spawned": "AGENT_SPAWNED",
        "interacted": "AGENT_INTERACTION_COMPLETED",
        "waiting": "AGENT_WAIT_STARTED",
        "waited": "AGENT_WAIT_COMPLETED",
        "resumed": "AGENT_RESUMED",
        "closed": "AGENT_CLOSED",
        "shutdown": "AGENT_CLOSED",
    }.get(activity, "AGENT_STATUS")
    occurred_at = _number(payload.get("occurred_at_ms"))
    if occurred_at is not None:
        timestamp = float(occurred_at) / 1000.0
    metadata = _collab_metadata(payload)
    if action == "AGENT_STATUS":
        metadata["status"] = activity
    return _event(
        timestamp,
        action,
        metadata.get("agent_path") or activity,
        source="rollout",
        source_id=source_id,
        turn_id=turn_id,
        metadata=metadata,
    )


def _error_info(payload: dict[str, Any]) -> tuple[str, str, str]:
    info = payload.get("codex_error_info")
    if isinstance(info, str):
        category = info
    elif isinstance(info, dict) and info:
        category = str(next(iter(info)))
    else:
        category = "other"
    return (
        category,
        redact_sensitive(str(payload.get("message") or "未知错误")),
        redact_sensitive(str(payload.get("additional_details") or "")),
    )


def _failure(timestamp: float, payload: dict[str, Any], turn_id: str, source: str) -> FailureInfo:
    category, message, details = _error_info(payload)
    return FailureInfo(category, message, details, turn_id, timestamp, source)


def normalize_rollout_record(record: dict[str, Any], source_id: str) -> list[NormalizedEvent]:
    timestamp = parse_timestamp(record.get("timestamp"))
    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    item_type = str(payload.get("type") or "")
    turn_id = str(payload.get("turn_id") or "")

    if record_type == "event_msg":
        if item_type in {"task_started", "turn_started"}:
            metadata = _timing(payload)
            metadata.update(
                {
                    "trace_id": str(payload.get("trace_id") or ""),
                    "model": str(payload.get("model") or ""),
                    "reasoning_effort": str(payload.get("reasoning_effort") or ""),
                    "collaboration_mode": str(payload.get("collaboration_mode") or ""),
                    "context_window": payload.get("model_context_window")
                    or payload.get("context_window"),
                }
            )
            return [
                _event(
                    timestamp,
                    "TURN_STARTED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type in {"task_complete", "turn_complete"}:
            error = payload.get("error")
            if isinstance(error, dict):
                failure = _failure(timestamp, error, turn_id, "rollout")
                return [
                    _event(
                        timestamp,
                        "TURN_FAILED",
                        failure.message,
                        source="rollout",
                        source_id=source_id,
                        turn_id=turn_id,
                        failure=failure,
                        metadata=_timing(payload),
                    )
                ]
            return [
                _event(
                    timestamp,
                    "TURN_COMPLETED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=_timing(payload),
                )
            ]
        if item_type in {"turn_aborted", "task_aborted"}:
            return [
                _event(
                    timestamp,
                    "TURN_ABORTED",
                    str(payload.get("reason") or ""),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "stream_error":
            category, message, details = _error_info(payload)
            detail = message if not details else f"{message} | {details}"
            failure = FailureInfo(
                category,
                message,
                details,
                turn_id,
                timestamp,
                "rollout",
            )
            return [
                _event(
                    timestamp,
                    "RECONNECTING",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    failure=failure,
                )
            ]
        if item_type == "error":
            failure = _failure(timestamp, payload, turn_id, "rollout")
            kind = "OPERATION_ERROR" if failure.category in NON_TURN_FAILURES else "TURN_FAILED"
            return [
                _event(
                    timestamp,
                    kind,
                    failure.message,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    failure=failure,
                )
            ]
        if item_type in {"warning", "guardian_warning"}:
            return [
                _event(
                    timestamp,
                    "WARNING",
                    str(payload.get("message") or ""),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "context_compacted":
            return [
                _event(
                    timestamp,
                    "COMPACT_COMPLETED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "token_count":
            return [
                _event(
                    timestamp,
                    "TOKEN_USAGE",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=_token_metadata(payload),
                )
            ]
        if item_type in {"rate_limit", "rate_limit_snapshot"}:
            metadata = {
                "rate_limits": payload.get("rate_limits")
                or payload.get("snapshot")
                or payload
            }
            return [
                _event(
                    timestamp,
                    "RATE_LIMIT",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type in {"item_started", "item_completed"}:
            item = payload.get("item")
            item = item if isinstance(item, dict) else {}
            metadata = _tool_metadata(payload, str(item.get("type") or "item"))
            metadata["item_id"] = str(payload.get("item_id") or item.get("id") or "")
            return [
                _event(
                    timestamp,
                    "ITEM_STARTED" if item_type == "item_started" else "ITEM_COMPLETED",
                    metadata["display_name"],
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type in {
            "exec_command_begin",
            "mcp_tool_call_begin",
            "dynamic_tool_call_begin",
        }:
            metadata = _tool_metadata(payload, item_type)
            return [
                _event(
                    timestamp,
                    "TOOL_RUNNING",
                    metadata["display_name"],
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type in {
            "exec_command_end",
            "mcp_tool_call_end",
            "dynamic_tool_call_end",
        }:
            metadata = _tool_metadata(payload, item_type)
            return [
                _event(
                    timestamp,
                    "TOOL_COMPLETED",
                    metadata["display_name"],
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        collab_actions = {
            "collab_agent_spawn_begin": "AGENT_SPAWN_STARTED",
            "collab_agent_spawn_end": "AGENT_SPAWNED",
            "collab_agent_interaction_begin": "AGENT_INTERACTION_STARTED",
            "collab_agent_interaction_end": "AGENT_INTERACTION_COMPLETED",
            "collab_waiting_begin": "AGENT_WAIT_STARTED",
            "collab_waiting_end": "AGENT_WAIT_COMPLETED",
            "collab_agent_resume_begin": "AGENT_RESUME_STARTED",
            "collab_agent_resume_end": "AGENT_RESUMED",
            "collab_agent_close_begin": "AGENT_CLOSE_STARTED",
            "collab_agent_close_end": "AGENT_CLOSED",
            "subagent_status": "AGENT_STATUS",
            "subagent_activity": "AGENT_STATUS",
        }
        if item_type == "sub_agent_activity":
            return [_subagent_activity(timestamp, payload, source_id, turn_id)]
        if item_type in collab_actions:
            metadata = _collab_metadata(payload)
            return [
                _event(
                    timestamp,
                    collab_actions[item_type],
                    metadata.get("nickname") or metadata.get("status") or item_type,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type in {"agent_message", "agent_reasoning", "reasoning_content_delta"}:
            detail = str(payload.get("message") or payload.get("text") or "")
            return [
                _event(
                    timestamp,
                    "MODEL_PROGRESS",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        return []

    if record_type == "response_item":
        if item_type == "agent_message":
            return [
                _event(
                    timestamp,
                    "MODEL_PROGRESS",
                    message_text(payload),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "reasoning":
            return [
                _event(
                    timestamp,
                    "MODEL_PROGRESS",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "message" and payload.get("role") == "assistant":
            return [
                _event(
                    timestamp,
                    "MODEL_PROGRESS",
                    message_text(payload),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type in {"custom_tool_call", "function_call", "local_shell_call"}:
            metadata = _tool_metadata(payload, item_type)
            return [
                _event(
                    timestamp,
                    "TOOL_RUNNING",
                    metadata["display_name"],
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    derived=True,
                    complete=False,
                    metadata=metadata,
                )
            ]
        if item_type in {
            "custom_tool_call_output",
            "function_call_output",
            "local_shell_call_output",
        }:
            metadata = _tool_metadata(payload, item_type)
            return [
                _event(
                    timestamp,
                    "TOOL_COMPLETED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    derived=True,
                    complete=False,
                    metadata=metadata,
                )
            ]
    if record_type in {"compacted", "context_compacted"}:
        return [
            _event(
                timestamp,
                "COMPACT_COMPLETED",
                source="rollout",
                source_id=source_id,
                turn_id=turn_id,
            )
        ]
    return []


def normalize_log(record: LogRecord) -> list[NormalizedEvent]:
    body = record.body
    lowered = body.lower()
    source_id = f"log:{record.log_id}"
    turn_id = ""
    if record.target == "codex_core::responses_retry":
        if "falling back from websockets to https" in lowered:
            return [
                _event(
                    record.timestamp,
                    "TRANSPORT_FALLBACK",
                    one_line(body),
                    source="log",
                    source_id=source_id,
                    turn_id=turn_id,
                    confidence=Confidence.MEDIUM,
                )
            ]
        if "stream disconnected" in lowered or "idle timeout waiting for sse" in lowered:
            return [
                _event(
                    record.timestamp,
                    "RECONNECTING",
                    one_line(body),
                    source="log",
                    source_id=source_id,
                    turn_id=turn_id,
                    confidence=Confidence.MEDIUM,
                )
            ]
    if (
        record.target == "codex_http_client::transport"
        and " post to " in lowered
        and "/responses" in lowered
    ):
        kind = (
            "COMPACTING"
            if "run_auto_compact{" in body or "run_remote_compact" in lowered
            else "REQUEST_SENT"
        )
        return [
            _event(
                record.timestamp,
                kind,
                source="log",
                source_id=source_id,
                turn_id=turn_id,
                confidence=Confidence.MEDIUM,
            )
        ]
    if record.target == "codex_api::sse::responses" and "sse event: " in lowered:
        encoded = body.split("SSE event: ", 1)[1]
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            match = re.search(r'"type"\s*:\s*"([^"]+)"', encoded)
            payload = {"type": match.group(1) if match else ""}
        event_type = str(payload.get("type") or "")
        mapping = {
            "keepalive": "KEEPALIVE",
            "response.created": "RESPONSE_STARTED",
            "response.completed": "MODEL_PROGRESS",
            "response.failed": "TURN_FAILED",
            "response.incomplete": "TURN_FAILED",
        }
        kind = mapping.get(event_type)
        if kind:
            failure = None
            detail = event_type
            if kind == "TURN_FAILED":
                response = payload.get("response")
                response = response if isinstance(response, dict) else {}
                error = response.get("error")
                error = error if isinstance(error, dict) else {}
                message = redact_sensitive(
                    str(error.get("message") or response.get("status_details") or event_type)
                )
                category = str(error.get("code") or event_type.replace(".", "_"))
                failure = FailureInfo(
                    category, message, "", turn_id, record.timestamp, "sse"
                )
                detail = message
            return [
                _event(
                    record.timestamp,
                    kind,
                    detail,
                    source="sse",
                    source_id=source_id,
                    turn_id=turn_id,
                    confidence=Confidence.MEDIUM,
                    failure=failure,
                )
            ]
    return []

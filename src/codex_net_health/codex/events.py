"""Normalize stable rollout protocol events and optional diagnostic log evidence."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..config import EVENT_LABELS
from ..models import Confidence, FailureInfo, NormalizedEvent
from ..utils import message_text, one_line, redact_sensitive
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
            return [
                _event(
                    timestamp,
                    "TURN_STARTED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
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
                    )
                ]
            return [
                _event(
                    timestamp,
                    "TURN_COMPLETED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
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
            info = payload.get("info")
            detail = json.dumps(info, ensure_ascii=False, separators=(",", ":")) if info else ""
            return [
                _event(
                    timestamp,
                    "TOKEN_USAGE",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
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
            return [
                _event(
                    timestamp,
                    "TOOL_RUNNING",
                    str(payload.get("name") or item_type),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type in {
            "custom_tool_call_output",
            "function_call_output",
            "local_shell_call_output",
        }:
            return [
                _event(
                    timestamp,
                    "TOOL_COMPLETED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
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

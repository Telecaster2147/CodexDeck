"""Normalize stable rollout protocol events and optional diagnostic log evidence."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from typing import Any

from config import EVENT_LABELS
from models import Confidence, FailureInfo, NormalizedEvent, UnparsedPayload
from utils import message_text, one_line, redact_sensitive
from .state_store import LogRecord


NON_TURN_FAILURES = {"active_turn_not_steerable", "thread_rollback_failed"}
COMPACT_COMMAND = re.compile(r"^/compact(?:\s+.*)?$", re.IGNORECASE)


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
    unparsed: UnparsedPayload | None = None,
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
        unparsed=unparsed,
    )


def _number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_compact_command(payload: dict[str, Any]) -> bool:
    """Return whether a user-message payload is an explicit compact command."""

    text = str(payload.get("message") or message_text(payload) or "").strip()
    return bool(COMPACT_COMMAND.fullmatch(text))


def _attention_event(
    timestamp: float,
    payload: dict[str, Any],
    source_id: str,
    turn_id: str,
    state: str,
) -> NormalizedEvent:
    questions = payload.get("questions")
    question = questions[0] if isinstance(questions, list) and questions else {}
    question = question if isinstance(question, dict) else {}
    request = payload.get("request")
    request = request if isinstance(request, dict) else {}
    command = payload.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    message = str(
        payload.get("reason")
        or question.get("question")
        or request.get("message")
        or payload.get("message")
        or command
        or ""
    )
    request_id = str(payload.get("approval_id") or payload.get("id") or "")
    call_id = str(payload.get("call_id") or "")
    metadata = {
        "attention_state": state,
        "request_id": request_id,
        "call_id": call_id,
        "command": redact_sensitive(str(command or "")),
        "cwd": redact_sensitive(str(payload.get("cwd") or "")),
        "server_name": str(payload.get("server_name") or ""),
        "question_count": len(questions) if isinstance(questions, list) else 0,
        "mode": str(request.get("mode") or payload.get("mode") or ""),
    }
    return _event(
        timestamp,
        "ACTION_REQUIRED",
        message,
        source="rollout",
        source_id=source_id,
        turn_id=turn_id,
        metadata=metadata,
    )


def _unparsed_event(
    timestamp: float,
    record_type: str,
    item_type: str,
    payload: dict[str, Any],
    source_id: str,
    turn_id: str,
) -> NormalizedEvent:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    cleaned = redact_sensitive(serialized)
    limit = 240
    unparsed = UnparsedPayload(
        f"{record_type}:{item_type or 'unknown'}",
        len(serialized),
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        cleaned[:limit],
        len(cleaned) > limit,
    )
    return _event(
        timestamp,
        "UNPARSED_PAYLOAD",
        f"{unparsed.source_type} · {unparsed.length} chars · {unparsed.sha256[:10]}",
        source="rollout",
        source_id=source_id,
        turn_id=turn_id,
        confidence=Confidence.LOW,
        complete=False,
        unparsed=unparsed,
    )
def _structured_text(value: object) -> str:
    """Extract human-readable text from current reasoning/message protocol shapes."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_structured_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "summary_text", "content", "message"):
            text = _structured_text(value.get(key))
            if text:
                return text
    return ""


def _bounded_metadata_text(value: object, limit: int = 1600) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    text = redact_sensitive(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _patch_files(value: object) -> list[str]:
    text = _bounded_metadata_text(value, 128 * 1024)
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    files = re.findall(
        r"(?m)^\*\*\* (?:Add|Update|Delete|Move) File:\s*(.+?)\s*$",
        text,
    )
    return list(dict.fromkeys(item.strip() for item in files if item.strip()))


def _nested_exec_input(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        return "", ""
    commands: list[str] = []
    workdirs: list[str] = []
    for match in re.finditer(r"tools\.exec_command\(\s*", value):
        try:
            arguments, _ = json.JSONDecoder().raw_decode(value[match.end() :])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(arguments, dict):
            continue
        command = _bounded_metadata_text(arguments.get("cmd"), 1200)
        workdir = _bounded_metadata_text(arguments.get("workdir"), 320)
        if command:
            commands.append(command)
        if workdir:
            workdirs.append(workdir)
        if len(commands) >= 6:
            break
    unique_workdirs = list(dict.fromkeys(workdirs))
    return (
        "\n".join(commands),
        unique_workdirs[0] if len(unique_workdirs) == 1 else "",
    )


def _nested_tool_names(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return list(dict.fromkeys(re.findall(r"\btools\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", value)))


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


def _background_tool_output(value: object) -> tuple[str, dict[str, Any]]:
    output = _bounded_metadata_text(value)
    match = re.fullmatch(
        r"Script running with cell ID (?P<cell_id>\S+)\s*\n"
        r"Wall time (?P<waited>\d+(?:\.\d+)?) seconds\s*\n"
        r"Output:\s*(?P<output>.*)",
        output,
        flags=re.DOTALL,
    )
    if not match:
        return output, {}
    actual_output = match.group("output").strip()
    return actual_output, {
        "background_running": True,
        "background_cell_id": match.group("cell_id"),
        "background_wait_seconds": float(match.group("waited")),
        "background_output_empty": not actual_output,
    }


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
    input_value = (
        payload.get("input")
        or payload.get("arguments")
        or item.get("input")
        or item.get("arguments")
    )
    output_value = (
        payload.get("output")
        or payload.get("result")
        or item.get("output")
        or item.get("result")
    )
    command_value = payload.get("command") or item.get("command")
    command = _bounded_metadata_text(command_value, 1200)
    cwd = _bounded_metadata_text(payload.get("cwd") or item.get("cwd"), 320)
    nested_command, nested_cwd = _nested_exec_input(input_value)
    command = command or nested_command
    cwd = cwd or nested_cwd
    files = _patch_files(input_value)
    output, background = _background_tool_output(output_value)
    metadata = {**_timing(payload), **background}
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
            "command": command,
            "cwd": cwd,
            "arguments": _bounded_metadata_text(input_value),
            "output": output,
            "files": files,
            "nested_tools": _nested_tool_names(input_value),
            "server": _bounded_metadata_text(payload.get("server") or item.get("server"), 160),
            "tool": _bounded_metadata_text(payload.get("tool") or item.get("tool"), 160),
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


def _model_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    collaboration = payload.get("collaboration_mode")
    collaboration = collaboration if isinstance(collaboration, dict) else {}
    settings = collaboration.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    return {
        "model": str(payload.get("model") or settings.get("model") or ""),
        "reasoning_effort": str(
            payload.get("reasoning_effort") or settings.get("reasoning_effort") or ""
        ),
    }


def _compaction_events(
    timestamp: float,
    payload: dict[str, Any],
    *,
    source_id: str,
    turn_id: str,
    inferred_manual_compact: bool,
    context_tokens: int | None,
    context_window: int | None,
    compact_started_at: float | None,
    compact_started_source_id: str,
    compact_started_turn_id: str,
) -> list[NormalizedEvent]:
    metadata: dict[str, Any] = {"window_number": payload.get("window_number")}
    if inferred_manual_compact:
        metadata["trigger"] = "manual"
    if context_tokens is not None:
        metadata["context_tokens"] = context_tokens
    if context_window is not None:
        metadata["context_window"] = context_window
    if context_tokens is not None and context_window:
        metadata["context_ratio"] = context_tokens / context_window

    events: list[NormalizedEvent] = []
    if inferred_manual_compact and compact_started_at is not None:
        events.append(
            _event(
                compact_started_at,
                "COMPACTING",
                "检测到手动 compact 任务",
                source="rollout",
                source_id=compact_started_source_id or f"{source_id}:manual-compact",
                turn_id=compact_started_turn_id or turn_id,
                metadata={
                    **metadata,
                    "trigger": "manual",
                    "inferred_from_compacted_record": True,
                    "reconstructed": True,
                },
            )
        )
    events.append(
        _event(
            timestamp,
            "COMPACT_COMPLETED",
            "手动 compact 已完成" if inferred_manual_compact else "",
            source="rollout",
            source_id=source_id,
            turn_id=turn_id,
            metadata=metadata,
        )
    )
    return events


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


def normalize_rollout_record(
    record: dict[str, Any],
    source_id: str,
    *,
    inferred_manual_compact: bool = False,
    context_tokens: int | None = None,
    context_window: int | None = None,
    compact_started_at: float | None = None,
    compact_started_source_id: str = "",
    compact_started_turn_id: str = "",
) -> list[NormalizedEvent]:
    timestamp = parse_timestamp(record.get("timestamp"))
    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    item_type = str(payload.get("type") or "")
    turn_id = str(payload.get("turn_id") or "")

    if record_type == "turn_context":
        metadata = _model_metadata(payload)
        if not any(metadata.values()):
            return []
        return [
            _event(
                timestamp,
                "MODEL_CONFIG",
                metadata["model"],
                source="rollout",
                source_id=source_id,
                turn_id=turn_id,
                metadata=metadata,
            )
        ]

    if record_type == "event_msg":
        attention_types = {
            "exec_approval_request": "APPROVAL",
            "apply_patch_approval_request": "APPROVAL",
            "request_permissions": "PERMISSIONS",
            "request_user_input": "USER_INPUT",
            "elicitation_request": "MCP_ELICITATION",
            "auth_elicitation_request": "AUTH_ELICITATION",
        }
        if item_type in attention_types:
            state = attention_types[item_type]
            request = payload.get("request")
            if item_type == "elicitation_request" and isinstance(request, dict):
                if str(request.get("mode") or "").lower() in {"url", "auth"}:
                    state = "AUTH_ELICITATION"
            return [_attention_event(timestamp, payload, source_id, turn_id, state)]
        if item_type in {
            "exec_approval",
            "patch_approval",
            "resolve_elicitation",
            "user_input_answer",
            "request_permissions_response",
        }:
            return [
                _event(
                    timestamp,
                    "ACTION_RESOLVED",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata={
                        "request_id": str(payload.get("approval_id") or payload.get("id") or ""),
                        "call_id": str(payload.get("call_id") or ""),
                    },
                )
            ]
        if item_type == "user_message" and is_compact_command(payload):
            metadata: dict[str, Any] = {"trigger": "manual", "explicit_command": True}
            if context_tokens is not None:
                metadata["context_tokens"] = context_tokens
            if context_window is not None:
                metadata["context_window"] = context_window
            if context_tokens is not None and context_window:
                metadata["context_ratio"] = context_tokens / context_window
            return [
                _event(
                    timestamp,
                    "COMPACT_REQUESTED",
                    "用户已发送 /compact",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
        if item_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            settings = settings if isinstance(settings, dict) else {}
            metadata = _model_metadata(settings)
            if not any(metadata.values()):
                return []
            return [
                _event(
                    timestamp,
                    "MODEL_CONFIG",
                    metadata["model"],
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
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
            return _compaction_events(
                timestamp,
                payload,
                source_id=source_id,
                turn_id=turn_id,
                inferred_manual_compact=inferred_manual_compact,
                context_tokens=context_tokens,
                context_window=context_window,
                compact_started_at=compact_started_at,
                compact_started_source_id=compact_started_source_id,
                compact_started_turn_id=compact_started_turn_id,
            )
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
            compact_item_type = str(item.get("type") or "").lower()
            if "compact" in compact_item_type:
                return [
                    _event(
                        timestamp,
                        "COMPACTING" if item_type == "item_started" else "COMPACT_PROGRESS",
                        "上下文压缩已开始" if item_type == "item_started" else "压缩步骤已返回",
                        source="rollout",
                        source_id=source_id,
                        turn_id=turn_id,
                        metadata={
                            "trigger": str(item.get("trigger") or "unknown"),
                            "item_type": compact_item_type,
                        },
                    )
                ]
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
        if item_type == "patch_apply_end":
            changes = payload.get("changes")
            changes = changes if isinstance(changes, dict) else {}
            files = [redact_sensitive(str(path)) for path in changes]
            change_types = {
                redact_sensitive(str(path)): str(change.get("type") or "update")
                for path, change in changes.items()
                if isinstance(change, dict)
            }
            success = bool(payload.get("success"))
            metadata = _tool_metadata(payload, item_type)
            metadata.update(
                {
                    "files": files,
                    "change_types": change_types,
                    "success": success,
                    "output": _bounded_metadata_text(
                        payload.get("stdout") or payload.get("stderr")
                    ),
                }
            )
            detail = f"{len(files)} 个文件"
            if files:
                detail += "：" + "，".join(files[:3])
                if len(files) > 3:
                    detail += f" 等 {len(files)} 个"
            return [
                _event(
                    timestamp,
                    "FILE_CHANGE_APPLIED" if success else "FILE_CHANGE_FAILED",
                    detail,
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
        if item_type in {"agent_reasoning", "reasoning_content_delta"}:
            detail = str(payload.get("message") or payload.get("text") or "")
            return [
                _event(
                    timestamp,
                    "REASONING_SUMMARY",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                )
            ]
        if item_type == "agent_message":
            detail = str(payload.get("message") or payload.get("text") or "")
            return [
                _event(
                    timestamp,
                    "MODEL_PROGRESS",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata={"phase": str(payload.get("phase") or "")},
                )
            ]
        if item_type in {"plan_update", "plan_updated"}:
            plan = payload.get("plan") or payload.get("items") or payload.get("steps")
            return [
                _event(
                    timestamp,
                    "PLAN_UPDATED",
                    _structured_text(plan),
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata={"plan": plan if isinstance(plan, list) else []},
                )
            ]
        if item_type in {"thread_goal_updated", "thread_rolled_back", "user_message"}:
            return []
        return [
            _unparsed_event(
                timestamp, record_type, item_type, payload, source_id, turn_id
            )
        ]

    if record_type == "response_item":
        if item_type == "message" and payload.get("role") == "user" and is_compact_command(payload):
            metadata: dict[str, Any] = {"trigger": "manual", "explicit_command": True}
            if context_tokens is not None:
                metadata["context_tokens"] = context_tokens
            if context_window is not None:
                metadata["context_window"] = context_window
            if context_tokens is not None and context_window:
                metadata["context_ratio"] = context_tokens / context_window
            return [
                _event(
                    timestamp,
                    "COMPACT_REQUESTED",
                    "用户已发送 /compact",
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata=metadata,
                )
            ]
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
            summary = _structured_text(payload.get("summary"))
            content = _structured_text(payload.get("content"))
            detail = summary or content
            return [
                _event(
                    timestamp,
                    "REASONING_SUMMARY" if detail else "MODEL_PROGRESS",
                    detail,
                    source="rollout",
                    source_id=source_id,
                    turn_id=turn_id,
                    metadata={
                        "summary_available": bool(summary),
                        "raw_available": bool(content),
                        "encrypted": bool(payload.get("encrypted_content")),
                    },
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
        if item_type == "message" and payload.get("role") == "user":
            return []
        return [
            _unparsed_event(
                timestamp, record_type, item_type, payload, source_id, turn_id
            )
        ]
    if record_type in {"compacted", "context_compacted"}:
        return _compaction_events(
            timestamp,
            payload,
            source_id=source_id,
            turn_id=turn_id,
            inferred_manual_compact=inferred_manual_compact,
            context_tokens=context_tokens,
            context_window=context_window,
            compact_started_at=compact_started_at,
            compact_started_source_id=compact_started_source_id,
            compact_started_turn_id=compact_started_turn_id,
        )
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
    if record.target == "codex_http_client::transport" and "/responses" in lowered:
        compacting = any(
            marker in lowered
            for marker in ("run_auto_compact{", "run_remote_compact", "run_pre_sampling_compact")
        )
        if not compacting and " post to " not in lowered:
            return []
        kind = "COMPACTING" if compacting else "REQUEST_SENT"
        trigger = "auto" if "run_auto_compact" in lowered else "unknown"
        return [
            _event(
                record.timestamp,
                kind,
                source="log",
                source_id=source_id,
                turn_id=turn_id,
                confidence=Confidence.MEDIUM,
                metadata={"trigger": trigger} if compacting else {},
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
        response = payload.get("response")
        response = response if isinstance(response, dict) else {}
        if event_type == "response.completed" and response.get("object") == "response.compaction":
            return [
                _event(
                    record.timestamp,
                    "COMPACT_COMPLETED",
                    source="sse",
                    source_id=source_id,
                    turn_id=turn_id,
                    confidence=Confidence.MEDIUM,
                )
            ]
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

"""Activity projection and rendering for the Textual inspector."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

from rich.table import Table
from rich.text import Text

from models import SessionHealth
from presentation.tui.theme import STATE_COLORS
from utils import operator_text


def _tool_name_is_fallback(metadata: dict[str, object]) -> bool:
    name = str(metadata.get("display_name") or "")
    return bool(
        metadata.get("display_name_is_fallback")
        or name.endswith("_output")
        or "tool_call_output" in name
    )


def event_severity(kind: str) -> tuple[str, str]:
    if kind in {
        "TURN_FAILED",
        "COMPACT_FAILED",
        "OPERATION_ERROR",
        "FILE_CHANGE_FAILED",
        "ALERT_ESCALATED",
    }:
        return "ERR", STATE_COLORS["error"]
    if kind in {
        "WARNING",
        "RECONNECTING",
        "TRANSPORT_FALLBACK",
        "TURN_ABORTED",
        "COMPACT_ABORTED",
        "ALERT_OPENED",
        "ALERT_ACKNOWLEDGED",
        "ACTION_REQUIRED",
        "COMPACT_REQUESTED",
        "UNPARSED_PAYLOAD",
    }:
        return "WARN", STATE_COLORS["warning"]
    if kind in {
        "RECOVERED",
        "TURN_COMPLETED",
        "TOOL_COMPLETED",
        "COMPACT_COMPLETED",
        "ALERT_RESOLVED",
    }:
        return "OK", STATE_COLORS["success"]
    return "INFO", STATE_COLORS["info"]


def timeline_entries(
    session: SessionHealth,
    auto_compact_token_limit: int | None = None,
) -> list[object]:
    tools = {item.call_id: item for item in session.tool_executions}
    tool_starts = {
        str(event.metadata.get("call_id")): event
        for event in session.events
        if event.kind == "TOOL_RUNNING" and event.metadata.get("call_id")
    }
    entries: list[object] = []
    for event in session.events:
        hidden = {
            "MODEL_CONFIG",
            "KEEPALIVE",
            "TOKEN_USAGE",
            "RATE_LIMIT",
            "ITEM_STARTED",
            "ITEM_COMPLETED",
            "COMPACT_CANDIDATE",
            "COMPACT_PROGRESS",
        }
        if event.kind in hidden:
            continue
        if event.kind in {
            "REASONING_SUMMARY",
            "MODEL_PROGRESS",
        }:
            continue
        call_id = str(event.metadata.get("call_id") or "")
        tool = tools.get(call_id)
        start = tool_starts.get(call_id)
        if event.kind in {"TOOL_RUNNING", "TOOL_COMPLETED"} and (tool or start):
            start_metadata = start.metadata if start else {}
            fallback_name = _tool_name_is_fallback(event.metadata)

            def resolved(name: str, summary_value: object = "") -> object:
                if summary_value not in (None, "", (), []):
                    return summary_value
                current = event.metadata.get(name)
                if name in {"category", "display_name", "tool_name"} and fallback_name:
                    current = None
                return current or start_metadata.get(name)

            metadata = {
                **event.metadata,
                "category": resolved("category", tool.category if tool else ""),
                "display_name": resolved("display_name", tool.display_name if tool else ""),
                "tool_name": resolved("tool_name", tool.tool_name if tool else ""),
                "command": resolved("command", tool.command if tool else ""),
                "cwd": resolved("cwd", tool.cwd if tool else ""),
                "arguments": resolved("arguments", tool.arguments if tool else ""),
                "output": resolved("output", tool.output if tool else ""),
                "files": resolved("files", list(tool.files) if tool else []),
                "nested_tools": resolved("nested_tools"),
            }
            detail = event.detail
            if not detail or _tool_name_is_fallback({"display_name": detail}):
                detail = str(metadata.get("display_name") or metadata.get("tool_name") or "")
            event = replace(event, detail=detail, metadata=metadata)
        if event.kind.startswith("COMPACT") and auto_compact_token_limit:
            event = replace(
                event,
                metadata={
                    **event.metadata,
                    "auto_compact_token_limit": event.metadata.get("auto_compact_token_limit")
                    or auto_compact_token_limit,
                },
            )
        entries.append(event)
    visible_compactions = {
        (event.kind, event.timestamp)
        for event in session.events
        if event.kind
        in {
            "COMPACT_REQUESTED",
            "COMPACTING",
            "COMPACT_COMPLETED",
            "COMPACT_FAILED",
            "COMPACT_ABORTED",
        }
    }
    for compact in session.compactions:
        metadata = {
            "trigger": compact.trigger,
            "context_tokens": compact.context_tokens,
            "context_tokens_after": compact.context_tokens_after,
            "context_window": compact.context_window,
            "auto_compact_token_limit": (
                compact.auto_compact_token_limit or auto_compact_token_limit
            ),
            "historical_summary": True,
            "operation_id": compact.operation_id,
            "status": compact.status,
            "source": compact.source,
            "confidence": compact.confidence.value,
            "reconstructed": compact.reconstructed,
        }
        trigger = "手动" if compact.trigger == "manual" else "自动/未知"
        if (
            compact.requested_at is not None
            and (
                "COMPACT_REQUESTED",
                compact.requested_at,
            )
            not in visible_compactions
        ):
            entries.append(
                {
                    "timestamp": compact.requested_at,
                    "kind": "COMPACT_REQUESTED",
                    "summary": "用户已请求上下文压缩",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if (
            compact.started_at is not None
            and (
                "COMPACTING",
                compact.started_at,
            )
            not in visible_compactions
        ):
            entries.append(
                {
                    "timestamp": compact.started_at,
                    "kind": "COMPACTING",
                    "summary": "上下文压缩开始",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if (
            compact.completed_at is not None
            and (
                "COMPACT_COMPLETED",
                compact.completed_at,
            )
            not in visible_compactions
        ):
            entries.append(
                {
                    "timestamp": compact.completed_at,
                    "kind": "COMPACT_COMPLETED",
                    "summary": "上下文压缩完成",
                    "detail": f"{trigger} compact · 历史摘要",
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if (
            compact.failed_at is not None
            and (
                "COMPACT_FAILED",
                compact.failed_at,
            )
            not in visible_compactions
        ):
            entries.append(
                {
                    "timestamp": compact.failed_at,
                    "kind": "COMPACT_FAILED",
                    "summary": "上下文压缩失败",
                    "detail": compact.failure.message if compact.failure else trigger,
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
        if (
            compact.aborted_at is not None
            and (
                "COMPACT_ABORTED",
                compact.aborted_at,
            )
            not in visible_compactions
        ):
            entries.append(
                {
                    "timestamp": compact.aborted_at,
                    "kind": "COMPACT_ABORTED",
                    "summary": "上下文压缩中止",
                    "detail": trigger,
                    "turn_id": compact.turn_id,
                    "metadata": metadata,
                }
            )
    failure = session.current_failure
    if failure and not any(_matches_failure(entry, failure) for entry in entries):
        entries.append(
            {
                "timestamp": failure.timestamp or time.time(),
                "kind": "TURN_FAILED",
                "summary": "模型调用失败",
                "detail": failure.message,
                "failure": failure,
                "turn_id": failure.turn_id,
            }
        )
    entries = sorted(entries, key=lambda item: float(_value(item, "timestamp", 0) or 0))
    folded: list[object] = []
    pending_tools: dict[str, object] = {}
    for entry in entries:
        kind = str(_value(entry, "kind", ""))
        metadata = _value(entry, "metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        call_id = str(metadata.get("call_id") or "")
        if kind == "TOOL_RUNNING" and call_id:
            pending_tools[call_id] = entry
            continue
        if kind == "TOOL_COMPLETED" and call_id in pending_tools:
            started = pending_tools.pop(call_id)
            duration = float(_value(entry, "timestamp", 0) or 0) - float(
                _value(started, "timestamp", 0) or 0
            )
            if hasattr(entry, "metadata"):
                entry = replace(
                    entry,
                    metadata={
                        **metadata,
                        "duration_seconds": metadata.get("duration_seconds") or max(0.0, duration),
                    },
                )
        folded.append(entry)
    folded.extend(pending_tools.values())
    entries = sorted(folded, key=lambda item: float(_value(item, "timestamp", 0) or 0))
    return entries


def _value(item: object, name: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    value = getattr(item, name, None)
    return default if value is None else value


def _matches_failure(event: object, failure: object) -> bool:
    if _value(event, "kind") != "TURN_FAILED":
        return False
    event_failure = _value(event, "failure", None)
    event_turn = str(_value(event, "turn_id", "") or _value(event_failure, "turn_id", ""))
    failure_turn = str(_value(failure, "turn_id", ""))
    if failure_turn and event_turn == failure_turn:
        return True
    return bool(
        event_failure
        and _value(event_failure, "timestamp", 0) == _value(failure, "timestamp", 0)
        and _value(event_failure, "message", "") == _value(failure, "message", "")
    )


def _timeline_signature(event: object) -> tuple[object, ...]:
    failure = _value(event, "failure", None)
    metadata = _value(event, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    return (
        _value(event, "timestamp", 0),
        _value(event, "kind", ""),
        _value(event, "summary", ""),
        _value(event, "detail", ""),
        _value(event, "turn_id", ""),
        metadata.get("command", ""),
        metadata.get("cwd", ""),
        tuple(metadata.get("files", ()) or ()),
        metadata.get("arguments", ""),
        metadata.get("output", ""),
        _value(failure, "timestamp", 0),
        _value(failure, "message", ""),
    )


def _trace_tag(kind: str, metadata: dict[str, object]) -> str:
    name = " ".join(str(metadata.get(key) or "") for key in ("tool_name", "display_name")).lower()
    category = str(metadata.get("category") or "").lower()
    nested_tools = [str(item) for item in (metadata.get("nested_tools") or [])]
    if kind == "ACTION_REQUIRED":
        return "ACTION"
    if kind == "UNPARSED_PAYLOAD":
        return "UNPARSED"
    if kind == "REASONING_SUMMARY":
        return "THINK"
    if kind == "PLAN_UPDATED" or "update_plan" in name:
        return "PLAN"
    if kind.startswith("FILE_CHANGE") or "apply_patch" in name or metadata.get("files"):
        return "WRITE"
    if kind == "COMPACTING" or kind == "COMPACT_COMPLETED":
        return "COMPACT"
    if kind in {"TOOL_RUNNING", "TOOL_COMPLETED"}:
        if "update_plan" in nested_tools and not metadata.get("command"):
            return "PLAN"
        if "mcp" in category or metadata.get("server"):
            return "MCP"
        if metadata.get("command") or "shell" in category or name == "exec":
            return "CMD"
        if "web" in category or "search" in name:
            return "WEB"
        return "TOOL"
    if kind.startswith("AGENT_"):
        return "AGENT"
    if kind == "MODEL_PROGRESS":
        return "MODEL"
    return "EVENT"


def _trace_excerpt(value: object, *, max_lines: int = 6, max_chars: int = 900) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        clipped += f"\n… {len(lines) - max_lines} more lines"
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 1] + "…"
    return clipped


PLAN_MARKERS = {"completed": "✓", "in_progress": "→", "pending": "○"}


def _looks_serialized(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text[:1] in '[{"' or text.startswith("```") or "tools." in text or "await " in text)


def _timeline_line(event: object) -> Table:
    timestamp = float(_value(event, "presentation_timestamp", _value(event, "timestamp", 0)) or 0)
    stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    kind = str(_value(event, "kind", ""))
    severity, color = event_severity(kind)
    summary = str(_value(event, "summary", kind or "事件"))
    detail = str(_value(event, "detail", ""))
    metadata = _value(event, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    tag = _trace_tag(kind, metadata)
    if kind in {"TOOL_RUNNING", "TOOL_COMPLETED"}:
        tool_label = str(
            metadata.get("display_name") or metadata.get("tool_name") or detail or "工具"
        )
        failed = metadata.get("exit_code") not in (None, 0) or str(
            metadata.get("completion_status") or ""
        ).lower() in {"failed", "error", "errored"}
        if kind == "TOOL_RUNNING":
            summary = f"正在调用 {tool_label}"
        elif failed:
            summary = f"{tool_label} 调用失败"
        else:
            summary = f"{tool_label} 调用完成"
        if detail == tool_label or _tool_name_is_fallback({"display_name": detail}):
            detail = ""
    table = Table.grid(expand=True, padding=0)
    table.add_column(width=8, no_wrap=True)
    table.add_column(width=3, no_wrap=True)
    table.add_column(width=9, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")

    headline = Text(operator_text(summary, max_cells=140), style=f"bold {color}")
    if detail and kind != "UNPARSED_PAYLOAD":
        detail_text = operator_text(
            _trace_excerpt(detail, max_lines=2, max_chars=420), max_cells=220
        )
        headline.append(f"  ·  {detail_text}", style="#cbd5e1")
    table.add_row(
        Text(stamp, style="#64748b"),
        Text(" │ ", style=color),
        Text(tag, style=f"bold {color}"),
        headline,
    )

    def add_detail(label: str, value: object, style: str = "#94a3b8") -> None:
        content = value if isinstance(value, Text) else Text(
            operator_text(value, max_cells=240), style=style
        )
        table.add_row(
            "",
            Text(" │ ", style="#334155"),
            Text(label, style="bold #64748b"),
            content,
        )

    command = _trace_excerpt(metadata.get("command"), max_lines=4, max_chars=700)
    tool_name = str(metadata.get("tool_name") or "")
    cwd = str(metadata.get("cwd") or "")
    files = [str(path) for path in (metadata.get("files") or [])]
    nested_tools = [str(name) for name in (metadata.get("nested_tools") or []) if name]
    if tool_name:
        add_detail("TOOL", tool_name, "#7dd3fc")
    if command:
        add_detail("CMD", f"$ {command}", "#e2e8f0")
    if cwd:
        add_detail("CWD", cwd, "#64748b")
    if files:
        change_types = metadata.get("change_types")
        change_types = change_types if isinstance(change_types, dict) else {}
        for path in files[:6]:
            operation = str(change_types.get(path) or "pending").upper()
            add_detail("FILE", f"{operation:<7}  {path}", "#7dd3fc")
        if len(files) > 6:
            add_detail("FILE", f"… 另有 {len(files) - 6} 个文件", "#64748b")
    if nested_tools:
        add_detail("CALLS", ", ".join(nested_tools[:6]), "#7dd3fc")
    if metadata.get("background_running"):
        cell_id = str(metadata.get("background_cell_id") or "?")
        waited = metadata.get("background_wait_seconds")
        task = f"cell {cell_id}"
        if isinstance(waited, (int, float)):
            task += f" · 已等待 {float(waited):.1f}s"
        task += " · 暂无新输出" if metadata.get("background_output_empty") else " · 有新输出"
        add_detail("TASK", task, "#fbbf24")
    if kind in {
        "COMPACT_REQUESTED",
        "COMPACTING",
        "COMPACT_COMPLETED",
        "COMPACT_FAILED",
        "COMPACT_ABORTED",
    }:
        context_tokens = metadata.get("context_tokens")
        context_window = metadata.get("context_window")
        if (
            isinstance(context_tokens, (int, float))
            and isinstance(context_window, (int, float))
            and context_window
        ):
            ratio = float(context_tokens) / float(context_window)
            add_detail(
                "CONTEXT",
                f"{int(context_tokens):,} / {int(context_window):,} · {ratio:.1%}",
                "#7dd3fc",
            )
        auto_limit = metadata.get("auto_compact_token_limit")
        if (
            isinstance(context_tokens, (int, float))
            and isinstance(auto_limit, (int, float))
            and auto_limit
        ):
            remaining = int(auto_limit) - int(context_tokens)
            boundary = Text(
                f"{int(context_tokens):,} / {int(auto_limit):,} · "
                f"{float(context_tokens) / float(auto_limit):.1%}",
                style="#fbbf24" if remaining <= 0 else "#7dd3fc",
            )
            boundary.append(f" · 剩余 {max(0, remaining):,}", style="#64748b")
            add_detail("BOUNDARY", boundary)
        source = metadata.get("source")
        confidence = metadata.get("confidence")
        if source or confidence:
            add_detail(
                "SOURCE",
                " · ".join(value for value in (str(source or ""), str(confidence or "")) if value),
            )
        if metadata.get("reconstructed"):
            add_detail("START", "由 completion 回溯重建", "#fbbf24")
    plan = metadata.get("plan")
    if isinstance(plan, list):
        for step in plan[:8]:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "pending")
            marker = PLAN_MARKERS.get(status, "•")
            text = str(step.get("step") or step.get("text") or "-")
            add_detail("STEP", f"{marker} {text}")
    arguments = metadata.get("arguments")
    if isinstance(arguments, str) and arguments.strip() and not _looks_serialized(arguments):
        add_detail("ARG", _trace_excerpt(arguments, max_lines=3, max_chars=600))
    unparsed = _value(event, "unparsed", None)
    if unparsed:
        add_detail(
            "SOURCE",
            f"{getattr(unparsed, 'source_type', 'unknown')} · "
            f"{getattr(unparsed, 'length', 0)} chars · "
            f"sha256 {getattr(unparsed, 'sha256', '')[:10]}",
            "#fbbf24",
        )
    exit_code = metadata.get("exit_code")
    duration = metadata.get("duration_seconds")
    footer = []
    if exit_code is not None:
        footer.append(f"exit {exit_code}")
    if isinstance(duration, (int, float)):
        footer.append(f"{float(duration):.2f}s")
    if footer:
        add_detail("META", " · ".join(footer), "#64748b")
    return table

"""Build bounded, read-only terminal transcripts from Codex evidence."""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import stat
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from models import (
    RUNNING_TERMINAL_STATUSES,
    TerminalCapability,
    TerminalChunk,
    TerminalSessionSummary,
)
from utils import redact_sensitive


MAX_TERMINAL_BYTES = 2 * 1024 * 1024
MAX_TERMINAL_CHUNKS = 4_000
MAX_TERMINALS_PER_SESSION = 16
MAX_GLOBAL_TERMINAL_BYTES = 16 * 1024 * 1024
TERMINAL_OS_MISS_WINDOWS = 2

_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_STRING_ESCAPE = re.compile(r"\x1b[PX^_].*?\x1b\\", re.DOTALL)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
_BACKGROUND_RUNNING = re.compile(
    r"^(?:Script running with cell ID|Process running with session ID) "
    r"(?P<process>[^\s\"\\\\]+).*?(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_SCRIPT_RESULT = re.compile(
    r"^Script (?:completed|failed).*?\n"
    r"Wall time[^\n]*\n(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_PROCESS_EXITED = re.compile(
    r"^Process exited with code (?P<code>-?\d+).*?\n"
    r"(?:Output|Final output):\s*(?P<output>.*)",
    re.DOTALL,
)
_EXPLICIT_PROCESS_ID = re.compile(
    r"(?m)^(?:SESSION_ID|CELL_ID|PROCESS_ID)\s*=\s*(?P<process>[^\s]+)\s*$",
    re.I,
)
_EXIT_CODE = re.compile(r"(?:Process exited with code|exit(?: code)?)\s*(?P<code>-?\d+)", re.I)
_TRUNCATED = re.compile(r"(?:tokens?|bytes?|chars?)\s+(?:truncated|omitted)|truncated after", re.I)
_TOOL_OUTPUT_TYPES = {
    "custom_tool_call_output",
    "function_call_output",
    "local_shell_call_output",
}
_TERMINAL_TOOLS = {"exec", "exec_command", "local_shell_call", "write_stdin", "wait"}


def sanitize_terminal_text(value: str) -> str:
    """Remove terminal control sequences without replaying them to CodexDeck's TTY."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC.sub("", text)
    text = _STRING_ESCAPE.sub("", text)
    text = _CSI.sub("", text)
    return redact_sensitive(_CONTROL.sub("", text))


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class _NestedToolCall:
    ordinal: int
    tool_name: str
    arguments: dict[str, Any]


def _js_string(value: str, start: int) -> tuple[str, int]:
    quote = value[start]
    result: list[str] = []
    index = start + 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
    while index < len(value):
        char = value[index]
        if char == quote:
            return "".join(result), index + 1
        if char == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            if escaped == "u" and index + 5 < len(value):
                try:
                    result.append(chr(int(value[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
            result.append(escapes.get(escaped, escaped))
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result), index


def _js_argument_value(value: str, start: int) -> tuple[object, int]:
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value):
        return "", index
    if value[index] in {'"', "'", "`"}:
        return _js_string(value, index)
    if value[index] == "[":
        end = index + 1
        depth = 1
        while end < len(value) and depth:
            if value[end] in {'"', "'", "`"}:
                _, end = _js_string(value, end)
                continue
            depth += value[end] == "["
            depth -= value[end] == "]"
            end += 1
        raw = value[index:end]
        try:
            return json.loads(raw), end
        except json.JSONDecodeError:
            return raw, end
    match = re.match(r"-?\d+(?:\.\d+)?", value[index:])
    if match:
        raw = match.group(0)
        return (float(raw) if "." in raw else int(raw)), index + len(raw)
    match = re.match(r"[A-Za-z_$][\w$]*", value[index:])
    if match:
        raw = match.group(0)
        return {"true": True, "false": False, "null": None}.get(raw, raw), index + len(raw)
    return "", index + 1


def _js_object_end(value: str, start: int) -> int:
    while start < len(value) and value[start].isspace():
        start += 1
    if start >= len(value) or value[start] != "{":
        return start
    depth = 0
    index = start
    while index < len(value):
        char = value[index]
        if char in {'"', "'", "`"}:
            _, index = _js_string(value, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(value)


def _js_terminal_arguments(value: str, start: int, end: int) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    keys = ("cmd", "command", "workdir", "cwd", "process_id", "session_id", "cell_id")
    object_text = value[start:end]
    for key in keys:
        match = re.search(rf"(?:[\"']?{key}[\"']?)\s*:\s*", object_text)
        if not match:
            continue
        parsed, _ = _js_argument_value(value, start + match.end())
        arguments[key] = parsed
    return arguments


def _nested_tool_calls(value: object) -> list[_NestedToolCall]:
    if not isinstance(value, str):
        return []
    calls: list[_NestedToolCall] = []
    index = 0
    while index < len(value):
        if value[index] in {'"', "'", "`"}:
            _, index = _js_string(value, index)
            continue
        if value.startswith("//", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            index = len(value) if end < 0 else end + 2
            continue
        if not value.startswith("tools.", index):
            index += 1
            continue
        name_start = index + len("tools.")
        name_match = re.match(r"[A-Za-z_$][\w$]*", value[name_start:])
        if not name_match:
            index += 1
            continue
        tool_name = name_match.group(0)
        call_start = name_start + len(tool_name)
        while call_start < len(value) and value[call_start].isspace():
            call_start += 1
        if call_start >= len(value) or value[call_start] != "(":
            index = call_start
            continue
        arguments_start = call_start + 1
        object_end = _js_object_end(value, arguments_start)
        try:
            parsed, consumed = json.JSONDecoder().raw_decode(
                value[arguments_start:object_end]
            )
        except (json.JSONDecodeError, TypeError):
            parsed = _js_terminal_arguments(value, arguments_start, object_end)
        else:
            if not consumed:
                index = max(call_start + 1, object_end)
                continue
        if isinstance(parsed, dict):
            calls.append(_NestedToolCall(len(calls), tool_name, parsed))
        index = max(call_start + 1, object_end)
    return calls


def _nested_terminal_call(value: object) -> tuple[str, dict[str, Any]]:
    for call in _nested_tool_calls(value):
        if call.tool_name in _TERMINAL_TOOLS:
            return call.tool_name, call.arguments
    return "", {}


def _argument_value(payload: dict[str, Any], item: dict[str, Any]) -> object:
    return (
        payload.get("arguments")
        or payload.get("input")
        or item.get("arguments")
        or item.get("input")
    )


def _arguments(payload: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    raw = _argument_value(payload, item)
    return _mapping(raw) or _nested_terminal_call(raw)[1]


def _tool_name(payload: dict[str, Any], item: dict[str, Any], item_type: str) -> str:
    explicit = str(
        payload.get("name")
        or payload.get("tool")
        or item.get("name")
        or item.get("tool")
        or ""
    ).strip()
    if explicit:
        return explicit
    if item_type in {"exec_command_begin", "exec_command_end", "command_execution"}:
        return "exec_command"
    return ""


def _command(value: object) -> str:
    if isinstance(value, list):
        return shlex.join(str(part) for part in value)
    return str(value or "")


def _content_text(value: object) -> str:
    """Flatten Codex content parts without serializing their container syntax."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_text(part) for part in value)
    if isinstance(value, dict):
        for key in ("text", "output", "content", "value"):
            if key in value:
                return _content_text(value[key])
        return ""
    return str(value)


def _process_id(value: object) -> str:
    """Keep a protocol process ID separate from serialized output delimiters."""

    process_id = str(value or "").strip()
    for delimiter in ("\\n", "\\r", "\n", "\r"):
        process_id = process_id.split(delimiter, 1)[0]
    return process_id.strip(" \t\"'")


def _output_fields(value: object) -> tuple[str, str, str, int | None, bool]:
    """Return text, stream, process id, exit code, and upstream truncation."""

    if isinstance(value, dict):
        process_id = _process_id(
            value.get("process_id")
            or value.get("session_id")
            or value.get("cell_id")
            or ""
        )
        exit_code = value.get("exit_code")
        text = _content_text(
            value.get("output")
            or value.get("aggregated_output")
            or value.get("stdout")
            or value.get("stderr")
            or ""
        )
        stream = "stderr" if value.get("stderr") and not value.get("stdout") else "combined"
        return (
            sanitize_terminal_text(text),
            stream,
            process_id,
            int(exit_code) if isinstance(exit_code, (int, float)) else None,
            bool(value.get("truncated") or value.get("omitted_bytes")) or bool(_TRUNCATED.search(text)),
        )
    text = sanitize_terminal_text(_content_text(value))
    explicit_processes = list(_EXPLICIT_PROCESS_ID.finditer(text))
    if explicit_processes:
        process_id = _process_id(explicit_processes[-1].group("process"))
        visible_text = _EXPLICIT_PROCESS_ID.sub("", text)
        visible_text = re.sub(r"\n{3,}", "\n\n", visible_text).rstrip("\n")
        if visible_text:
            visible_text += "\n"
        return (
            visible_text,
            "combined",
            process_id,
            None,
            bool(_TRUNCATED.search(text)),
        )
    running = _BACKGROUND_RUNNING.match(text)
    if running:
        return (
            running.group("output"),
            "combined",
            _process_id(running.group("process")),
            None,
            bool(_TRUNCATED.search(text)),
        )
    script_result = _SCRIPT_RESULT.match(text)
    if script_result:
        text = script_result.group("output")
        nested_result = _mapping(text)
        if (
            "wall_time_seconds" in nested_result
            and ("session_id" in nested_result or "exit_code" in nested_result)
        ):
            return _output_fields(nested_result)
        running = _BACKGROUND_RUNNING.match(text)
        if running:
            return (
                running.group("output"),
                "combined",
                _process_id(running.group("process")),
                None,
                bool(_TRUNCATED.search(text)),
            )
    exited_result = _PROCESS_EXITED.match(text)
    if exited_result:
        return (
            exited_result.group("output"),
            "combined",
            "",
            int(exited_result.group("code")),
            bool(_TRUNCATED.search(text)),
        )
    exit_match = _EXIT_CODE.match(text)
    return (
        text,
        "combined",
        "",
        int(exit_match.group("code")) if exit_match else None,
        bool(_TRUNCATED.search(text)),
    )


@dataclass(frozen=True)
class TerminalUpdate:
    source_id: str
    observed_at: float
    call_id: str = ""
    process_id: str = ""
    turn_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = "unknown"
    exit_code: int | None = None
    stream: str = "combined"
    output: str = ""
    capability: TerminalCapability = TerminalCapability.METADATA_ONLY
    terminal_candidate: bool = False
    cumulative: bool = False
    upstream_truncated: bool = False
    source: str = "rollout"
    continuation: bool = False
    wait_for_completion: bool = False


@dataclass(frozen=True)
class _TerminalRecordContext:
    record_type: str
    item_type: str
    payload: dict[str, Any]
    item: dict[str, Any]
    call_id: str
    turn_id: str
    tool_name: str
    argument_value: object
    output_value: object


@dataclass(frozen=True)
class _ToolCallBatch:
    outer_call_id: str
    turn_id: str
    calls: tuple[_NestedToolCall, ...]


def _terminal_record_context(record: dict[str, object]) -> _TerminalRecordContext:
    record_type = str(record.get("type") or "")
    payload_value = record.get("payload")
    payload = payload_value if isinstance(payload_value, dict) else {}
    item_value = payload.get("item")
    item = item_value if isinstance(item_value, dict) else {}
    item_type = str(payload.get("type") or item.get("type") or "")
    if record_type == "event_msg" and item_type in {"item_started", "item_completed"} and item:
        payload = {**payload, **item, "item": item}
        item_type = str(item.get("type") or "")
    return _TerminalRecordContext(
        record_type=record_type,
        item_type=item_type,
        payload=payload,
        item=item,
        call_id=str(
            payload.get("call_id")
            or payload.get("id")
            or item.get("call_id")
            or item.get("id")
            or ""
        ),
        turn_id=str(payload.get("turn_id") or item.get("turn_id") or ""),
        tool_name=_tool_name(payload, item, item_type).lower(),
        argument_value=_argument_value(payload, item),
        output_value=(
            payload.get("output")
            or payload.get("result")
            or item.get("output")
            or item.get("result")
        ),
    )


def _batch_call_id(outer_call_id: str, ordinal: int, total: int) -> str:
    if total <= 1:
        return outer_call_id
    return f"{outer_call_id}:tool:{ordinal}"


def _command_result(value: object) -> bool:
    if isinstance(value, dict):
        return "wall_time_seconds" in value and bool(
            {"session_id", "process_id", "cell_id", "exit_code", "chunk_id"} & value.keys()
        )
    text = sanitize_terminal_text(_content_text(value))
    if (
        _BACKGROUND_RUNNING.match(text)
        or _PROCESS_EXITED.match(text)
        or _EXPLICIT_PROCESS_ID.search(text)
    ):
        return True
    script_result = _SCRIPT_RESULT.match(text)
    if not script_result:
        return False
    nested = _mapping(script_result.group("output"))
    return _command_result(nested) if nested else True


def _unified_exec_results(value: object) -> tuple[object, ...] | None:
    """Decode one functions.exec result envelope without flattening parallel results."""

    if isinstance(value, list):
        parts = [_content_text(part) for part in value]
        if len(parts) > 1 and re.match(
            r"^Script (?:completed|failed).*?(?:Output|Final output):\s*$",
            parts[0],
            re.DOTALL,
        ):
            return tuple(_mapping(part) or part for part in parts[1:])
        value = "".join(parts)
    text = _content_text(value)
    if not text:
        return None
    sanitized = sanitize_terminal_text(text)
    script_result = _SCRIPT_RESULT.match(sanitized)
    if script_result:
        nested = _mapping(script_result.group("output"))
        return (nested or sanitized,)
    if (
        _BACKGROUND_RUNNING.match(sanitized)
        or _PROCESS_EXITED.match(sanitized)
        or _EXPLICIT_PROCESS_ID.search(sanitized)
    ):
        return (sanitized,)
    return None


class TerminalProtocolParser:
    """Parse direct and wrapped terminal protocol records into stable operations."""

    def __init__(self, max_pending_batches: int = 4_096) -> None:
        self.max_pending_batches = max_pending_batches
        self.pending_batches: OrderedDict[tuple[str, str], _ToolCallBatch] = OrderedDict()

    def _remember(self, scope: str, batch: _ToolCallBatch) -> None:
        if not batch.outer_call_id:
            return
        key = (scope, batch.outer_call_id)
        self.pending_batches[key] = batch
        self.pending_batches.move_to_end(key)
        while len(self.pending_batches) > self.max_pending_batches:
            self.pending_batches.popitem(last=False)

    @staticmethod
    def _combine_fragments(fragments: list[object]) -> object:
        if len(fragments) == 1:
            return fragments[0]
        values = [_content_text(fragment) for fragment in fragments]
        return "\n".join(value.rstrip("\n") for value in values if value)

    @classmethod
    def _result_slots(
        cls,
        calls: tuple[_NestedToolCall, ...],
        results: tuple[object, ...],
    ) -> list[tuple[int, _NestedToolCall, object]]:
        if len(calls) == 1:
            return [(0, calls[0], cls._combine_fragments(list(results)))]
        if len(results) == len(calls) and all(
            _mapping(result) or _command_result(result) for result in results
        ):
            return [
                (ordinal, calls[ordinal], result)
                for ordinal, result in enumerate(results)
            ]

        groups: list[object] = []
        pending: list[object] = []
        for result in results:
            pending.append(result)
            text = sanitize_terminal_text(_content_text(result))
            boundary = bool(
                _mapping(result)
                or _BACKGROUND_RUNNING.match(text)
                or _PROCESS_EXITED.match(text)
                or _EXPLICIT_PROCESS_ID.search(text)
            )
            if boundary:
                groups.append(cls._combine_fragments(pending))
                pending = []
        if pending:
            groups.append(cls._combine_fragments(pending))
        if len(groups) == len(calls):
            return [
                (ordinal, calls[ordinal], result)
                for ordinal, result in enumerate(groups)
            ]

        return [
            (ordinal, calls[ordinal], result)
            for ordinal, result in enumerate(results[: len(calls)])
        ]

    @staticmethod
    def _invocation_update(
        context: _TerminalRecordContext,
        call: _NestedToolCall,
        total: int,
        source_id: str,
        observed_at: float,
    ) -> TerminalUpdate:
        arguments = call.arguments
        process_id = _process_id(
            arguments.get("process_id")
            or arguments.get("session_id")
            or arguments.get("cell_id")
            or ""
        )
        tool_name = call.tool_name.lower()
        return TerminalUpdate(
            source_id=f"{source_id}:tool:{call.ordinal}",
            observed_at=observed_at,
            call_id=_batch_call_id(context.call_id, call.ordinal, total),
            process_id=process_id,
            turn_id=context.turn_id,
            command=_command(arguments.get("cmd") or arguments.get("command")),
            cwd=str(arguments.get("workdir") or arguments.get("cwd") or ""),
            status="running",
            capability=(
                TerminalCapability.POLL_TRANSCRIPT
                if process_id or tool_name in {"write_stdin", "wait"}
                else TerminalCapability.METADATA_ONLY
            ),
            terminal_candidate=True,
            continuation=tool_name in {"write_stdin", "wait"},
            wait_for_completion=tool_name == "wait",
        )

    @staticmethod
    def _result_update(
        context: _TerminalRecordContext,
        call: _NestedToolCall | None,
        result: object,
        ordinal: int,
        total: int,
        source_id: str,
        observed_at: float,
        turn_id: str = "",
    ) -> TerminalUpdate:
        text, stream, process_id, exit_code, truncated = _output_fields(result)
        arguments = call.arguments if call is not None else {}
        tool_name = call.tool_name.lower() if call is not None else "exec_command"
        process_id = process_id or _process_id(
            arguments.get("process_id")
            or arguments.get("session_id")
            or arguments.get("cell_id")
            or ""
        )
        running = bool(process_id and exit_code is None)
        return TerminalUpdate(
            source_id=f"{source_id}:tool:{ordinal}",
            observed_at=observed_at,
            call_id=_batch_call_id(context.call_id, ordinal, total),
            process_id=process_id,
            turn_id=context.turn_id or turn_id,
            command=_command(arguments.get("cmd") or arguments.get("command")),
            cwd=str(arguments.get("workdir") or arguments.get("cwd") or ""),
            status="running" if running else "completed",
            exit_code=exit_code,
            stream=stream,
            output=text,
            capability=(
                TerminalCapability.POLL_TRANSCRIPT
                if process_id
                else TerminalCapability.FINAL_TRANSCRIPT
            ),
            terminal_candidate=True,
            upstream_truncated=truncated,
            continuation=tool_name in {"write_stdin", "wait"},
            wait_for_completion=tool_name == "wait",
        )

    def parse(
        self,
        record: dict[str, object],
        source_id: str,
        observed_at: float,
        scope: str = "",
    ) -> tuple[TerminalUpdate, ...]:
        context = _terminal_record_context(record)
        nested_calls = (
            _nested_tool_calls(context.argument_value)
            if context.tool_name in {"exec", "functions.exec"}
            else []
        )
        if nested_calls:
            batch = _ToolCallBatch(context.call_id, context.turn_id, tuple(nested_calls))
            self._remember(scope, batch)
            return tuple(
                self._invocation_update(
                    context,
                    call,
                    len(nested_calls),
                    source_id,
                    observed_at,
                )
                for call in nested_calls
                if call.tool_name.lower() in _TERMINAL_TOOLS
            )

        results = _unified_exec_results(context.output_value)
        batch = (
            self.pending_batches.pop((scope, context.call_id), None)
            if results is not None
            else None
        )
        if results is not None and (
            batch is not None or context.item_type == "custom_tool_call_output"
        ):
            if batch is not None:
                selected = [
                    (ordinal, call, result)
                    for ordinal, call, result in self._result_slots(batch.calls, results)
                    if call.tool_name.lower() in _TERMINAL_TOOLS
                ]
                total = len(batch.calls)
            else:
                command_results = [result for result in results if _command_result(result)]
                if len(command_results) == 1:
                    selected = [(0, None, self._combine_fragments(list(results)))]
                    total = 1
                else:
                    selected = [
                        (ordinal, None, result)
                        for ordinal, result in enumerate(results)
                        if _command_result(result)
                    ]
                    total = len(results)
            if selected:
                return tuple(
                    self._result_update(
                        context,
                        call,
                        result,
                        ordinal,
                        total,
                        source_id,
                        observed_at,
                        batch.turn_id if batch is not None else "",
                    )
                    for ordinal, call, result in selected
                )

        return _extract_direct_terminal_updates(record, source_id, observed_at)


def _extract_direct_terminal_updates(
    record: dict[str, object], source_id: str, observed_at: float
) -> tuple[TerminalUpdate, ...]:
    """Extract terminal evidence from a single direct protocol call."""

    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    item = payload.get("item")
    item = item if isinstance(item, dict) else {}
    item_type = str(payload.get("type") or item.get("type") or "")
    if record_type == "event_msg" and item_type in {"item_started", "item_completed"} and item:
        payload = {**payload, **item, "item": item}
        item_type = str(item.get("type") or "")

    call_id = str(
        payload.get("call_id")
        or payload.get("id")
        or item.get("call_id")
        or item.get("id")
        or ""
    )
    turn_id = str(payload.get("turn_id") or item.get("turn_id") or "")
    process_id = _process_id(payload.get("process_id") or item.get("process_id") or "")
    tool_name = _tool_name(payload, item, item_type).lower()
    nested_tool, _ = _nested_terminal_call(_argument_value(payload, item))
    if nested_tool and tool_name in {"exec", "functions.exec"}:
        tool_name = nested_tool
    arguments = _arguments(payload, item)
    if not process_id:
        process_id = _process_id(
            arguments.get("process_id")
            or arguments.get("session_id")
            or arguments.get("cell_id")
            or ""
        )
    command = _command(
        payload.get("command")
        or item.get("command")
        or arguments.get("cmd")
        or arguments.get("command")
    )
    cwd = str(
        payload.get("cwd")
        or item.get("cwd")
        or arguments.get("workdir")
        or arguments.get("cwd")
        or ""
    )

    output_value = (
        payload.get("output")
        or payload.get("result")
        or item.get("output")
        or item.get("result")
    )
    direct_end = item_type in {"exec_command_end", "command_execution"}
    if direct_end:
        stdout = payload.get("stdout") or item.get("stdout")
        stderr = payload.get("stderr") or item.get("stderr")
        if stdout is not None or stderr is not None:
            declared_exit = payload.get("exit_code")
            if declared_exit is None:
                declared_exit = item.get("exit_code")
            exit_code = int(declared_exit) if isinstance(declared_exit, (int, float)) else None
            status = str(payload.get("status") or item.get("status") or "completed").lower()
            truncated = bool(
                payload.get("truncated")
                or item.get("truncated")
                or payload.get("omitted_bytes")
                or item.get("omitted_bytes")
            )
            updates: list[TerminalUpdate] = []
            for stream, value in (("stdout", stdout), ("stderr", stderr)):
                if value in (None, ""):
                    continue
                text = sanitize_terminal_text(str(value))
                updates.append(
                    TerminalUpdate(
                        source_id=f"{source_id}:{stream}",
                        observed_at=observed_at,
                        call_id=call_id,
                        process_id=process_id,
                        turn_id=turn_id,
                        command=command,
                        cwd=cwd,
                        status=status,
                        exit_code=exit_code,
                        stream=stream,
                        output=text,
                        capability=TerminalCapability.FINAL_TRANSCRIPT,
                        terminal_candidate=True,
                        upstream_truncated=truncated or bool(_TRUNCATED.search(text)),
                    )
                )
            if updates:
                return tuple(updates)
    if direct_end:
        output_value = (
            payload.get("aggregated_output")
            or item.get("aggregated_output")
            or payload.get("stdout")
            or item.get("stdout")
            or output_value
        )
    if record_type == "response_item" and item_type in _TOOL_OUTPUT_TYPES:
        output_value = payload.get("output") or payload.get("result")

    if output_value is not None:
        text, stream, output_process, exit_code, truncated = _output_fields(output_value)
        declared_exit = payload.get("exit_code")
        if declared_exit is None:
            declared_exit = item.get("exit_code")
        if exit_code is None and isinstance(declared_exit, (int, float)):
            exit_code = int(declared_exit)
        process_id = process_id or _process_id(output_process)
        running = bool(output_process and exit_code is None)
        return (
            TerminalUpdate(
                source_id=source_id,
                observed_at=observed_at,
                call_id=call_id,
                process_id=process_id,
                turn_id=turn_id,
                command=command,
                cwd=cwd,
                status=(
                    "running"
                    if running
                    else str(payload.get("status") or item.get("status") or "completed").lower()
                ),
                exit_code=exit_code,
                stream=stream,
                output=text,
                capability=(
                    TerminalCapability.POLL_TRANSCRIPT
                    if running or process_id
                    else TerminalCapability.FINAL_TRANSCRIPT
                ),
                terminal_candidate=bool(
                    tool_name in _TERMINAL_TOOLS
                    or process_id
                    or command
                    or direct_end
                ),
                cumulative=direct_end,
                upstream_truncated=truncated,
            ),
        )

    is_terminal = bool(
        tool_name in _TERMINAL_TOOLS
        or item_type in {"exec_command_begin", "command_execution"}
        or command
        or process_id
    )
    if not is_terminal:
        return ()
    status = str(payload.get("status") or item.get("status") or "running").lower()
    exit_code = payload.get("exit_code") or item.get("exit_code")
    return (
        TerminalUpdate(
            source_id=source_id,
            observed_at=observed_at,
            call_id=call_id,
            process_id=process_id,
            turn_id=turn_id,
            command=command,
            cwd=cwd,
            status=status,
            exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            capability=(
                TerminalCapability.POLL_TRANSCRIPT
                if process_id or tool_name in {"write_stdin", "wait"}
                else TerminalCapability.METADATA_ONLY
            ),
            terminal_candidate=True,
            continuation=tool_name in {"write_stdin", "wait"},
            wait_for_completion=tool_name == "wait",
        ),
    )


def extract_terminal_updates(
    record: dict[str, object],
    source_id: str,
    observed_at: float,
    *,
    parser: TerminalProtocolParser | None = None,
    scope: str = "",
) -> tuple[TerminalUpdate, ...]:
    """Extract terminal evidence without changing lifecycle normalization."""

    return (parser or TerminalProtocolParser()).parse(
        record,
        source_id,
        observed_at,
        scope,
    )


@dataclass
class _TerminalSession:
    terminal_id: str
    root_call_id: str = ""
    process_id: str = ""
    turn_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = "unknown"
    exit_code: int | None = None
    capability: TerminalCapability = TerminalCapability.METADATA_ONLY
    started_at: float | None = None
    completed_at: float | None = None
    last_output_at: float | None = None
    dropped_bytes: int = 0
    upstream_truncated: bool = False
    stale: bool = False
    source: str = "rollout"
    last_state_at: float = 0.0
    process_active: bool = False
    os_confirmed: bool = False
    os_match_windows: int = 0
    pending_os_process_id: str = ""
    os_miss_windows: int = 0
    last_os_seen_at: float | None = None
    chunks: deque[TerminalChunk] = field(default_factory=deque)
    retained_bytes: int = 0

    def append(self, update: TerminalUpdate, sequence: int) -> None:
        text = update.output
        if update.cumulative and text and self.chunks:
            current = "".join(chunk.text for chunk in self.chunks if chunk.stream != "system")
            if text == current or current.endswith(text):
                text = ""
            elif text.startswith(current):
                text = text[len(current) :]
            elif current and current in text:
                text = text.split(current, 1)[1]
            elif current:
                text = "\n[final aggregate]\n" + text
        if not text:
            return
        encoded = text.encode("utf-8", errors="replace")
        self.chunks.append(
            TerminalChunk(
                source_id=update.source_id,
                observed_at=update.observed_at,
                stream=update.stream,
                text=text,
                sequence=sequence,
            )
        )
        self.retained_bytes += len(encoded)
        self.last_output_at = update.observed_at
        while self.chunks and (
            len(self.chunks) > MAX_TERMINAL_CHUNKS
            or self.retained_bytes > MAX_TERMINAL_BYTES
        ):
            removed = self.chunks.popleft()
            size = len(removed.text.encode("utf-8", errors="replace"))
            self.retained_bytes = max(0, self.retained_bytes - size)
            self.dropped_bytes += size

    def summary(self) -> TerminalSessionSummary:
        chunks = tuple(self.chunks)
        if self.dropped_bytes:
            marker = TerminalChunk(
                source_id=f"trim:{self.terminal_id}",
                observed_at=self.last_output_at or self.started_at or 0.0,
                stream="system",
                text=f"[CodexDeck dropped {self.dropped_bytes} earlier bytes]\n",
                sequence=-1,
            )
            chunks = (marker, *chunks)
        return TerminalSessionSummary(
            terminal_id=self.terminal_id,
            root_call_id=self.root_call_id,
            process_id=self.process_id,
            turn_id=self.turn_id,
            command=self.command,
            cwd=self.cwd,
            status=self.status,
            exit_code=self.exit_code,
            capability=self.capability,
            started_at=self.started_at,
            completed_at=self.completed_at,
            last_output_at=self.last_output_at,
            retained_bytes=self.retained_bytes,
            dropped_bytes=self.dropped_bytes,
            upstream_truncated=self.upstream_truncated,
            stale=self.stale,
            process_active=self.process_active,
            source=self.source,
            chunks=chunks,
        )


class TerminalStore:
    """Correlate terminal updates while keeping output memory bounded."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, _TerminalSession]] = defaultdict(dict)
        self.call_ids: dict[str, dict[str, str]] = defaultdict(dict)
        self.process_ids: dict[str, dict[str, str]] = defaultdict(dict)
        self.continuation_call_ids: dict[str, set[str]] = defaultdict(set)
        self.wait_call_ids: dict[str, set[str]] = defaultdict(set)
        self.seen_sources: dict[str, set[str]] = defaultdict(set)
        self.sequence = 0

    @staticmethod
    def _rank(capability: TerminalCapability) -> int:
        return {
            TerminalCapability.METADATA_ONLY: 0,
            TerminalCapability.FINAL_TRANSCRIPT: 1,
            TerminalCapability.POLL_TRANSCRIPT: 2,
            TerminalCapability.FILE_TAIL: 3,
            TerminalCapability.STREAMING: 4,
        }[capability]

    def apply(self, session_key: str, updates: tuple[TerminalUpdate, ...]) -> bool:
        changed = False
        for update in updates:
            if update.source_id in self.seen_sources[session_key]:
                continue
            if update.continuation and update.call_id:
                self.continuation_call_ids[session_key].add(update.call_id)
            if update.wait_for_completion and update.call_id:
                self.wait_call_ids[session_key].add(update.call_id)
            terminal_id = ""
            if update.process_id:
                terminal_id = self.process_ids[session_key].get(update.process_id, "")
            if not terminal_id and update.call_id:
                terminal_id = self.call_ids[session_key].get(update.call_id, "")
            if not terminal_id and not update.terminal_candidate:
                continue
            if not terminal_id:
                terminal_id = update.process_id or update.call_id or update.source_id
                self.sessions[session_key][terminal_id] = _TerminalSession(
                    terminal_id=terminal_id,
                    root_call_id=update.call_id,
                    process_id=update.process_id,
                    turn_id=update.turn_id,
                    command=update.command,
                    cwd=update.cwd,
                    status=update.status,
                    capability=update.capability,
                    started_at=update.observed_at,
                    source=update.source,
                    last_state_at=update.observed_at,
                )
            terminal = self.sessions[session_key][terminal_id]
            if update.call_id:
                self.call_ids[session_key][update.call_id] = terminal_id
            if update.process_id:
                self.process_ids[session_key][update.process_id] = terminal_id
                terminal.process_id = terminal.process_id or update.process_id
            terminal.root_call_id = terminal.root_call_id or update.call_id
            terminal.turn_id = terminal.turn_id or update.turn_id
            terminal.command = terminal.command or update.command
            terminal.cwd = terminal.cwd or update.cwd
            terminal.upstream_truncated |= update.upstream_truncated
            if self._rank(update.capability) > self._rank(terminal.capability):
                terminal.capability = update.capability
            if update.observed_at >= terminal.last_state_at:
                terminal.last_state_at = update.observed_at
                status = update.status
                if (
                    update.call_id in self.continuation_call_ids[session_key]
                    and update.exit_code is None
                    and status in {"completed", "complete", "success"}
                    and (
                        update.continuation
                        or update.call_id not in self.wait_call_ids[session_key]
                    )
                ):
                    status = "running"
                if status and status != "unknown":
                    terminal.status = status
                if update.exit_code is not None:
                    terminal.exit_code = update.exit_code
                if terminal.status in {"completed", "failed", "declined", "error", "errored"}:
                    terminal.completed_at = update.observed_at
                    terminal.process_active = False
                elif (
                    terminal.status in RUNNING_TERMINAL_STATUSES
                    and update.source == "file-tail"
                ):
                    terminal.process_active = True
            self.sequence += 1
            terminal.append(update, self.sequence)
            self.seen_sources[session_key].add(update.source_id)
            changed = True
        self._trim_sessions(session_key)
        self._trim_global()
        return changed

    def _trim_sessions(self, session_key: str) -> None:
        values = self.sessions.get(session_key, {})
        if len(values) <= MAX_TERMINALS_PER_SESSION:
            return
        ordered = sorted(
            values.values(),
            key=lambda item: (
                item.status in RUNNING_TERMINAL_STATUSES,
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        for terminal in ordered[: len(values) - MAX_TERMINALS_PER_SESSION]:
            values.pop(terminal.terminal_id, None)
            for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
                for key, value in list(mapping.items()):
                    if value == terminal.terminal_id:
                        mapping.pop(key, None)

    def summaries(self, session_key: str) -> list[TerminalSessionSummary]:
        return [
            terminal.summary()
            for terminal in sorted(
                self.sessions.get(session_key, {}).values(),
                key=lambda item: item.started_at or 0.0,
            )
        ]

    def current_summaries(self, session_key: str) -> list[TerminalSessionSummary]:
        """Publish background tasks known active from protocol or current OS evidence."""

        return [
            summary
            for summary in self.summaries(session_key)
            if summary.status in RUNNING_TERMINAL_STATUSES
            and bool(summary.process_id)
            and summary.process_active
            and not summary.stale
            and (
                summary.source == "file-tail"
                or self.sessions[session_key][summary.terminal_id].os_confirmed
            )
        ]

    @staticmethod
    def _command_matches_child(
        command: str,
        child_command: str,
        *,
        exact_only: bool = False,
    ) -> bool:
        if not command or not child_command:
            return False

        normalized_command = " ".join(command.split())
        normalized_child = " ".join(child_command.split())
        if normalized_command in normalized_child:
            return True
        if exact_only:
            return False

        def tokens(value: str) -> set[str]:
            try:
                values = shlex.split(value)
            except ValueError:
                values = value.split()
            return {
                Path(token).name.lower()
                for token in values
                if token and not token.startswith("-") and "=" not in token
            }

        command_tokens = tokens(command)
        child_tokens = tokens(child_command)
        if not command_tokens or not child_tokens:
            return False
        if command_tokens & child_tokens:
            return True

        return bool(
            {re.sub(r"[\d.]+$", "", token) for token in command_tokens}
            & {re.sub(r"[\d.]+$", "", token) for token in child_tokens}
        )

    @staticmethod
    def _os_process_id(child: object) -> str:
        identity = getattr(child, "identity", None)
        pid = getattr(identity, "pid", None)
        start_time = getattr(identity, "start_time", None)
        if not isinstance(pid, int) or not isinstance(start_time, int):
            return ""
        return f"os:{pid}:{start_time}"

    def reconcile_children(
        self,
        session_key: str,
        children: tuple[object, ...],
        observed_at: float,
        *,
        evidence_cutoff: float | None = None,
    ) -> bool:
        """Close previously confirmed rollout terminals after stable OS absence."""

        all_live_children = [
            child
            for child in children
            if str(getattr(child, "state", "")).upper() != "Z"
        ]
        candidates = [
            terminal
            for terminal in self.sessions.get(session_key, {}).values()
            if terminal.source == "rollout"
            and terminal.command
            and not terminal.stale
            and (
                (
                    bool(terminal.process_id)
                    and terminal.status in RUNNING_TERMINAL_STATUSES
                )
                or (
                    not terminal.process_id
                    and terminal.completed_at is not None
                )
            )
            and (
                evidence_cutoff is None
                or terminal.last_state_at <= evidence_cutoff
            )
        ]
        if not candidates:
            return False
        for terminal in candidates:
            if terminal.os_confirmed:
                terminal.process_active = False
        matched_terminal_ids: set[str] = set()
        claimed_job_roots: set[int] = set()
        changed = False
        for terminal in reversed(candidates):
            exact_only = (
                not terminal.process_id
                or terminal.process_id.startswith("os:")
            )
            matching_children = [
                child
                for child in all_live_children
                if self._command_matches_child(
                    terminal.command,
                    str(getattr(child, "command", "") or ""),
                    exact_only=exact_only,
                )
            ]
            matching_pids = {
                getattr(getattr(child, "identity", None), "pid", None)
                for child in matching_children
            }
            job_roots = [
                child
                for child in matching_children
                if getattr(child, "parent_pid", 0) not in matching_pids
            ]
            for child in job_roots:
                child_pid = getattr(getattr(child, "identity", None), "pid", None)
                if not isinstance(child_pid, int) or child_pid in claimed_job_roots:
                    continue
                if self._command_matches_child(
                    terminal.command,
                    str(getattr(child, "command", "") or ""),
                    exact_only=exact_only,
                ):
                    os_process_id = self._os_process_id(child)
                    existing = self.process_ids[session_key].get(os_process_id, "")
                    if existing and existing != terminal.terminal_id:
                        continue
                    matched_terminal_ids.add(terminal.terminal_id)
                    claimed_job_roots.add(child_pid)
                    protocol_confirmed = bool(
                        terminal.process_id
                        and not terminal.process_id.startswith("os:")
                    )
                    if protocol_confirmed:
                        terminal.os_match_windows = TERMINAL_OS_MISS_WINDOWS
                    elif terminal.pending_os_process_id == os_process_id:
                        terminal.os_match_windows += 1
                    else:
                        terminal.pending_os_process_id = os_process_id
                        terminal.os_match_windows = 1
                    if terminal.os_match_windows >= TERMINAL_OS_MISS_WINDOWS:
                        if not terminal.process_id and os_process_id:
                            terminal.process_id = os_process_id
                            self.process_ids[session_key][os_process_id] = terminal.terminal_id
                            changed = True
                        if terminal.status not in RUNNING_TERMINAL_STATUSES:
                            terminal.status = "running"
                            terminal.completed_at = None
                            terminal.exit_code = None
                            changed = True
                        terminal.os_confirmed = True
                        terminal.process_active = True
                    terminal.os_miss_windows = 0
                    terminal.last_os_seen_at = observed_at
                    break
        unmatched = [
            terminal
            for terminal in candidates
            if terminal.terminal_id not in matched_terminal_ids
        ]
        for terminal in unmatched:
            if not terminal.os_confirmed:
                terminal.pending_os_process_id = ""
                terminal.os_match_windows = 0
                continue
            terminal.os_miss_windows += 1
            if terminal.os_miss_windows < TERMINAL_OS_MISS_WINDOWS:
                continue
            terminal.status = "completed"
            terminal.completed_at = observed_at
            terminal.last_state_at = max(terminal.last_state_at, observed_at)
            changed = True
        return changed

    def _trim_global(self) -> None:
        terminals = [
            terminal
            for values in self.sessions.values()
            for terminal in values.values()
        ]
        total = sum(terminal.retained_bytes for terminal in terminals)
        if total <= MAX_GLOBAL_TERMINAL_BYTES:
            return
        ordered = sorted(
            terminals,
            key=lambda item: (
                item.status in RUNNING_TERMINAL_STATUSES,
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        while total > MAX_GLOBAL_TERMINAL_BYTES and ordered:
            progressed = False
            for terminal in ordered:
                if not terminal.chunks:
                    continue
                removed = terminal.chunks.popleft()
                size = len(removed.text.encode("utf-8", errors="replace"))
                terminal.retained_bytes = max(0, terminal.retained_bytes - size)
                terminal.dropped_bytes += size
                total -= size
                progressed = True
                if total <= MAX_GLOBAL_TERMINAL_BYTES:
                    break
            if not progressed:
                break

    def mark_stale(self, session_key: str) -> None:
        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.status in RUNNING_TERMINAL_STATUSES:
                terminal.stale = True
                terminal.process_active = False

    def mark_process_unavailable(self, session_key: str) -> None:
        """Hide live terminals when the current process tree cannot be verified."""

        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.status in RUNNING_TERMINAL_STATUSES:
                terminal.process_active = False

    def prune(self, retained_session_keys: set[str]) -> None:
        for session_key in set(self.sessions) - retained_session_keys:
            self.sessions.pop(session_key, None)
            self.call_ids.pop(session_key, None)
            self.process_ids.pop(session_key, None)
            self.continuation_call_ids.pop(session_key, None)
            self.wait_call_ids.pop(session_key, None)
            self.seen_sources.pop(session_key, None)


@dataclass
class _FileCursor:
    device: int
    inode: int
    offset: int = 0
    partial: bytes = b""
    process_id: str = ""
    command: str = ""
    cwd: str = ""


class RegularFileTailCollector:
    """Tail child stdout/stderr only when they resolve to allowed regular files."""

    def __init__(self, root: Path = Path("/proc"), max_read_bytes: int = 512 * 1024) -> None:
        self.root = root
        self.max_read_bytes = max_read_bytes
        self.cursors: dict[tuple[str, int, int], _FileCursor] = {}

    @staticmethod
    def _allowed(target: Path, workspace: Path) -> bool:
        target = target.resolve(strict=False)
        roots = [workspace.resolve(strict=False), Path("/tmp")]
        for root in roots:
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def read(
        self,
        session_key: str,
        workspace: str,
        children: tuple[object, ...],
        observed_at: float,
    ) -> tuple[TerminalUpdate, ...]:
        workspace_path = Path(workspace or ".")
        updates: list[TerminalUpdate] = []
        active_keys: set[tuple[str, int, int]] = set()
        for child in children:
            identity = getattr(child, "identity", None)
            pid = getattr(identity, "pid", None)
            start_time = getattr(identity, "start_time", 0)
            if not isinstance(pid, int):
                continue
            targets: dict[tuple[int, int], tuple[Path, set[int]]] = {}
            for fd in (1, 2):
                descriptor = self.root / str(pid) / "fd" / str(fd)
                try:
                    raw_target = os.readlink(descriptor)
                    target = Path(raw_target.removesuffix(" (deleted)"))
                    descriptor_stat = descriptor.stat()
                except OSError:
                    continue
                if not target.is_absolute() or not stat.S_ISREG(descriptor_stat.st_mode):
                    continue
                if not self._allowed(target, workspace_path):
                    continue
                key = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                if key in targets:
                    targets[key][1].add(fd)
                else:
                    targets[key] = (descriptor, {fd})
            for (device, inode), (descriptor, fds) in targets.items():
                cursor_key = (session_key, pid, inode)
                active_keys.add(cursor_key)
                cursor = self.cursors.get(cursor_key)
                try:
                    size = descriptor.stat().st_size
                except OSError:
                    continue
                upstream_truncated = False
                if cursor is None or (cursor.device, cursor.inode) != (device, inode):
                    offset = max(0, size - MAX_TERMINAL_BYTES)
                    upstream_truncated = offset > 0
                    cursor = _FileCursor(
                        device,
                        inode,
                        offset,
                        process_id=f"os:{pid}:{start_time}",
                        command=str(getattr(child, "command", "") or "child process"),
                        cwd=workspace,
                    )
                    self.cursors[cursor_key] = cursor
                elif size < cursor.offset:
                    cursor.offset = 0
                    cursor.partial = b""
                updates.append(
                    TerminalUpdate(
                        source_id=f"file-active:{device}:{inode}:{observed_at}",
                        observed_at=observed_at,
                        process_id=cursor.process_id,
                        command=cursor.command,
                        cwd=cursor.cwd,
                        status="running",
                        capability=TerminalCapability.FILE_TAIL,
                        terminal_candidate=True,
                        upstream_truncated=upstream_truncated,
                        source="file-tail",
                    )
                )
                if size <= cursor.offset:
                    continue
                try:
                    with descriptor.open("rb") as handle:
                        handle.seek(cursor.offset)
                        start = cursor.offset
                        payload = handle.read(self.max_read_bytes)
                        cursor.offset = handle.tell()
                except OSError:
                    continue
                if not payload:
                    continue
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                decoded = decoder.decode(cursor.partial + payload, final=False)
                cursor.partial = decoder.getstate()[0]
                streams = {1: "stdout", 2: "stderr"}
                stream = (
                    streams[next(iter(fds))]
                    if len(fds) == 1
                    else "combined"
                )
                updates.append(
                    TerminalUpdate(
                        source_id=f"file:{device}:{inode}:{start}",
                        observed_at=observed_at,
                        process_id=cursor.process_id,
                        command=cursor.command,
                        cwd=cursor.cwd,
                        status="running",
                        stream=stream,
                        output=sanitize_terminal_text(decoded),
                        capability=TerminalCapability.FILE_TAIL,
                        terminal_candidate=True,
                        upstream_truncated=upstream_truncated,
                        source="file-tail",
                    )
                )
        closed_keys = {
            key
            for key in self.cursors
            if key[0] == session_key and key not in active_keys
        }
        for key in closed_keys:
            cursor = self.cursors[key]
            updates.append(
                TerminalUpdate(
                    source_id=f"file-closed:{cursor.device}:{cursor.inode}:{observed_at}",
                    observed_at=observed_at,
                    process_id=cursor.process_id,
                    command=cursor.command,
                    cwd=cursor.cwd,
                    status="completed",
                    capability=TerminalCapability.FILE_TAIL,
                    terminal_candidate=True,
                    source="file-tail",
                )
            )
        self.cursors = {
            key: cursor
            for key, cursor in self.cursors.items()
            if key[0] != session_key or key in active_keys
        }
        return tuple(updates)

    def prune(self, retained_session_keys: set[str]) -> None:
        self.cursors = {
            key: cursor for key, cursor in self.cursors.items() if key[0] in retained_session_keys
        }

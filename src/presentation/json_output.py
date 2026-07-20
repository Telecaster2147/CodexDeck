"""Versioned JSON and NDJSON serialization."""

from __future__ import annotations

import json

from models import MonitorSnapshot, json_value
from utils import strip_transcript_bodies


SCHEMA_VERSION = 1
NULLABLE_STRING_FIELDS = {
    "alert",
    "alert_level",
    "alert_reason",
    "additional_details",
    "agent_path",
    "aggregated_output",
    "collaboration_mode",
    "command",
    "completion_status",
    "current_task",
    "cwd",
    "detail",
    "formatted_output",
    "interaction_input",
    "arguments",
    "model",
    "nickname",
    "output",
    "parent_thread_id",
    "reason",
    "reasoning_effort",
    "result",
    "role",
    "rollout_path",
    "session_id",
    "session_title",
    "source_id",
    "source",
    "stderr",
    "stdout",
    "trace_id",
    "transcript",
    "turn_id",
    "wait_channel",
}


def _normalize_nulls(value: object, field: str = "") -> object:
    if isinstance(value, dict):
        return {key: _normalize_nulls(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_nulls(item, field) for item in value]
    if value == "" and field in NULLABLE_STRING_FIELDS:
        return None
    return value


def snapshot_dict(snapshot: MonitorSnapshot, *, show_auxiliary: bool = False) -> dict[str, object]:
    instances = _normalize_nulls(strip_transcript_bodies(json_value(snapshot.instances)))
    if not show_auxiliary:
        for instance in instances:
            instance["processes"] = [
                process for process in instance["processes"] if process["role"] == "session"
            ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": snapshot.generated_at,
        "interval_seconds": snapshot.interval_seconds,
        "collection_duration_seconds": snapshot.collection_duration_seconds,
        "summary": snapshot.summary(),
        "diagnostics": snapshot.diagnostics,
        "instances": instances,
    }


def render_json(
    snapshot: MonitorSnapshot,
    *,
    pretty: bool,
    show_auxiliary: bool = False,
) -> str:
    return json.dumps(
        snapshot_dict(snapshot, show_auxiliary=show_auxiliary),
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )

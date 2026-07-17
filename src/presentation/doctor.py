"""Read-only collector and protocol capability diagnostics."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from models import MonitorSnapshot


DOCTOR_SCHEMA_VERSION = 1
COLLECTION_BUDGET_SECONDS = 2.0


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {field.name: _value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _collector_items(owner: object) -> list[dict[str, Any]]:
    raw = getattr(owner, "collector_health", ()) or ()
    if isinstance(raw, dict):
        raw = [
            {**value, "name": value.get("name", key)} if isinstance(value, dict) else value
            for key, value in raw.items()
        ]
    result: list[dict[str, Any]] = []
    for item in raw:
        data = _value(item)
        if isinstance(data, dict):
            result.append(data)
    return result


def _capability_rank(value: object) -> int:
    mode = value.get("mode", "unavailable") if isinstance(value, dict) else value
    return {"unavailable": 0, "derived": 1, "direct": 2}.get(str(mode), 0)


def _protocol_capabilities(instance: object) -> dict[str, Any]:
    direct = getattr(instance, "protocol_capabilities", None)
    value = _value(direct)
    merged: dict[str, Any] = value if isinstance(value, dict) else {}
    for session in getattr(instance, "sessions", ()):
        value = _value(getattr(session, "protocol_capabilities", None))
        if not isinstance(value, dict):
            continue
        for name, status in value.items():
            if name not in merged or _capability_rank(status) > _capability_rank(merged[name]):
                merged[name] = status
    return merged


def _rollout_diagnostics(instance: object) -> dict[str, Any]:
    return {
        "context_truncated": bool(getattr(instance, "rollout_context_truncated", False)),
        "activity": _value(getattr(instance, "rollout_activity", [])),
        "cursor": _value(getattr(instance, "rollout_cursor", None)),
        "file_identity": _value(getattr(instance, "rollout_file_identity", None)),
        "file_replaced": _value(getattr(instance, "rollout_file_replaced", None)),
        "partial_line": _value(getattr(instance, "rollout_partial_line", None)),
    }


def doctor_dict(snapshot: MonitorSnapshot) -> dict[str, Any]:
    global_collectors = _collector_items(snapshot)
    instances = []
    for instance in snapshot.instances:
        paths = instance.paths
        unknown = dict(getattr(instance, "unknown_event_types", {}) or {})
        instance_collectors = _collector_items(instance)
        if not instance_collectors:
            instance_collectors = [
                item
                for item in global_collectors
                if str(item.get("name", "")) in {"process", "socket"}
                or str(item.get("name", "")).endswith(f":{instance.instance_id}")
            ]
        instances.append(
            {
                "instance_id": instance.instance_id,
                "discovery_method": instance.discovery_method,
                "paths": {
                    "codex_home": str(paths.codex_home),
                    "sqlite_home": str(paths.sqlite_home),
                    "state_db": str(paths.state_db),
                    "log_db": str(paths.log_db),
                    "session_index": str(paths.session_index),
                    "sessions_dir": str(paths.sessions_dir),
                },
                "schema_capabilities": _value(instance.capabilities),
                "protocol_capabilities": _protocol_capabilities(instance),
                "diagnostics": list(instance.diagnostics),
                "unknown_events": {
                    "types": unknown,
                    "total": sum(unknown.values()),
                    "parse_success_rate": _value(
                        getattr(instance, "event_parse_success_rate", None)
                    ),
                },
                "rollout": _rollout_diagnostics(instance),
                "compact_sources": {
                    "tui_session_log": _value(instance.tui_session_log),
                    "hook_events": _value(instance.hook_events),
                },
                "collector_health": instance_collectors,
            }
        )

    duration = snapshot.collection_duration_seconds
    return {
        "doctor_schema_version": DOCTOR_SCHEMA_VERSION,
        "generated_at": snapshot.generated_at,
        "status": _status(snapshot),
        "collection": {
            "duration_seconds": duration,
            "budget_seconds": COLLECTION_BUDGET_SECONDS,
            "budget_exceeded": duration > COLLECTION_BUDGET_SECONDS,
            "process_data_stale_age_seconds": snapshot.process_data_stale_age_seconds,
            "socket_data_stale_age_seconds": snapshot.socket_data_stale_age_seconds,
        },
        "diagnostics": list(snapshot.diagnostics),
        "collector_health": global_collectors,
        "instances": instances,
    }


def _collector_degraded(items: Iterable[dict[str, Any]]) -> bool:
    return any(
        bool(item.get("error"))
        or int(item.get("consecutive_failures", 0) or 0) > 0
        or bool(item.get("budget_exceeded"))
        for item in items
    )


def _is_degraded(snapshot: MonitorSnapshot) -> bool:
    if snapshot.collection_duration_seconds > COLLECTION_BUDGET_SECONDS:
        return True
    if snapshot.diagnostics or _collector_degraded(_collector_items(snapshot)):
        return True
    for instance in snapshot.instances:
        if instance.diagnostics or getattr(instance, "unknown_event_types", {}):
            return True
        if _collector_degraded(_collector_items(instance)):
            return True
        for source in (instance.tui_session_log, instance.hook_events):
            if source.configured and (not source.readable or source.error):
                return True
    return False


def _status(snapshot: MonitorSnapshot) -> str:
    if not snapshot.instances:
        return "no_instances"
    return "degraded" if _is_degraded(snapshot) else "healthy"


def doctor_exit_code(snapshot: MonitorSnapshot) -> int:
    if not snapshot.instances:
        return 1
    return 2 if _is_degraded(snapshot) else 0


def render_doctor_json(snapshot: MonitorSnapshot) -> str:
    return json.dumps(doctor_dict(snapshot), ensure_ascii=False, indent=2)


def _format_capability(status: object) -> str:
    if isinstance(status, bool):
        return "yes" if status else "no"
    if isinstance(status, dict):
        mode = status.get("mode", "unavailable")
        source = status.get("source")
        confidence = status.get("confidence")
        details = ", ".join(str(item) for item in (source, confidence) if item)
        return f"{mode} ({details})" if details else str(mode)
    return str(status)


def _append_collectors(lines: list[str], collectors: list[dict[str, Any]], indent: str) -> None:
    if not collectors:
        lines.append(f"{indent}- unavailable")
        return
    for collector in collectors:
        name = collector.get("name", "unknown")
        duration = float(collector.get("duration_seconds", 0.0) or 0.0)
        last_success = collector.get("last_success_at")
        stale = collector.get("stale_age_seconds")
        failures = int(collector.get("consecutive_failures", 0) or 0)
        error = collector.get("error") or "-"
        over_budget = "yes" if collector.get("budget_exceeded") else "no"
        lines.append(
            f"{indent}- {name}: {duration:.3f}s; last_success={last_success}; "
            f"stale={stale}; failures={failures}; budget_exceeded={over_budget}; "
            f"error={error}"
        )


def render_doctor_text(snapshot: MonitorSnapshot) -> str:
    report = doctor_dict(snapshot)
    collection = report["collection"]
    lines = [
        f"codexnet doctor: {report['status']}",
        (
            f"采集: {collection['duration_seconds']:.3f}s / "
            f"{collection['budget_seconds']:.1f}s; "
            f"超时={'yes' if collection['budget_exceeded'] else 'no'}"
        ),
    ]
    for diagnostic in report["diagnostics"]:
        lines.append(f"诊断: {diagnostic}")
    lines.append("全局采集器:")
    _append_collectors(lines, report["collector_health"], "  ")

    if not report["instances"]:
        lines.append("实例: 未发现运行中的 Codex")
        return "\n".join(lines)

    for instance in report["instances"]:
        lines.extend(
            [
                "",
                f"实例 {instance['instance_id']} ({instance['discovery_method']})",
                f"  CODEX_HOME: {instance['paths']['codex_home']}",
                f"  SQLite home: {instance['paths']['sqlite_home']}",
                f"  state DB: {instance['paths']['state_db']}",
                f"  log DB: {instance['paths']['log_db']}",
                "  schema capabilities:",
            ]
        )
        for name, status in instance["schema_capabilities"].items():
            lines.append(f"    - {name}: {_format_capability(status)}")
        lines.append("  protocol capabilities:")
        capabilities = instance["protocol_capabilities"]
        if capabilities:
            for name, status in capabilities.items():
                lines.append(f"    - {name}: {_format_capability(status)}")
        else:
            lines.append("    - unavailable")
        rollout = instance["rollout"]
        lines.append(
            "  rollout: "
            f"cursor={rollout['cursor']}; identity={rollout['file_identity']}; "
            f"truncated={rollout['context_truncated']}; replaced={rollout['file_replaced']}; "
            f"partial_line={rollout['partial_line']}"
        )
        for activity in rollout["activity"]:
            lines.append(
                f"    - {activity['path']}: size={activity['stat_size']}; "
                f"bytes_read={activity['bytes_read']}; records="
                f"{activity['complete_record_count']}; ignored="
                f"{activity['ignored_record_count']}; normalized="
                f"{activity['normalized_count']}; partial={activity['partial_bytes']}; "
                f"replaced={activity['replaced']}; truncated={activity['truncated']}; "
                f"copy_truncated={activity['copy_truncated']}"
            )
        lines.append("  compact sources:")
        for name, source in instance["compact_sources"].items():
            state = "disabled"
            if source["configured"]:
                state = "readable" if source["readable"] else "unreadable"
            lines.append(
                f"    - {name}: {state}; source={source['source'] or '-'}; "
                f"last_probe={source['last_probe_at']}; "
                f"last_event={source['last_event_at']}; error={source['error'] or '-'}"
            )
        unknown = instance["unknown_events"]
        lines.append(
            f"  unknown events: {unknown['total']}; "
            f"parse_success_rate={unknown['parse_success_rate']}"
        )
        for name, count in unknown["types"].items():
            lines.append(f"    - {name}: {count}")
        for diagnostic in instance["diagnostics"]:
            lines.append(f"  诊断: {diagnostic}")
        lines.append("  collectors:")
        _append_collectors(lines, instance["collector_health"], "    ")
    return "\n".join(lines)

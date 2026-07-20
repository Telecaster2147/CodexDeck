"""Read-only collector and protocol capability diagnostics."""

from __future__ import annotations

import json
from typing import Any

from models import MonitorSnapshot
from presentation.projection import (
    collector_degraded,
    collector_items,
    instance_collector_items,
    primitive_value,
)


DOCTOR_SCHEMA_VERSION = 1
COLLECTION_BUDGET_SECONDS = 2.0


def _capability_rank(value: object) -> int:
    mode = value.get("mode", "unavailable") if isinstance(value, dict) else value
    return {"unavailable": 0, "derived": 1, "direct": 2}.get(str(mode), 0)


def _protocol_capabilities(instance: object) -> dict[str, Any]:
    direct = getattr(instance, "protocol_capabilities", None)
    value = primitive_value(direct)
    merged: dict[str, Any] = value if isinstance(value, dict) else {}
    for session in getattr(instance, "sessions", ()):
        value = primitive_value(getattr(session, "protocol_capabilities", None))
        if not isinstance(value, dict):
            continue
        for name, status in value.items():
            if name not in merged or _capability_rank(status) > _capability_rank(merged[name]):
                merged[name] = status
    return merged


def _rollout_diagnostics(instance: object) -> dict[str, Any]:
    return {
        "context_truncated": bool(getattr(instance, "rollout_context_truncated", False)),
        "activity": primitive_value(getattr(instance, "rollout_activity", [])),
        "cursor": primitive_value(getattr(instance, "rollout_cursor", None)),
        "file_identity": primitive_value(getattr(instance, "rollout_file_identity", None)),
        "file_replaced": primitive_value(getattr(instance, "rollout_file_replaced", None)),
        "partial_line": primitive_value(getattr(instance, "rollout_partial_line", None)),
    }


def doctor_dict(snapshot: MonitorSnapshot) -> dict[str, Any]:
    global_collectors = collector_items(snapshot)
    instances = []
    for instance in snapshot.instances:
        paths = instance.paths
        unknown = dict(getattr(instance, "unknown_event_types", {}) or {})
        instance_collectors = instance_collector_items(instance, global_collectors)
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
                "schema_capabilities": primitive_value(instance.capabilities),
                "protocol_capabilities": _protocol_capabilities(instance),
                "diagnostics": list(instance.diagnostics),
                "unknown_events": {
                    "types": unknown,
                    "total": sum(unknown.values()),
                    "parse_success_rate": primitive_value(
                        getattr(instance, "event_parse_success_rate", None)
                    ),
                },
                "rollout": _rollout_diagnostics(instance),
                "compact_sources": {
                    "tui_session_log": primitive_value(instance.tui_session_log),
                    "hook_events": primitive_value(instance.hook_events),
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


def _is_degraded(snapshot: MonitorSnapshot) -> bool:
    if snapshot.collection_duration_seconds > COLLECTION_BUDGET_SECONDS:
        return True
    if snapshot.diagnostics or collector_degraded(collector_items(snapshot)):
        return True
    for instance in snapshot.instances:
        if instance.diagnostics or getattr(instance, "unknown_event_types", {}):
            return True
        if collector_degraded(collector_items(instance)):
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


def _degraded_collectors(collectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        collector
        for collector in collectors
        if collector.get("error")
        or int(collector.get("consecutive_failures", 0) or 0) > 0
        or collector.get("budget_exceeded")
        or collector.get("stale_age_seconds") is not None
    ]


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
    degraded = _degraded_collectors(report["collector_health"])
    if degraded:
        lines.append("降级采集器:")
        _append_collectors(lines, degraded, "  ")

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
                "  完整 schema、协议、rollout 与 collector 矩阵: doctor --json",
            ]
        )
        rollout = instance["rollout"]
        if rollout["context_truncated"] or rollout["file_replaced"] or rollout["partial_line"]:
            lines.append(
                "  rollout 降级: "
                f"truncated={rollout['context_truncated']}; "
                f"replaced={rollout['file_replaced']}; partial_line={rollout['partial_line']}"
            )
        for name, source in instance["compact_sources"].items():
            if not source["configured"] or (source["readable"] and not source["error"]):
                continue
            lines.append(
                f"  compact source {name}: unreadable; error={source['error'] or '-'}"
            )
        unknown = instance["unknown_events"]
        if unknown["total"]:
            lines.append(
                f"  unknown events: {unknown['total']}; "
                f"parse_success_rate={unknown['parse_success_rate']}"
            )
            for name, count in unknown["types"].items():
                lines.append(f"    - {name}: {count}")
        for diagnostic in instance["diagnostics"]:
            lines.append(f"  诊断: {diagnostic}")
        degraded = _degraded_collectors(instance["collector_health"])
        if degraded:
            lines.append("  降级采集器:")
            _append_collectors(lines, degraded, "    ")
    return "\n".join(lines)

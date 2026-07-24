"""Shared, read-only projections for presentation adapters."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from diagnostics import diagnostic_text
from models import InstanceSnapshot
from presentation.privacy import public_value


def primitive_value(value: Any) -> Any:
    """Convert domain values to stable JSON-safe primitives."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {field.name: primitive_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): primitive_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive_value(item) for item in value]
    return value


def collector_items(owner: object) -> list[dict[str, Any]]:
    """Project collector health from snapshots or compatible fixtures."""

    raw = getattr(owner, "collector_health", ()) or ()
    if isinstance(raw, dict):
        raw = [
            {**value, "name": value.get("name", key)} if isinstance(value, dict) else value
            for key, value in raw.items()
        ]
    result: list[dict[str, Any]] = []
    for item in raw:
        data = public_value(item)
        if isinstance(data, dict):
            result.append(data)
    return result


def instance_collector_items(
    instance: InstanceSnapshot,
    global_collectors: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return the explicit or inherited collector view for one instance."""

    items = collector_items(instance)
    if items:
        return items
    return [
        item
        for item in global_collectors
        if str(item.get("name", "")) in {"process", "socket"}
        or str(item.get("name", "")).endswith(f":{instance.instance_id}")
    ]


def collector_degraded(items: Iterable[dict[str, Any]]) -> bool:
    return any(
        bool(item.get("error"))
        or int(item.get("consecutive_failures", 0) or 0) > 0
        or bool(item.get("budget_exceeded"))
        for item in items
    )


def instance_quality_issues(instance: InstanceSnapshot) -> list[str]:
    """Build the shared concise data-quality projection for an instance."""

    issues: list[str] = []
    for collector in instance.collector_health:
        if not collector.error and collector.stale_age_seconds is None:
            continue
        detail = collector.error or f"陈旧 {collector.stale_age_seconds:.1f}s"
        issues.append(f"{collector.name}: {detail}")
    issues.extend(diagnostic_text(item) for item in instance.diagnostics)
    issues.extend(
        f"{result.source}: {result.status.value} ({result.error_code or 'partial_result'})"
        for result in instance.adapter_results
        if result.status.value in {"failed", "incomplete"}
    )
    if instance.rollout_context_truncated:
        issues.append("rollout 初始读取已截断")
    if instance.process_data_stale_age_seconds is not None:
        issues.append(f"进程数据已陈旧 {instance.process_data_stale_age_seconds:.1f}s")
    if instance.socket_data_stale_age_seconds is not None:
        issues.append(f"socket 数据已陈旧 {instance.socket_data_stale_age_seconds:.1f}s")
    issues.extend(
        f"未解析 {event_type} × {count}"
        for event_type, count in instance.unknown_event_types.items()
    )
    family_counters = instance.protocol_family_counters
    dropped = family_counters.get("unknown_dropped_family_count", 0) + family_counters.get(
        "shape_dropped_family_count", 0
    )
    if dropped:
        issues.append(f"协议族计数已截断 × {dropped}")
    return issues

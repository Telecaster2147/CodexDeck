"""Read-only collector and protocol capability diagnostics."""

from __future__ import annotations

import json
from typing import Any

from codex.compatibility import compatibility_stats
from diagnostics import (
    diagnostic_text,
    normalize_diagnostic,
    observation_degraded,
    snapshot_diagnostics,
)
from models import MonitorSnapshot
from presentation.projection import (
    collector_degraded,
    collector_items,
    instance_collector_items,
)
from presentation.privacy import public_rollout_activity, public_value


DOCTOR_SCHEMA_VERSION = 1
COLLECTION_BUDGET_SECONDS = 2.0


def _capability_rank(value: object) -> int:
    mode = value.get("mode", "unavailable") if isinstance(value, dict) else value
    return {"unavailable": 0, "derived": 1, "direct": 2}.get(str(mode), 0)


def _protocol_capabilities(instance: object) -> dict[str, Any]:
    direct = getattr(instance, "protocol_capabilities", None)
    value = public_value(direct)
    merged: dict[str, Any] = value if isinstance(value, dict) else {}
    for session in getattr(instance, "sessions", ()):
        value = public_value(getattr(session, "protocol_capabilities", None))
        if not isinstance(value, dict):
            continue
        for name, status in value.items():
            if name not in merged or _capability_rank(status) > _capability_rank(merged[name]):
                merged[name] = status
    return merged


def _rollout_diagnostics(instance: object) -> dict[str, Any]:
    return {
        "context_truncated": bool(getattr(instance, "rollout_context_truncated", False)),
        "activity": public_rollout_activity(getattr(instance, "rollout_activity", [])),
        "cursor": public_value(getattr(instance, "rollout_cursor", None)),
        "file_identity": public_value(getattr(instance, "rollout_file_identity", None)),
        "file_replaced": public_value(getattr(instance, "rollout_file_replaced", None)),
        "partial_line": public_value(getattr(instance, "rollout_partial_line", None)),
    }


def doctor_dict(snapshot: MonitorSnapshot) -> dict[str, Any]:
    global_collectors = collector_items(snapshot)
    packet_enabled = any(item.get("name") == "packet" for item in global_collectors)
    instances = []
    for instance in snapshot.instances:
        paths = instance.paths
        unknown = dict(getattr(instance, "unknown_event_types", {}) or {})
        shape_families = dict(getattr(instance, "protocol_shape_families", {}) or {})
        family_counters = dict(getattr(instance, "protocol_family_counters", {}) or {})
        shape_total = sum(shape_families.values())
        unknown_total = sum(unknown.values())
        instance_collectors = instance_collector_items(instance, global_collectors)
        instances.append(
            {
                "instance_id": instance.instance_id,
                "discovery_method": instance.discovery_method,
                "process_discovery": [
                    {
                        "pid": process.pid,
                        "role": process.role,
                        "method": process.discovery_method,
                        "confidence": process.discovery_confidence.value,
                        "evidence": list(process.discovery_evidence),
                    }
                    for process in instance.processes
                ],
                "paths": {
                    "codex_home": str(paths.codex_home),
                    "sqlite_home": str(paths.sqlite_home),
                    "state_db": str(paths.state_db),
                    "log_db": str(paths.log_db),
                    "session_index": str(paths.session_index),
                    "sessions_dir": str(paths.sessions_dir),
                },
                "schema_capabilities": public_value(instance.capabilities),
                "adapter_results": public_value(instance.adapter_results),
                "protocol_capabilities": _protocol_capabilities(instance),
                "diagnostics": public_value(
                    tuple(
                        normalize_diagnostic(item, source="instance")
                        for item in instance.diagnostics
                    )
                ),
                "unknown_events": {
                    "types": unknown,
                    "total": unknown_total,
                    "rate": unknown_total / shape_total if shape_total else 0.0,
                    "parse_success_rate": public_value(
                        getattr(instance, "event_parse_success_rate", None)
                    ),
                },
                "protocol_compatibility": {
                    "status": (
                        "degraded"
                        if unknown_total
                        else "matched"
                        if shape_families
                        else "unobserved"
                    ),
                    "shape_families": shape_families,
                    "family_counters": family_counters,
                    **compatibility_stats(),
                },
                "protocol_uncertainty": [
                    {
                        "session_id": session.session_id,
                        "scope": session.protocol_uncertainty_scope,
                        "reason": session.protocol_uncertainty_reason,
                        "lifecycle_confidence": session.lifecycle_confidence.value,
                        "attention_confidence": session.attention_confidence.value,
                    }
                    for session in instance.sessions
                    if session.protocol_uncertain
                ],
                "state_completeness": [
                    {
                        "session_id": session.session_id,
                        "axes": public_value(session.completeness),
                        "incomplete_axes": list(session.completeness.incomplete_axes),
                    }
                    for session in instance.sessions
                ],
                "clock_uncertainty": [
                    {
                        "session_id": session.session_id,
                        "source": assessment.source,
                        "clock_domain": assessment.clock_domain,
                        "source_timestamp": assessment.source_timestamp,
                        "observed_at": assessment.observed_at,
                        "adjudicated_at": assessment.adjudicated_at,
                        "reason": assessment.reason,
                    }
                    for session in instance.sessions
                    for assessment in session.clock_assessments
                ],
                "rollout": _rollout_diagnostics(instance),
                "compact_sources": {
                    "tui_session_log": public_value(instance.tui_session_log),
                    "hook_events": public_value(instance.hook_events),
                },
                "collector_health": instance_collectors,
            }
        )

    duration = snapshot.collection_duration_seconds
    return public_value(
        {
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
            "observer": public_value(snapshot.observer),
            "temporal": public_value(snapshot.temporal),
            "discovery": public_value(snapshot.discovery),
            "packet_inspection": {
                "enabled": packet_enabled,
                "capture_scope": "current_allowlisted_codex_socket_flows",
                "prefilter_stage": "before_tls_reassembly",
                "requires": "CAP_NET_RAW_or_root",
                "namespace_visibility": "AF_PACKET_can_observe_namespace_frames",
                "known_gap": "client_hello_before_first_fresh_socket_snapshot_may_be_missed",
            },
            "diagnostics": public_value(snapshot_diagnostics(snapshot)),
            "history": public_value(snapshot.history),
            "collector_health": global_collectors,
            "instances": instances,
        }
    )


def _is_degraded(snapshot: MonitorSnapshot) -> bool:
    if observation_degraded(snapshot):
        return True
    if snapshot.observer.degraded:
        return True
    if snapshot.collection_duration_seconds > COLLECTION_BUDGET_SECONDS:
        return True
    if snapshot.diagnostics or collector_degraded(collector_items(snapshot)):
        return True
    if snapshot.discovery.unresolved:
        return True
    for instance in snapshot.instances:
        if (
            instance.diagnostics
            or getattr(instance, "unknown_event_types", {})
            or getattr(instance, "protocol_family_counters", {}).get(
                "unknown_dropped_family_count", 0
            )
            or getattr(instance, "protocol_family_counters", {}).get(
                "shape_dropped_family_count", 0
            )
        ):
            return True
        if any(session.protocol_uncertain for session in instance.sessions):
            return True
        if any(session.clock_uncertain for session in instance.sessions):
            return True
        if any(session.completeness.incomplete_axes for session in instance.sessions):
            return True
        if any(
            activity.get("backlog_bytes")
            or activity.get("gap_count")
            or activity.get("metadata_backfill_dropped")
            or activity.get("terminal_parser_evictions")
            or activity.get("stream_uncertain")
            for activity in instance.rollout_activity
        ):
            return True
        if collector_degraded(collector_items(instance)):
            return True
        for source in (instance.tui_session_log, instance.hook_events):
            if source.configured and (not source.readable or source.error):
                return True
            if source.backlog_bytes or source.gap_count:
                return True
            if source.stream_uncertain:
                return True
            if source.configured and source.source_authenticity.value != "high":
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
        f"codexdeck doctor: {report['status']}",
        (
            f"采集: {collection['duration_seconds']:.3f}s / "
            f"{collection['budget_seconds']:.1f}s; "
            f"超时={'yes' if collection['budget_exceeded'] else 'no'}"
        ),
        (
            "Packet inspection: "
            f"enabled={'yes' if report['packet_inspection']['enabled'] else 'no'}; "
            "scope=current allowlisted Codex socket flows; "
            "prefilter=before TLS reassembly; requires=CAP_NET_RAW or root; "
            "首次新鲜 socket 快照前的 ClientHello 可能缺失"
        ),
        (
            "进程发现: "
            f"candidates={report['discovery']['candidates']}; "
            f"confirmed={report['discovery']['confirmed']}; "
            f"rejected={report['discovery']['rejected']}; "
            f"unresolved={report['discovery']['unresolved']}"
        ),
    ]
    for diagnostic in report["diagnostics"]:
        lines.append(f"诊断: {diagnostic_text(diagnostic)}")
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
        for process in instance["process_discovery"]:
            lines.append(
                "  process discovery: "
                f"pid={process['pid']}; role={process['role']}; "
                f"method={process['method']}; confidence={process['confidence']}; "
                f"evidence={','.join(process['evidence']) or '-'}"
            )
        rollout = instance["rollout"]
        if rollout["context_truncated"] or rollout["file_replaced"] or rollout["partial_line"]:
            lines.append(
                "  rollout 降级: "
                f"truncated={rollout['context_truncated']}; "
                f"replaced={rollout['file_replaced']}; partial_line={rollout['partial_line']}"
            )
        for activity in rollout["activity"]:
            if not activity.get("backlog_bytes") and not activity.get("gap_count"):
                continue
            lines.append(
                "  rollout ingress: "
                f"backlog={activity.get('backlog_bytes', 0)}; "
                f"backlog_records>={activity.get('backlog_records_lower_bound', 0)}; "
                f"age={activity.get('backlog_age_seconds')}; "
                f"budget_exceeded={activity.get('budget_exceeded')}; "
                f"gaps={activity.get('gap_count', 0)}; "
                f"skipped={activity.get('skipped_bytes', 0)}; "
                f"reason={activity.get('gap_reason') or '-'}"
            )
        for name, source in instance["compact_sources"].items():
            if not source["configured"]:
                continue
            if (
                source["readable"]
                and not source["error"]
                and not source["backlog_bytes"]
                and not source["gap_count"]
            ):
                continue
            lines.append(
                f"  compact source {name}: readable={source['readable']}; "
                f"error={source['error'] or '-'}; backlog={source['backlog_bytes']}; "
                f"backlog_records>={source['backlog_records_lower_bound']}; "
                f"age={source['backlog_age_seconds']}; "
                f"budget_exceeded={source['budget_exceeded']}; gaps={source['gap_count']}; "
                f"skipped={source['skipped_bytes']}; reason={source['gap_reason'] or '-'}"
            )
        unknown = instance["unknown_events"]
        if unknown["total"]:
            lines.append(
                f"  unknown events: {unknown['total']}; "
                f"parse_success_rate={unknown['parse_success_rate']}"
            )
            for name, count in unknown["types"].items():
                lines.append(f"    - {name}: {count}")
        compatibility = instance["protocol_compatibility"]
        if compatibility["status"] != "unobserved":
            counters = compatibility["family_counters"]
            lines.append(
                f"  protocol compatibility: {compatibility['status']}; "
                f"families={len(compatibility['shape_families'])}; "
                f"unknown_other={counters.get('unknown_other', 0)}; "
                f"shape_other={counters.get('shape_other', 0)}; "
                f"dropped_family_count="
                f"{counters.get('unknown_dropped_family_count', 0) + counters.get('shape_dropped_family_count', 0)}"
            )
        for uncertainty in instance["protocol_uncertainty"]:
            lines.append(
                "  protocol uncertainty: "
                f"session={uncertainty['session_id']}; scope={uncertainty['scope']}; "
                f"lifecycle_confidence={uncertainty['lifecycle_confidence']}"
            )
        for completeness in instance["state_completeness"]:
            if not completeness["incomplete_axes"]:
                continue
            lines.append(
                "  state completeness: "
                f"session={completeness['session_id']}; incomplete="
                f"{','.join(completeness['incomplete_axes'])}"
            )
        for uncertainty in instance["clock_uncertainty"]:
            lines.append(
                "  clock uncertainty: "
                f"session={uncertainty['session_id']}; source={uncertainty['source']}; "
                f"domain={uncertainty['clock_domain']}; reason={uncertainty['reason']}; "
                f"adjudicated_at={uncertainty['adjudicated_at']:.6f}"
            )
        for diagnostic in instance["diagnostics"]:
            lines.append(f"  诊断: {diagnostic_text(diagnostic)}")
        degraded = _degraded_collectors(instance["collector_health"])
        if degraded:
            lines.append("  降级采集器:")
            _append_collectors(lines, degraded, "    ")
    return "\n".join(lines)

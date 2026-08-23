"""Auditable ownership registry for MonitorEngine mutable state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineStateOwner:
    owner: str
    fields: tuple[str, ...]
    lifecycle: str
    publication_boundary: str


ENGINE_STATE_OWNERS = (
    EngineStateOwner(
        "engine configuration",
        (
            "interval",
            "idle_threshold",
            "selected_pids",
            "selected_homes",
            "pinned_session_key",
        ),
        "MonitorEngine process lifetime",
        "values are copied into options or published snapshot metadata",
    ),
    EngineStateOwner(
        "host collector stages",
        (
            "proc",
            "discovery",
            "sockets",
            "process_activity",
            "last_discovery",
            "last_socket_by_pid",
            "discovery_stale_since",
            "socket_stale_since",
            "previous_sockets",
            "stall_windows",
        ),
        "replaced only by a complete full-sample stage",
        "collector results are copied into InstanceSnapshot",
    ),
    EngineStateOwner(
        "Codex evidence readers",
        (
            "rollouts",
            "codex_configs",
            "machine",
            "terminals",
            "terminal_files",
            "store_cache",
            "log_cursors",
            "log_process_keys",
            "session_index_cache",
            "task_cache",
            "rollout_path_cache",
        ),
        "incremental and bounded; pruned after each complete full sample",
        "only immutable summaries and copied collections cross publication",
    ),
    EngineStateOwner(
        "identity and session retention",
        (
            "live_sessions",
            "retired_sessions",
            "instance_templates",
            "identity_registry",
        ),
        "current identity generation plus bounded lookback",
        "published sessions are copy-on-write and never reused as mutable caches",
    ),
    EngineStateOwner(
        "snapshot publication",
        ("collectors", "snapshot_publisher"),
        "MonitorEngine process lifetime with per-sample generation",
        "SnapshotPublisher is the sole complete-snapshot publication boundary",
    ),
)


def engine_state_fields() -> tuple[str, ...]:
    return tuple(field for owner in ENGINE_STATE_OWNERS for field in owner.fields)

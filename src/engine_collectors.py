"""Bounded process-discovery and socket collector stages."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, replace

from codex.paths import ResolvedInstance
from codex.processes import DiscoveryResult
from models import InstanceIdentity, ProcessInfo, SocketInfo
from utils import CommandError


@dataclass(frozen=True)
class DiscoveryStage:
    result: DiscoveryResult
    by_instance: dict[InstanceIdentity, list[ProcessInfo]]
    resolved_by_instance: dict[InstanceIdentity, ResolvedInstance]
    identity_collisions: set[InstanceIdentity]
    active_process_keys: set[str]
    stale: bool


@dataclass(frozen=True)
class SocketStage:
    by_pid: dict[int, list[SocketInfo]]
    stale: bool


class CollectorStagesMixin:
    """Run host collectors while preserving the last complete result on failure."""

    def _collect_discovery_stage(
        self,
        now_monotonic: float,
        diagnostics: list[str],
    ) -> DiscoveryStage:
        process_started = time.monotonic()
        try:
            discovery = self.discovery.discover(self.selected_pids, self.selected_homes)
            self.collectors.record(
                "process",
                process_started,
                command_result=discovery.command_result,
            )
            self.last_discovery = discovery
            self.discovery_stale_since = None
            discovery_stale = False
        except CommandError as exc:
            self.collectors.record("process", process_started, exc)
            if self.last_discovery is None:
                raise RuntimeError(str(exc)) from exc
            discovery = self.last_discovery
            if self.discovery_stale_since is None:
                self.discovery_stale_since = now_monotonic
            discovery_stale = True
            stale_age = now_monotonic - self.discovery_stale_since
            diagnostics.append(f"进程列表已过期 {stale_age:.1f}s：{exc}")

        by_instance: dict[InstanceIdentity, list[ProcessInfo]] = defaultdict(list)
        resolved_by_instance: dict[InstanceIdentity, ResolvedInstance] = {}
        collisions: set[InstanceIdentity] = set()
        for process in discovery.processes:
            resolved = discovery.instances[process.instance_id]
            identity = process.instance_identity or resolved.identity
            storage_id, collided = self.identity_registry.register(identity)
            normalized = replace(process, instance_id=storage_id, instance_identity=identity)
            by_instance[identity].append(normalized)
            resolved_by_instance[identity] = resolved
            if collided:
                collisions.add(identity)
        return DiscoveryStage(
            result=discovery,
            by_instance=dict(by_instance),
            resolved_by_instance=resolved_by_instance,
            identity_collisions=collisions,
            active_process_keys={process.stable_key for process in discovery.processes},
            stale=discovery_stale,
        )

    def _collect_socket_stage(
        self,
        discovery: DiscoveryResult,
        now_monotonic: float,
        diagnostics: list[str],
    ) -> SocketStage:
        pids = {process.pid for process in discovery.processes}
        socket_started = time.monotonic()
        try:
            socket_by_pid = self.sockets.snapshot(pids)
            self.collectors.record(
                "socket",
                socket_started,
                command_result=getattr(self.sockets, "last_command_result", None),
            )
            self.last_socket_by_pid = socket_by_pid
            sockets_stale = False
            self.socket_stale_since = None
        except CommandError as exc:
            self.collectors.record("socket", socket_started, exc)
            socket_by_pid = self.last_socket_by_pid
            sockets_stale = True
            if self.socket_stale_since is None:
                self.socket_stale_since = now_monotonic
            stale_age = now_monotonic - self.socket_stale_since
            diagnostics.append(f"TCP 快照已过期 {stale_age:.1f}s：{exc}")

        return SocketStage(by_pid=socket_by_pid, stale=sockets_stale)

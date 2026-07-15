"""Persistent multi-instance sampling engine."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .codex.events import normalize_log
from .codex.paths import ProcReader, open_rollout_paths
from .codex.processes import DiscoveryResult, ProcessDiscovery
from .codex.rollout import RolloutReader, latest_user_task, rollout_identity
from .codex.state_store import StateStore
from .models import (
    CodexPaths,
    Confidence,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessInfo,
    SessionHealth,
)
from .network.classifier import assess_process_network
from .network.sockets import SocketCollector
from .state_machine import PROGRESS_KINDS, SessionStateMachine
from .utils import CommandError, compact_path, one_line


class MonitorEngine:
    def __init__(
        self,
        interval: float,
        idle_threshold: float,
        event_lookback: int,
        selected_pids: set[int] | None = None,
        selected_homes: set[Path] | None = None,
        discovery: ProcessDiscovery | None = None,
        sockets: SocketCollector | None = None,
        proc: ProcReader | None = None,
    ) -> None:
        self.interval = interval
        self.idle_threshold = idle_threshold
        self.selected_pids = selected_pids
        self.selected_homes = selected_homes
        self.proc = proc or ProcReader()
        self.discovery = discovery or ProcessDiscovery(proc=self.proc)
        self.sockets = sockets or SocketCollector()
        self.rollouts = RolloutReader()
        self.machine = SessionStateMachine(event_lookback)
        self.previous_sockets: dict[str, list] = {}
        self.stall_windows: dict[str, int] = defaultdict(int)
        self.log_cursors: dict[str, int] = defaultdict(int)
        self.log_process_keys: dict[str, set[str]] = {}
        self.session_index_cache: dict[str, tuple[int, int, dict[str, str]]] = {}
        self.task_cache: dict[str, tuple[int, int, int, str]] = {}
        self.rollout_path_cache: dict[str, tuple[Path | None, str]] = {}
        self.store_cache: dict[str, StateStore] = {}
        self.live_sessions: dict[str, SessionHealth] = {}
        self.retired_sessions: dict[str, tuple[SessionHealth, float]] = {}
        self.instance_templates: dict[str, InstanceSnapshot] = {}
        self.pinned_session_key = ""
        self.last_discovery: DiscoveryResult | None = None
        self.last_socket_by_pid: dict[int, list] = {}
        self.discovery_stale_since: float | None = None
        self.socket_stale_since: float | None = None

    def baseline(self) -> None:
        """Capture a cheap first socket baseline before a completed sample window."""

        try:
            discovery = self.discovery.discover(self.selected_pids, self.selected_homes)
            current = self.sockets.snapshot({process.pid for process in discovery.processes})
        except CommandError:
            return
        self.last_discovery = discovery
        self.last_socket_by_pid = current
        self.previous_sockets = {
            process.stable_key: current.get(process.pid, []) for process in discovery.processes
        }

    def sample(self) -> MonitorSnapshot:
        started = time.monotonic()
        now_monotonic = started
        diagnostics: list[str] = []
        try:
            discovery = self.discovery.discover(self.selected_pids, self.selected_homes)
            self.last_discovery = discovery
            self.discovery_stale_since = None
        except CommandError as exc:
            if self.last_discovery is None:
                raise RuntimeError(str(exc)) from exc
            discovery = self.last_discovery
            if self.discovery_stale_since is None:
                self.discovery_stale_since = now_monotonic
            stale_age = now_monotonic - self.discovery_stale_since
            diagnostics.append(f"进程列表已过期 {stale_age:.1f}s：{exc}")

        pids = {process.pid for process in discovery.processes}
        try:
            socket_by_pid = self.sockets.snapshot(pids)
            self.last_socket_by_pid = socket_by_pid
            sockets_stale = False
            self.socket_stale_since = None
        except CommandError as exc:
            socket_by_pid = self.last_socket_by_pid
            sockets_stale = True
            if self.socket_stale_since is None:
                self.socket_stale_since = now_monotonic
            stale_age = now_monotonic - self.socket_stale_since
            diagnostics.append(f"TCP 快照已过期 {stale_age:.1f}s：{exc}")

        by_instance: dict[str, list[ProcessInfo]] = defaultdict(list)
        for process in discovery.processes:
            by_instance[process.instance_id].append(process)

        instance_snapshots: list[InstanceSnapshot] = []
        active_session_keys: set[str] = set()
        active_rollouts: set[str] = set()
        active_process_keys = {process.stable_key for process in discovery.processes}

        for instance_id, processes in by_instance.items():
            resolved = discovery.instances[instance_id]
            instance_diagnostics: list[str] = []
            instance_rollouts: set[str] = set()
            if resolved.method == "unresolved":
                instance_diagnostics.append("进程环境与活动文件不可读，路径按默认值推测")
            store = self._store_for(instance_id, resolved.paths)
            process_keys = {process.stable_key for process in processes}
            if self.log_process_keys.get(instance_id) != process_keys:
                self.log_cursors[instance_id] = 0
                self.log_process_keys[instance_id] = process_keys
            if not store.capabilities.threads:
                instance_diagnostics.append("state DB 不可用或缺少 threads 表")
            if not store.capabilities.logs:
                instance_diagnostics.append("logs DB 不可用，重试诊断可能不完整")

            sessions_by_pid: dict[int, str] = {}
            rollout_by_pid: dict[int, Path | None] = {}
            for process in processes:
                if process.role != "session":
                    continue
                rollout_path, session_id = self._fallback_rollout(
                    process, resolved.paths.sessions_dir
                )
                rollout_by_pid[process.pid] = rollout_path
                if session_id:
                    sessions_by_pid[process.pid] = session_id
            unresolved = [
                process.pid
                for process in processes
                if process.role == "session" and process.pid not in sessions_by_pid
            ]
            if unresolved:
                sessions_by_pid.update(
                    store.active_threads(
                        unresolved,
                        cutoff=int(time.time()) - 21600,
                    )
                )
            records = store.threads(sessions_by_pid.values())
            names = self._session_names(instance_id, resolved.paths.session_index)
            enriched: list[ProcessInfo] = []
            for process in processes:
                session_id = sessions_by_pid.get(process.pid, "")
                record = records.get(session_id)
                rollout_path = rollout_by_pid.get(process.pid)
                if not rollout_path and record and record.rollout_path:
                    rollout_path = Path(record.rollout_path)
                if not rollout_path:
                    rollout_path, fallback_id = self._fallback_rollout(
                        process,
                        resolved.paths.sessions_dir,
                    )
                    session_id = session_id or fallback_id
                    if session_id and record is None:
                        record = store.threads([session_id]).get(session_id)
                title = names.get(session_id, "") or (record.title if record else "")
                fallback_task = (record.preview or record.first_user_message) if record else ""
                task = self._latest_task(rollout_path) if rollout_path else ""
                process = replace(
                    process,
                    cwd=process.cwd or (record.cwd if record else ""),
                    session_id=session_id,
                    session_title=self._bounded(one_line(title), 120),
                    current_task=self._bounded(one_line(task or fallback_task), 240),
                    model=record.model if record else "",
                    reasoning_effort=record.reasoning_effort if record else "",
                    rollout_path=str(rollout_path or ""),
                )
                enriched.append(process)
                if process.rollout_path:
                    active_rollouts.add(process.rollout_path)
                    instance_rollouts.add(process.rollout_path)

            session_for_pid = {
                process.pid: process.session_id
                for process in enriched
                if process.session_id
            }
            events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
            cutoff = int(time.time()) - self.machine.lookback_seconds
            logs = store.logs_since(
                [process.pid for process in enriched], self.log_cursors[instance_id], cutoff
            )
            if logs:
                self.log_cursors[instance_id] = max(record.log_id for record in logs)
            for record in logs:
                session_id = record.thread_id
                if not session_id:
                    match = re.match(r"pid:(\d+):", record.process_uuid)
                    session_id = session_for_pid.get(int(match.group(1)), "") if match else ""
                if session_id:
                    events_by_session[session_id].extend(normalize_log(record))

            sessions = []
            for process in enriched:
                if process.role != "session" or not process.session_id:
                    continue
                incoming = events_by_session.get(process.session_id, [])
                if process.rollout_path:
                    incoming.extend(self.rollouts.read(Path(process.rollout_path)))
                session_key = f"{instance_id}:{process.session_id}"
                if session_key in self.retired_sessions:
                    incoming.append(
                        NormalizedEvent(
                            timestamp=time.time(),
                            kind="PROCESS_RESUMED",
                            summary="进程已重新启动",
                            detail=f"当前 PID {process.pid}",
                            source="process",
                            confidence=Confidence.HIGH,
                            source_id=f"process-resumed:{process.stable_key}",
                        )
                    )
                active_session_keys.add(session_key)
                before = self.previous_sockets.get(process.stable_key, [])
                after = socket_by_pid.get(process.pid, [])
                network = assess_process_network(before, after, self.idle_threshold)
                if sockets_stale:
                    network.stale = True
                    network.stale_age_seconds = (
                        now_monotonic - self.socket_stale_since
                        if self.socket_stale_since is not None
                        else 0.0
                    )
                    network.reason = f"{network.reason}（TCP 数据已过期）"
                recent_progress = any(
                    event.kind in PROGRESS_KINDS
                    and event.timestamp >= time.time() - self.interval * 1.5
                    for event in incoming
                )
                if network.state == NetworkState.SUSPECT and not recent_progress:
                    self.stall_windows[process.stable_key] += 1
                    if self.stall_windows[process.stable_key] >= 2:
                        network.state = NetworkState.STALLED
                        windows = self.stall_windows[process.stable_key]
                        network.reason = f"连续 {windows} 个窗口异常：{network.reason}"
                else:
                    self.stall_windows[process.stable_key] = 0
                    if network.state == NetworkState.SUSPECT and recent_progress:
                        network.state = NetworkState.IDLE
                        network.reason = "TCP 指标异常，但 Codex 协议仍有进展"
                self.machine.ingest(session_key, incoming)
                session = self.machine.derive(session_key, process, network)
                previous = self.live_sessions.get(session_key)
                recovery_states = {
                    "SUSPECT",
                    "RECONNECTING",
                    "TRANSPORT_FALLBACK",
                }
                was_recovering = (
                    session.recovery.value in recovery_states
                    or bool(previous and previous.recovery.value in recovery_states)
                )
                if network.state == NetworkState.ACTIVE and was_recovering:
                    recovered = NormalizedEvent(
                        timestamp=time.time(),
                        kind="RECOVERED",
                        summary="连接已恢复",
                        detail="TCP 传输重新出现进展",
                        source="detector",
                        confidence=Confidence.MEDIUM,
                        source_id=(
                            f"network-recovered:{process.stable_key}:"
                            f"{int(time.time() * 1000)}"
                        ),
                    )
                    self.machine.ingest(session_key, [recovered])
                    session = self.machine.derive(session_key, process, network)
                sessions.append(session)
                self.previous_sockets[process.stable_key] = after

            instance_snapshots.append(
                InstanceSnapshot(
                    instance_id=instance_id,
                    paths=resolved.paths,
                    display_codex_home=compact_path(resolved.paths.codex_home),
                    display_sqlite_home=compact_path(resolved.paths.sqlite_home),
                    discovery_method=resolved.method,
                    capabilities=store.capabilities,
                    diagnostics=instance_diagnostics,
                    unknown_event_types=self.rollouts.unknown_counts(instance_rollouts),
                    rollout_context_truncated=(
                        self.rollouts.has_truncated_context(instance_rollouts)
                    ),
                    processes=enriched,
                    sessions=sessions,
                )
            )

        self._retain_exited_sessions(instance_snapshots, active_session_keys)

        self.previous_sockets = {
            key: value for key, value in self.previous_sockets.items() if key in active_process_keys
        }
        self.stall_windows = defaultdict(
            int,
            {
                key: value
                for key, value in self.stall_windows.items()
                if key in active_process_keys
            },
        )
        active_instances = set(by_instance)
        self.log_cursors = defaultdict(
            int, {key: value for key, value in self.log_cursors.items() if key in active_instances}
        )
        self.log_process_keys = {
            key: value
            for key, value in self.log_process_keys.items()
            if key in active_instances
        }
        for instance_id in set(self.store_cache) - active_instances:
            self.store_cache.pop(instance_id).close()
        self.rollouts.prune(active_rollouts)
        self.task_cache = {
            path: value for path, value in self.task_cache.items() if path in active_rollouts
        }
        retained_keys = set(self.retired_sessions)
        self.machine.prune(active_session_keys | retained_keys)
        self.rollout_path_cache = {
            key: value
            for key, value in self.rollout_path_cache.items()
            if key in active_process_keys
        }
        return MonitorSnapshot(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            interval_seconds=self.interval,
            instances=sorted(instance_snapshots, key=lambda item: item.display_codex_home),
            collection_duration_seconds=time.monotonic() - started,
            diagnostics=diagnostics,
            process_data_stale_age_seconds=(
                now_monotonic - self.discovery_stale_since
                if self.discovery_stale_since is not None
                else None
            ),
            socket_data_stale_age_seconds=(
                now_monotonic - self.socket_stale_since
                if self.socket_stale_since is not None
                else None
            ),
        )

    def _fallback_rollout(
        self,
        process: ProcessInfo,
        sessions_dir: Path,
    ) -> tuple[Path | None, str]:
        cached = self.rollout_path_cache.get(process.stable_key)
        if cached and cached[0] is not None and cached[0].exists():
            return cached
        candidates: list[tuple[bool, int, Path, str]] = []
        for path in open_rollout_paths(process.pid, sessions_dir, self.proc):
            session_id, is_subagent = rollout_identity(path)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            candidates.append((not is_subagent, size, path, session_id))
        if not candidates:
            result = (None, "")
            self.rollout_path_cache[process.stable_key] = result
            return result
        _, _, path, session_id = max(candidates, key=lambda item: (item[0], item[1]))
        result = (path, session_id)
        self.rollout_path_cache[process.stable_key] = result
        return result

    def _store_for(self, instance_id: str, paths: CodexPaths) -> StateStore:
        store = self.store_cache.get(instance_id)
        if store and store.paths == paths and store.is_current():
            return store
        if store:
            store.close()
            self.log_cursors[instance_id] = 0
        store = StateStore(paths)
        self.store_cache[instance_id] = store
        return store

    def _latest_task(self, path: Path) -> str:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return ""
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = self.task_cache.get(key)
        if cached and cached[:3] == signature:
            return cached[3]
        task = latest_user_task(path)
        self.task_cache[key] = (*signature, task)
        return task

    def _retain_exited_sessions(
        self,
        snapshots: list[InstanceSnapshot],
        active_keys: set[str],
    ) -> None:
        """Keep recently exited sessions visible without treating them as unhealthy."""

        now = time.time()
        current: dict[str, SessionHealth] = {}
        snapshot_by_instance = {snapshot.instance_id: snapshot for snapshot in snapshots}
        for snapshot in snapshots:
            self.instance_templates[snapshot.instance_id] = replace(
                snapshot,
                processes=[],
                sessions=[],
            )
            for session in snapshot.sessions:
                key = f"{session.instance_id}:{session.session_id}"
                current[key] = session
                self.retired_sessions.pop(key, None)

        for key, session in self.live_sessions.items():
            if key in active_keys:
                continue
            exited = NormalizedEvent(
                timestamp=now,
                kind="PROCESS_EXITED",
                summary="进程已退出",
                detail=f"PID {session.process.pid} 已结束",
                source="process",
                confidence=Confidence.HIGH,
                source_id=f"process-exited:{session.process.stable_key}",
            )
            self.machine.ingest(key, [exited])
            network = NetworkEvidence(
                state=NetworkState.CLOSED,
                reason="Codex 进程已退出",
            )
            retained = self.machine.derive(key, session.process, network, now)
            self.retired_sessions[key] = (retained, now)

        expiry = now - self.machine.lookback_seconds
        self.retired_sessions = {
            key: value
            for key, value in self.retired_sessions.items()
            if (value[1] >= expiry or key == self.pinned_session_key)
            and key not in active_keys
        }
        for retained, _ in self.retired_sessions.values():
            snapshot = snapshot_by_instance.get(retained.instance_id)
            if snapshot is None:
                template = self.instance_templates.get(retained.instance_id)
                if template is None:
                    continue
                snapshot = replace(template, sessions=[])
                snapshots.append(snapshot)
                snapshot_by_instance[retained.instance_id] = snapshot
            snapshot.sessions.append(retained)
        self.live_sessions = current
        retained_instances = {
            session.instance_id for session, _ in self.retired_sessions.values()
        }
        visible_instances = set(snapshot_by_instance) | retained_instances
        self.instance_templates = {
            key: value
            for key, value in self.instance_templates.items()
            if key in visible_instances
        }

    def close(self) -> None:
        for store in self.store_cache.values():
            store.close()
        self.store_cache.clear()

    def pin_session(self, session: SessionHealth | None) -> None:
        """Retain the selected session timeline while the TUI references it."""

        self.pinned_session_key = (
            f"{session.instance_id}:{session.session_id}" if session else ""
        )

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def _session_names(self, instance_id: str, path: Path) -> dict[str, str]:
        try:
            stat = path.stat()
        except OSError:
            return {}
        cached = self.session_index_cache.get(instance_id)
        signature = (stat.st_ino, stat.st_mtime_ns)
        if cached and cached[:2] == signature:
            return cached[2]
        names: dict[str, str] = {}
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = str(record.get("id") or "")
                    name = one_line(str(record.get("thread_name") or ""))
                    if session_id and name:
                        names[session_id] = name
        except OSError:
            return {}
        self.session_index_cache[instance_id] = (signature[0], signature[1], names)
        return names

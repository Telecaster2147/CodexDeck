"""Persistent multi-instance sampling engine."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

from codex.config_reader import CodexConfigReader
from codex.events import normalize_log
from codex.hook_events import HookEventReader
from codex.paths import ProcReader, open_rollout_paths
from codex.process_activity import ProcessActivityCollector
from codex.processes import DiscoveryResult, ProcessDiscovery
from codex.rollout import RolloutActivity, RolloutReader, latest_user_task, rollout_identity
from codex.state_store import StateStore
from codex.tui_session_log import TuiSessionLogReader, configured_session_log_path
from codex.terminal import RegularFileTailCollector, TerminalStore
from diagnostics import CollectorTracker
from history import HistoryStore
from models import (
    CodexPaths,
    CompactSourceStatus,
    Confidence,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProtocolCapabilities,
    ProcessInfo,
    SessionHealth,
)
from network.classifier import assess_process_network
from network.packet import PacketInspector
from network.sockets import SocketCollector
from state_machine import PROGRESS_KINDS, SessionStateMachine
from utils import CommandError, compact_path, one_line


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
        history: HistoryStore | None = None,
        packet_inspection: bool = False,
        packet_inspector: PacketInspector | None = None,
        process_activity: ProcessActivityCollector | None = None,
        hook_events_path: Path | None = None,
    ) -> None:
        self.interval = interval
        self.idle_threshold = idle_threshold
        self.selected_pids = selected_pids
        self.selected_homes = selected_homes
        self.proc = proc or ProcReader()
        self.discovery = discovery or ProcessDiscovery(proc=self.proc)
        self.sockets = sockets or SocketCollector()
        self.rollouts = RolloutReader()
        self.process_activity = process_activity or ProcessActivityCollector(
            getattr(self.proc, "root", Path("/proc"))
        )
        self.tui_session_logs = TuiSessionLogReader()
        self.hook_events = HookEventReader(hook_events_path)
        self.session_log_paths: dict[str, Path] = {}
        self.codex_configs = CodexConfigReader()
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
        self.terminals = TerminalStore()
        self.terminal_files = RegularFileTailCollector(
            getattr(self.proc, "root", Path("/proc"))
        )
        self.retired_sessions: dict[str, tuple[SessionHealth, float]] = {}
        self.instance_templates: dict[str, InstanceSnapshot] = {}
        self.pinned_session_key = ""
        self.last_discovery: DiscoveryResult | None = None
        self.last_socket_by_pid: dict[int, list] = {}
        self.discovery_stale_since: float | None = None
        self.socket_stale_since: float | None = None
        self.collectors = CollectorTracker(interval)
        self.history = history
        self.packet_inspector = packet_inspector or (
            PacketInspector() if packet_inspection else None
        )
        if self.packet_inspector is not None:
            packet_started = time.monotonic()
            if self.packet_inspector.start():
                self.collectors.record("packet", packet_started)

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
        process_started = time.monotonic()
        try:
            discovery = self.discovery.discover(self.selected_pids, self.selected_homes)
            self.collectors.record("process", process_started)
            self.last_discovery = discovery
            self.discovery_stale_since = None
        except CommandError as exc:
            self.collectors.record("process", process_started, exc)
            if self.last_discovery is None:
                raise RuntimeError(str(exc)) from exc
            discovery = self.last_discovery
            if self.discovery_stale_since is None:
                self.discovery_stale_since = now_monotonic
            stale_age = now_monotonic - self.discovery_stale_since
            diagnostics.append(f"进程列表已过期 {stale_age:.1f}s：{exc}")

        pids = {process.pid for process in discovery.processes}
        socket_started = time.monotonic()
        try:
            socket_by_pid = self.sockets.snapshot(pids)
            self.collectors.record("socket", socket_started)
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

        self._annotate_packet_metadata(socket_by_pid, diagnostics)

        by_instance: dict[str, list[ProcessInfo]] = defaultdict(list)
        for process in discovery.processes:
            by_instance[process.instance_id].append(process)

        instance_snapshots: list[InstanceSnapshot] = []
        active_session_keys: set[str] = set()
        active_rollouts: set[str] = set()
        active_process_keys = {process.stable_key for process in discovery.processes}
        active_session_log_paths: set[str] = set()
        hook_started = time.monotonic()
        hook_events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
        unassigned_hook_events: list[NormalizedEvent] = []
        for session_id, event in self.hook_events.read():
            if session_id:
                hook_events_by_session[session_id].append(event)
            else:
                unassigned_hook_events.append(event)
        if self.hook_events.configured:
            self.collectors.record(
                "hook_events",
                hook_started,
                self.hook_events.error or None,
            )

        for instance_id, processes in by_instance.items():
            resolved = discovery.instances[instance_id]
            instance_diagnostics: list[str] = []
            instance_rollouts: set[str] = set()
            codex_config = self.codex_configs.read(resolved.paths.codex_home)
            if codex_config.error:
                instance_diagnostics.append(f"config.toml 读取失败：{codex_config.error}")
            if resolved.method == "unresolved":
                instance_diagnostics.append("进程环境与活动文件不可读，路径按默认值推测")
            state_started = time.monotonic()
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
            self.collectors.record(
                f"state_db:{instance_id}",
                state_started,
                None if store.capabilities.threads else "缺少 threads capability",
            )
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
                previous = self.live_sessions.get(f"{instance_id}:{session_id}")
                process = replace(
                    process,
                    cwd=(record.cwd if record and record.cwd else process.cwd),
                    session_id=session_id,
                    session_title=self._bounded(one_line(title), 120),
                    current_task=self._bounded(one_line(task or fallback_task), 240),
                    model=(
                        record.model
                        if record and record.model
                        else previous.process.model if previous else ""
                    ),
                    reasoning_effort=(
                        record.reasoning_effort
                        if record and record.reasoning_effort
                        else previous.process.reasoning_effort if previous else ""
                    ),
                    rollout_path=str(rollout_path or ""),
                    activity=(
                        self.process_activity.snapshot(process.identity)
                        if process.role == "session"
                        else process.activity
                    ),
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
            if unassigned_hook_events:
                instance_session_ids = [
                    process.session_id
                    for process in enriched
                    if process.role == "session" and process.session_id
                ]
                remaining_hooks: list[NormalizedEvent] = []
                for event in unassigned_hook_events:
                    matches = [
                        session_id
                        for session_id in instance_session_ids
                        if event.turn_id
                        and any(
                            retained.turn_id == event.turn_id
                            for retained in self.machine.retained_events(
                                f"{instance_id}:{session_id}"
                            )
                        )
                    ]
                    if len(matches) == 1:
                        hook_events_by_session[matches[0]].append(event)
                    elif len(discovery.processes) == 1 and len(instance_session_ids) == 1:
                        hook_events_by_session[instance_session_ids[0]].append(event)
                    else:
                        remaining_hooks.append(event)
                unassigned_hook_events = remaining_hooks
            events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
            log_activity_by_session: dict[str, float] = {}
            tui_log_configured = False
            tui_log_readable = False
            tui_log_last_probe: float | None = None
            tui_log_last_event: float | None = None
            tui_log_error = ""
            tui_log_sources: set[str] = set()
            tui_started = time.monotonic()
            environ_reader = getattr(self.proc, "environ", None)
            for process in enriched:
                if process.role != "session" or not process.session_id:
                    continue
                environment = environ_reader(process.pid) if callable(environ_reader) else None
                session_log_path = configured_session_log_path(environment, process.cwd)
                if session_log_path is None:
                    self.session_log_paths.pop(process.stable_key, None)
                    continue
                tui_log_configured = True
                self.session_log_paths[process.stable_key] = session_log_path
                tui_log_sources.add(str(session_log_path))
                active_session_log_paths.add(str(session_log_path))
                result = self.tui_session_logs.read(
                    session_log_path,
                    default_session_id=process.session_id,
                )
                tui_log_readable = tui_log_readable or result.readable
                tui_log_last_probe = max(
                    (value for value in (tui_log_last_probe, result.observed_at) if value),
                    default=None,
                )
                tui_log_last_event = max(
                    (value for value in (tui_log_last_event, result.last_event_at) if value),
                    default=None,
                )
                tui_log_error = result.error or tui_log_error
                for session_id, event in result.events:
                    if session_id:
                        events_by_session[session_id].append(event)
            if tui_log_configured:
                self.collectors.record(
                    f"tui_session_log:{instance_id}",
                    tui_started,
                    tui_log_error or None,
                )
            cutoff = int(time.time()) - self.machine.lookback_seconds
            log_started = time.monotonic()
            logs = store.logs_since(
                [process.pid for process in enriched], self.log_cursors[instance_id], cutoff
            )
            self.collectors.record(
                f"log_db:{instance_id}",
                log_started,
                None if store.capabilities.logs else "缺少 logs capability",
            )
            if logs:
                self.log_cursors[instance_id] = max(record.log_id for record in logs)
            for record in logs:
                session_id = record.thread_id
                if not session_id:
                    match = re.match(r"pid:(\d+):", record.process_uuid)
                    session_id = session_for_pid.get(int(match.group(1)), "") if match else ""
                if session_id:
                    observed_at = time.time()
                    log_activity_by_session[session_id] = observed_at
                    events_by_session[session_id].extend(
                        replace(event, observed_at=observed_at)
                        for event in normalize_log(record)
                    )

            sessions = []
            instance_rollout_activity: list[dict[str, object]] = []
            rollout_started = time.monotonic()
            for process in enriched:
                if process.role != "session" or not process.session_id:
                    continue
                incoming = events_by_session.get(process.session_id, [])
                incoming.extend(hook_events_by_session.get(process.session_id, []))
                session_key = f"{instance_id}:{process.session_id}"
                rollout_activity = RolloutActivity(
                    process.rollout_path,
                    time.time(),
                )
                if process.rollout_path:
                    rollout_result = self.rollouts.read_with_activity(
                        Path(process.rollout_path)
                    )
                    rollout_activity = rollout_result.activity
                    incoming.extend(rollout_result.events)
                    self.terminals.apply(session_key, rollout_result.terminal_updates)
                self.terminals.apply(
                    session_key,
                    self.terminal_files.read(
                        session_key,
                        process.cwd,
                        process.activity.children,
                        time.time(),
                    ),
                )
                instance_rollout_activity.append(
                    self._rollout_activity_value(rollout_activity)
                )
                incoming = self._with_compact_config(
                    incoming,
                    codex_config.auto_compact_token_limit,
                    codex_config.auto_compact_token_limit_scope,
                )
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
                            observed_at=time.time(),
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
                previous = self.live_sessions.get(session_key)
                observation = self._observation_pulse(
                    previous,
                    process,
                    incoming,
                    rollout_activity,
                    network,
                    log_activity_by_session.get(process.session_id),
                    full_sample=True,
                    process_stale=self.discovery_stale_since is not None,
                    network_stale=sockets_stale,
                )
                session = self.machine.derive(
                    session_key,
                    process,
                    network,
                    observation=observation,
                )
                if (
                    session.observation.last_evidence_at is not None
                    and (
                        previous is None
                        or session.observation.last_evidence_at
                        > (previous.observation.last_evidence_at or 0.0)
                    )
                ):
                    self.machine.observe_compaction(
                        session_key,
                        timestamp=session.observation.last_evidence_at,
                        source=session.observation.last_evidence_source or "observation",
                        detail=session.observation.last_evidence_detail,
                    )
                    session = self.machine.derive(
                        session_key,
                        process,
                        network,
                        observation=observation,
                    )
                if self.history is not None:
                    baseline_samples, baseline_p50, baseline_p95 = (
                        self.history.silence_baseline(
                            now=time.time(),
                            instance_id=instance_id,
                            workspace=process.cwd,
                            phase=session.lifecycle.value,
                            model=process.model,
                            tool_category=session.current_operation.category,
                        )
                    )
                    observation = replace(
                        observation,
                        silence_baseline_samples=baseline_samples,
                        silence_p50_seconds=baseline_p50,
                        silence_p95_seconds=baseline_p95,
                    )
                    session = self.machine.derive(
                        session_key,
                        process,
                        network,
                        observation=observation,
                    )
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
                        observed_at=time.time(),
                    )
                    self.machine.ingest(session_key, [recovered])
                    session = self.machine.derive(
                        session_key,
                        process,
                        network,
                        observation=observation,
                    )
                session.terminal_sessions = self.terminals.summaries(session_key)
                sessions.append(session)
                self.previous_sockets[process.stable_key] = after
            self.collectors.record(f"rollout:{instance_id}", rollout_started)
            collector_health = [
                item
                for item in self.collectors.snapshot()
                if item.name in {"process", "socket"}
                or item.name.endswith(f":{instance_id}")
            ]

            instance_snapshots.append(
                InstanceSnapshot(
                    instance_id=instance_id,
                    paths=resolved.paths,
                    display_codex_home=compact_path(resolved.paths.codex_home),
                    display_sqlite_home=compact_path(resolved.paths.sqlite_home),
                    discovery_method=resolved.method,
                    capabilities=store.capabilities,
                    protocol_capabilities=self._merge_protocol_capabilities(sessions),
                    collector_health=collector_health,
                    diagnostics=instance_diagnostics,
                    unknown_event_types=self.rollouts.unknown_counts(instance_rollouts),
                    rollout_context_truncated=(
                        self.rollouts.has_truncated_context(instance_rollouts)
                    ),
                    rollout_activity=instance_rollout_activity,
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
                    auto_compact_token_limit=codex_config.auto_compact_token_limit,
                    auto_compact_token_limit_scope=(
                        codex_config.auto_compact_token_limit_scope
                    ),
                    compact_prompt_overridden=codex_config.compact_prompt_overridden,
                    auto_compact_config_source=codex_config.source,
                    tui_session_log=CompactSourceStatus(
                        configured=tui_log_configured,
                        readable=tui_log_readable,
                        source=", ".join(sorted(tui_log_sources)),
                        last_probe_at=tui_log_last_probe,
                        last_event_at=tui_log_last_event,
                        error=tui_log_error,
                    ),
                    hook_events=CompactSourceStatus(
                        configured=self.hook_events.configured,
                        readable=self.hook_events.configured and not self.hook_events.error,
                        source=str(self.hook_events.path) if self.hook_events.path else "",
                        last_probe_at=self.hook_events.last_probe_at,
                        last_event_at=self.hook_events.last_event_at,
                        error=self.hook_events.error,
                    ),
                    processes=enriched,
                    sessions=sessions,
                )
            )

        if unassigned_hook_events:
            diagnostics.append(
                f"{len(unassigned_hook_events)} 条 compact hook 事件缺少可关联 session"
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
        self.tui_session_logs.prune(active_session_log_paths)
        self.session_log_paths = {
            key: path
            for key, path in self.session_log_paths.items()
            if key in active_process_keys and str(path) in active_session_log_paths
        }
        self.process_activity.prune(active_process_keys)
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
        snapshot = MonitorSnapshot(
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
            collector_health=self.collectors.snapshot(),
        )
        if self.history is not None:
            history_started = time.monotonic()
            try:
                self.history.record_snapshot(snapshot)
                history_now = datetime.fromisoformat(snapshot.generated_at).timestamp()
                for instance in snapshot.instances:
                    instance.history_windows = self.history.window_stats(
                        now=history_now,
                        instance_id=instance.instance_id,
                    )
                self.collectors.record("history", history_started)
            except Exception as exc:
                self.collectors.record("history", history_started, exc)
                diagnostics.append(f"历史库写入失败：{exc}")
            snapshot.collection_duration_seconds = time.monotonic() - started
            snapshot.collector_health = self.collectors.snapshot()
        return snapshot

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        """Refresh active rollout events without resampling processes or sockets."""

        started = time.monotonic()
        refreshed_instances: list[InstanceSnapshot] = []
        changed = False
        hook_events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
        unassigned_hook_events: list[NormalizedEvent] = []
        for session_id, event in self.hook_events.read():
            if session_id:
                hook_events_by_session[session_id].append(event)
            else:
                unassigned_hook_events.append(event)
        active_sessions = [
            session
            for instance in snapshot.instances
            for session in instance.sessions
            if not session.process_exited
        ]
        for event in unassigned_hook_events:
            matches = [
                session
                for session in active_sessions
                if event.turn_id
                and any(
                    retained.turn_id == event.turn_id
                    for retained in self.machine.retained_events(
                        f"{session.instance_id}:{session.session_id}"
                    )
                )
            ]
            if len(matches) == 1:
                hook_events_by_session[matches[0].session_id].append(event)
            elif len(active_sessions) == 1:
                hook_events_by_session[active_sessions[0].session_id].append(event)
        for instance in snapshot.instances:
            refreshed_sessions: list[SessionHealth] = []
            refreshed_processes: dict[str, ProcessInfo] = {}
            rollout_paths: set[str] = set()
            rollout_activity_values: list[dict[str, object]] = []
            tui_log_readable = instance.tui_session_log.readable
            tui_log_last_probe = instance.tui_session_log.last_probe_at
            tui_log_last_event = instance.tui_session_log.last_event_at
            tui_log_error = instance.tui_session_log.error
            for session in instance.sessions:
                process = session.process
                if session.process_exited:
                    refreshed_sessions.append(session)
                    continue
                rollout_activity = RolloutActivity(process.rollout_path, time.time())
                rollout_events: tuple[NormalizedEvent, ...] = ()
                terminal_changed = False
                key = f"{session.instance_id}:{session.session_id}"
                if process.rollout_path:
                    rollout_paths.add(process.rollout_path)
                    rollout_result = self.rollouts.read_with_activity(
                        Path(process.rollout_path)
                    )
                    rollout_activity = rollout_result.activity
                    rollout_events = rollout_result.events
                    terminal_changed = self.terminals.apply(
                        key, rollout_result.terminal_updates
                    )
                rollout_activity_values.append(
                    self._rollout_activity_value(rollout_activity)
                )
                incoming = list(rollout_events)
                session_log_path = self.session_log_paths.get(process.stable_key)
                if session_log_path is not None:
                    session_log_result = self.tui_session_logs.read(
                        session_log_path,
                        default_session_id=session.session_id,
                    )
                    tui_log_readable = tui_log_readable or session_log_result.readable
                    tui_log_last_probe = max(
                        (
                            value
                            for value in (tui_log_last_probe, session_log_result.observed_at)
                            if value
                        ),
                        default=None,
                    )
                    tui_log_last_event = max(
                        (
                            value
                            for value in (tui_log_last_event, session_log_result.last_event_at)
                            if value
                        ),
                        default=None,
                    )
                    tui_log_error = session_log_result.error or tui_log_error
                    incoming.extend(
                        event
                        for session_id, event in session_log_result.events
                        if session_id == session.session_id
                    )
                incoming.extend(hook_events_by_session.get(session.session_id, []))
                if incoming or rollout_activity.changed or terminal_changed:
                    changed = True
                    incoming = self._with_compact_config(
                        incoming,
                        instance.auto_compact_token_limit,
                        instance.auto_compact_token_limit_scope,
                    )
                    self.machine.ingest(key, incoming)
                    observation = self._observation_pulse(
                        session,
                        process,
                        incoming,
                        rollout_activity,
                        session.network,
                        None,
                        full_sample=False,
                    )
                    session = self.machine.derive(
                        key,
                        process,
                        session.network,
                        observation=observation,
                    )
                    if (
                        session.observation.last_evidence_at is not None
                        and session.observation.last_evidence_at
                        > (self.live_sessions.get(key, session).observation.last_evidence_at or 0.0)
                    ):
                        self.machine.observe_compaction(
                            key,
                            timestamp=session.observation.last_evidence_at,
                            source=session.observation.last_evidence_source or "observation",
                            detail=session.observation.last_evidence_detail,
                        )
                        session = self.machine.derive(
                            key,
                            process,
                            session.network,
                            observation=observation,
                        )
                    self.live_sessions[key] = session
                else:
                    session.observation = replace(
                        session.observation,
                        sampled_at=rollout_activity.observed_at,
                        rollout_probe_at=(
                            rollout_activity.observed_at
                            if rollout_activity.available
                            else session.observation.rollout_probe_at
                        ),
                        last_probe_at=max(
                            filter(
                                lambda value: value is not None,
                                (
                                    session.observation.last_probe_at,
                                    rollout_activity.observed_at
                                    if rollout_activity.available
                                    else None,
                                ),
                            ),
                            default=None,
                        ),
                    )
                session.terminal_sessions = self.terminals.summaries(key)
                refreshed_sessions.append(session)
                refreshed_processes[process.stable_key] = session.process

            processes = [
                refreshed_processes.get(process.stable_key, process)
                for process in instance.processes
            ]
            refreshed_instances.append(
                replace(
                    instance,
                    protocol_capabilities=self._merge_protocol_capabilities(
                        refreshed_sessions
                    ),
                    unknown_event_types=self.rollouts.unknown_counts(rollout_paths),
                    rollout_context_truncated=(
                        self.rollouts.has_truncated_context(rollout_paths)
                    ),
                    rollout_activity=rollout_activity_values,
                    tui_session_log=replace(
                        instance.tui_session_log,
                        readable=tui_log_readable,
                        last_probe_at=tui_log_last_probe,
                        last_event_at=tui_log_last_event,
                        error=tui_log_error,
                    ),
                    hook_events=CompactSourceStatus(
                        configured=self.hook_events.configured,
                        readable=self.hook_events.configured and not self.hook_events.error,
                        source=str(self.hook_events.path) if self.hook_events.path else "",
                        last_probe_at=self.hook_events.last_probe_at,
                        last_event_at=self.hook_events.last_event_at,
                        error=self.hook_events.error,
                    ),
                    processes=processes,
                    sessions=refreshed_sessions,
                )
            )

        if not changed:
            return snapshot
        return replace(
            snapshot,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            instances=refreshed_instances,
            collection_duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _rollout_activity_value(activity: RolloutActivity) -> dict[str, object]:
        return {
            "path": activity.path,
            "observed_at": activity.observed_at,
            "available": activity.available,
            "stat_size": activity.stat_size,
            "mtime_ns": activity.mtime_ns,
            "bytes_read": activity.bytes_read,
            "complete_record_count": activity.complete_record_count,
            "ignored_record_count": activity.ignored_record_count,
            "normalized_count": activity.normalized_count,
            "partial_bytes": activity.partial_bytes,
            "last_growth_at": activity.last_growth_at,
            "replaced": activity.replaced,
            "truncated": activity.truncated,
            "copy_truncated": activity.copy_truncated,
        }

    @staticmethod
    def _observation_pulse(
        previous: SessionHealth | None,
        process: ProcessInfo,
        incoming: list[NormalizedEvent],
        rollout: RolloutActivity,
        network: NetworkEvidence,
        log_activity_at: float | None,
        *,
        full_sample: bool,
        process_stale: bool = False,
        network_stale: bool = False,
    ) -> ObservationPulse:
        now = rollout.observed_at
        base = previous.observation if previous else ObservationPulse()
        semantic = next(
            (
                event
                for event in reversed(incoming)
                if event.kind
                not in {"KEEPALIVE", "TOKEN_USAGE", "RATE_LIMIT", "MODEL_CONFIG", "UNPARSED_PAYLOAD"}
            ),
            None,
        )
        rollout_growth_at = (
            rollout.last_growth_at if rollout.changed else base.last_rollout_growth_at
        )
        process_activity_at = base.last_process_activity_at
        if full_sample and process.activity.active:
            process_activity_at = process.activity.sampled_at or now
        network_delta = 0
        if full_sample:
            network_delta = sum(
                item.sent_delta + item.received_delta + item.acked_delta
                for item in network.connections
            )
        network_progress_at = base.last_network_progress_at
        if network_delta:
            network_progress_at = now
        last_log_activity_at = log_activity_at or base.last_log_activity_at
        direct_activity = bool(
            semantic
            or rollout.changed
            or (full_sample and process.activity.active)
            or network_delta
            or log_activity_at
        )
        quiet_samples = base.quiet_full_samples
        if direct_activity:
            quiet_samples = 0
        elif full_sample:
            quiet_samples += 1
        process_probe = (
            process.activity.sampled_at
            if full_sample and process.activity.available
            else base.process_probe_at
        )
        network_probe = now if full_sample and not network_stale else base.network_probe_at
        log_probe = now if full_sample else base.log_probe_at
        rollout_probe = rollout.observed_at if rollout.available else base.rollout_probe_at
        probes = [
            value
            for value in (rollout_probe, process_probe, network_probe, log_probe)
            if value is not None
        ]
        stale_sources = []
        if process_stale or (full_sample and not process.activity.available):
            stale_sources.append("process")
        if network_stale:
            stale_sources.append("network")
        if process.rollout_path and not rollout.available:
            stale_sources.append("rollout")
        return replace(
            base,
            sampled_at=now,
            last_semantic_at=(semantic.timestamp if semantic else base.last_semantic_at),
            last_semantic_kind=(semantic.kind if semantic else base.last_semantic_kind),
            last_semantic_source=(semantic.source if semantic else base.last_semantic_source),
            last_rollout_growth_at=rollout_growth_at,
            last_process_activity_at=process_activity_at,
            last_network_progress_at=network_progress_at,
            last_log_activity_at=last_log_activity_at,
            last_probe_at=max(probes, default=base.last_probe_at),
            rollout_probe_at=rollout_probe,
            process_probe_at=process_probe,
            network_probe_at=network_probe,
            log_probe_at=log_probe,
            rollout_partial_bytes=rollout.partial_bytes,
            rollout_bytes_delta=rollout.bytes_read if rollout.changed else 0,
            process_activity=process.activity,
            network_bytes_delta=network_delta,
            quiet_full_samples=quiet_samples,
            collector_stale=bool(stale_sources),
            collector_stale_reason=(
                f"监测证据不足：{', '.join(stale_sources)} collector 数据不可用"
                if stale_sources
                else ""
            ),
        )

    @staticmethod
    def _with_compact_config(
        events: list[NormalizedEvent],
        auto_compact_token_limit: int | None,
        auto_compact_token_limit_scope: str = "",
    ) -> list[NormalizedEvent]:
        if auto_compact_token_limit is None:
            return events
        return [
            replace(
                event,
                metadata={
                    **event.metadata,
                    "auto_compact_token_limit": auto_compact_token_limit,
                    "auto_compact_token_limit_scope": auto_compact_token_limit_scope,
                },
            )
            if event.kind
            in {
                "TOKEN_USAGE",
                "COMPACT_REQUESTED",
                "COMPACT_CANDIDATE",
                "COMPACTING",
                "COMPACT_COMPLETED",
                "COMPACT_FAILED",
                "COMPACT_ABORTED",
            }
            else event
            for event in events
        ]

    @staticmethod
    def _merge_protocol_capabilities(
        sessions: list[SessionHealth],
    ) -> ProtocolCapabilities:
        rank = {"unavailable": 0, "derived": 1, "direct": 2}
        merged = {}
        for descriptor in fields(ProtocolCapabilities):
            statuses = [
                getattr(session.protocol_capabilities, descriptor.name)
                for session in sessions
            ]
            merged[descriptor.name] = max(
                statuses,
                key=lambda status: rank[status.mode.value],
                default=descriptor.default_factory(),
            )
        return ProtocolCapabilities(**merged)

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
                observed_at=now,
            )
            self.machine.ingest(key, [exited])
            network = NetworkEvidence(
                state=NetworkState.CLOSED,
                reason="Codex 进程已退出",
            )
            retained = self.machine.derive(
                key,
                session.process,
                network,
                now,
                observation=session.observation,
            )
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
        if self.packet_inspector is not None:
            self.packet_inspector.close()
            self.packet_inspector = None
        for store in self.store_cache.values():
            store.close()
        self.store_cache.clear()
        if self.history is not None:
            self.history.close()
            self.history = None

    def _annotate_packet_metadata(
        self,
        socket_by_pid: dict[int, list],
        diagnostics: list[str],
    ) -> None:
        """Merge optional passive TLS metadata without affecting socket sampling."""

        if self.packet_inspector is None:
            return
        packet_started = time.monotonic()
        try:
            self.packet_inspector.annotate(socket_by_pid)
            error = self.packet_inspector.error
            if error:
                self.collectors.record("packet", packet_started, error)
                diagnostics.append(f"网络解包不可用：{error}")
            elif self.packet_inspector.running:
                self.collectors.record("packet", packet_started)
            else:
                error = "AF_PACKET 采集线程未运行"
                self.collectors.record("packet", packet_started, error)
                diagnostics.append(f"网络解包不可用：{error}")
        except Exception as exc:
            self.collectors.record("packet", packet_started, exc)
            diagnostics.append(f"网络解包采集异常：{exc}")

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

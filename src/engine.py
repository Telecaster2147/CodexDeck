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
from codex.compact_evidence import CompactEvidenceReader
from codex.events import normalize_log
from codex.paths import ProcReader, open_rollout_paths
from codex.process_activity import ProcessActivityCollector
from codex.processes import DiscoveryResult, ProcessDiscovery
from codex.rollout import RolloutActivity, RolloutReader, latest_user_task, rollout_identity
from codex.state_store import StateStore
from codex.tui_session_log import (
    SessionLogReadResult,
    configured_session_log_path,
)
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
from snapshot_publisher import SnapshotPublisher
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
        self.compact_evidence = CompactEvidenceReader(hook_events_path)
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
        self.snapshot_publisher = SnapshotPublisher(interval, self.collectors, history)
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
        """Run the full collector pipeline and publish one coherent snapshot."""

        return self._collect_full_sample()

    def _collect_full_sample(self) -> MonitorSnapshot:
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
        hook_records = self.compact_evidence.read_hooks()
        if self.compact_evidence.hooks.configured:
            self.collectors.record(
                "hook_events",
                hook_started,
                self.compact_evidence.hooks.error or None,
            )

        environ_reader = getattr(self.proc, "environ", None)
        configured_session_logs: dict[str, Path] = {}
        session_log_path_counts: dict[str, int] = defaultdict(int)
        for process in discovery.processes:
            if process.role != "session":
                continue
            environment = environ_reader(process.pid) if callable(environ_reader) else None
            path = configured_session_log_path(environment, process.cwd)
            if path is None:
                continue
            configured_session_logs[process.stable_key] = path
            session_log_path_counts[str(path)] += 1
        session_log_results: dict[str, SessionLogReadResult] = {}

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
            events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
            log_activity_by_session: dict[str, float] = {}
            tui_log_configured = False
            tui_log_readable = False
            tui_log_last_probe: float | None = None
            tui_log_last_event: float | None = None
            tui_log_error = ""
            tui_log_sources: set[str] = set()
            tui_started = time.monotonic()
            for process in enriched:
                if process.role != "session" or not process.session_id:
                    continue
                session_log_path = configured_session_logs.get(process.stable_key)
                if session_log_path is None:
                    self.session_log_paths.pop(process.stable_key, None)
                    continue
                tui_log_configured = True
                self.session_log_paths[process.stable_key] = session_log_path
                tui_log_sources.add(str(session_log_path))
                active_session_log_paths.add(str(session_log_path))
                path_key = str(session_log_path)
                result = session_log_results.get(path_key)
                if result is None:
                    result = self.compact_evidence.read_session_log(
                        session_log_path,
                        default_session_id=(
                            process.session_id if session_log_path_counts[path_key] == 1 else ""
                        ),
                    )
                    session_log_results[path_key] = result
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

            session_processes: dict[str, list[ProcessInfo]] = defaultdict(list)
            for process in enriched:
                if process.role == "session" and process.session_id:
                    session_processes[process.session_id].append(process)

            sessions = []
            instance_rollout_activity: list[dict[str, object]] = []
            rollout_started = time.monotonic()
            for session_id, candidates in session_processes.items():
                process = max(
                    candidates,
                    key=lambda item: (
                        {True: 2, None: 1, False: 0}[item.foreground_active],
                        item.activity.active,
                        item.activity.sampled_at or 0.0,
                        bool(item.rollout_path),
                        item.identity.start_time,
                        item.pid,
                    ),
                )
                if len(candidates) > 1:
                    pids = ", ".join(
                        str(item.pid) for item in sorted(candidates, key=lambda p: p.pid)
                    )
                    instance_diagnostics.append(
                        f"检测到同一会话由 {len(candidates)} 个 Codex 进程打开；"
                        f"列表已合并，当前显示 PID {process.pid}（进程 {pids}）"
                    )

                incoming = list(events_by_session.get(session_id, []))
                session_key = f"{instance_id}:{session_id}"
                rollout_activities: list[RolloutActivity] = []
                seen_rollouts: set[str] = set()
                observed_at = time.time()
                process_children: dict[str, object] = {}
                process_tree_available = False
                process_tree_sampled_at: list[float] = []
                for candidate in candidates:
                    if candidate.activity.available:
                        process_tree_available = True
                        if candidate.activity.sampled_at is not None:
                            process_tree_sampled_at.append(candidate.activity.sampled_at)
                        process_children.update(
                            {
                                child.identity.key: child
                                for child in candidate.activity.children
                            }
                        )
                    if candidate.rollout_path and candidate.rollout_path not in seen_rollouts:
                        seen_rollouts.add(candidate.rollout_path)
                        rollout_result = self.rollouts.read_with_activity(
                            Path(candidate.rollout_path)
                        )
                        rollout_activities.append(rollout_result.activity)
                        incoming.extend(rollout_result.events)
                        self.terminals.apply(
                            session_key,
                            rollout_result.terminal_updates,
                        )
                    if candidate.activity.available:
                        self.terminals.apply(
                            session_key,
                            self.terminal_files.read(
                                session_key,
                                candidate.cwd,
                                candidate.activity.children,
                                observed_at,
                            ),
                        )
                if process_tree_available:
                    self.terminals.reconcile_children(
                        session_key,
                        tuple(process_children.values()),
                        observed_at,
                        evidence_cutoff=(
                            min(process_tree_sampled_at)
                            if process_tree_sampled_at
                            else None
                        ),
                    )
                else:
                    self.terminals.mark_process_unavailable(session_key)
                rollout_activity = max(
                    rollout_activities,
                    key=lambda item: (
                        item.last_growth_at or 0.0,
                        item.changed,
                        item.observed_at,
                    ),
                    default=RolloutActivity(process.rollout_path, observed_at),
                )
                instance_rollout_activity.extend(
                    self._rollout_activity_value(item) for item in rollout_activities
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
                before = [
                    socket
                    for candidate in candidates
                    for socket in self.previous_sockets.get(candidate.stable_key, [])
                ]
                after = [
                    socket
                    for candidate in candidates
                    for socket in socket_by_pid.get(candidate.pid, [])
                ]
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
                    log_activity_by_session.get(session_id),
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
                session.terminal_sessions = self.terminals.current_summaries(session_key)
                sessions.append(session)
                for candidate in candidates:
                    self.previous_sockets[candidate.stable_key] = socket_by_pid.get(
                        candidate.pid, []
                    )
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
                        configured=self.compact_evidence.hooks.configured,
                        readable=(
                            self.compact_evidence.hooks.configured
                            and not self.compact_evidence.hooks.error
                        ),
                        source=(
                            str(self.compact_evidence.hooks.path)
                            if self.compact_evidence.hooks.path
                            else ""
                        ),
                        last_probe_at=self.compact_evidence.hooks.last_probe_at,
                        last_event_at=self.compact_evidence.hooks.last_event_at,
                        error=self.compact_evidence.hooks.error,
                    ),
                    processes=enriched,
                    sessions=sessions,
                )
            )

        unresolved_hooks = self._apply_hook_records(instance_snapshots, hook_records)
        if unresolved_hooks:
            diagnostics.append(
                f"{unresolved_hooks} 条 compact hook 事件缺少唯一可关联 session"
            )

        self._retain_exited_sessions(instance_snapshots, active_session_keys)

        self._prune_full_sample_state(
            by_instance=set(by_instance),
            active_process_keys=active_process_keys,
            active_session_keys=active_session_keys,
            active_rollouts=active_rollouts,
            active_session_log_paths=active_session_log_paths,
        )
        return self.snapshot_publisher.publish(
            instances=instance_snapshots,
            started=started,
            now_monotonic=now_monotonic,
            diagnostics=diagnostics,
            discovery_stale_since=self.discovery_stale_since,
            socket_stale_since=self.socket_stale_since,
        )

    def _prune_full_sample_state(
        self,
        *,
        by_instance: set[str],
        active_process_keys: set[str],
        active_session_keys: set[str],
        active_rollouts: set[str],
        active_session_log_paths: set[str],
    ) -> None:
        """Drop mutable collector state that no longer belongs to an active sample."""

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
        self.log_cursors = defaultdict(
            int, {key: value for key, value in self.log_cursors.items() if key in by_instance}
        )
        self.log_process_keys = {
            key: value
            for key, value in self.log_process_keys.items()
            if key in by_instance
        }
        for instance_id in set(self.store_cache) - by_instance:
            self.store_cache.pop(instance_id).close()
        self.rollouts.prune(active_rollouts)
        self.compact_evidence.prune_session_logs(active_session_log_paths)
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
        self.terminals.prune(active_session_keys | retained_keys)
        self.terminal_files.prune(active_session_keys)
        self.rollout_path_cache = {
            key: value
            for key, value in self.rollout_path_cache.items()
            if key in active_process_keys
        }

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        """Refresh active rollout events without resampling processes or sockets."""

        started = time.monotonic()
        refreshed_instances: list[InstanceSnapshot] = []
        changed = False
        hook_records = self.compact_evidence.read_hooks()
        active_sessions = [
            session
            for instance in snapshot.instances
            for session in instance.sessions
            if not session.process_exited
        ]
        hook_events_by_key, unresolved_hooks = self._route_hook_records(
            active_sessions, hook_records
        )
        session_log_events_by_key, session_log_results = self._read_session_logs_once(
            active_sessions
        )
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
                        Path(process.rollout_path),
                        allow_terminal_metadata_backfill=False,
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
                    session_log_result = session_log_results[str(session_log_path)]
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
                    incoming.extend(session_log_events_by_key.get(key, []))
                incoming.extend(hook_events_by_key.get(key, []))
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
                session.terminal_sessions = self.terminals.current_summaries(key)
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
                        configured=self.compact_evidence.hooks.configured,
                        readable=(
                            self.compact_evidence.hooks.configured
                            and not self.compact_evidence.hooks.error
                        ),
                        source=(
                            str(self.compact_evidence.hooks.path)
                            if self.compact_evidence.hooks.path
                            else ""
                        ),
                        last_probe_at=self.compact_evidence.hooks.last_probe_at,
                        last_event_at=self.compact_evidence.hooks.last_event_at,
                        error=self.compact_evidence.hooks.error,
                    ),
                    processes=processes,
                    sessions=refreshed_sessions,
                )
            )

        diagnostics = list(snapshot.diagnostics)
        if unresolved_hooks:
            diagnostics.append(
                f"{unresolved_hooks} 条 compact hook 事件缺少唯一可关联 session"
            )
            changed = True
        if not changed:
            return snapshot
        return replace(
            snapshot,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            instances=refreshed_instances,
            collection_duration_seconds=time.monotonic() - started,
            diagnostics=diagnostics,
        )

    def _route_hook_records(
        self,
        sessions: list[SessionHealth],
        records: list[tuple[str, NormalizedEvent]],
    ) -> tuple[dict[str, list[NormalizedEvent]], int]:
        routed: dict[str, list[NormalizedEvent]] = defaultdict(list)
        active = [session for session in sessions if not session.process_exited]
        unresolved = 0
        for session_id, event in records:
            if session_id:
                matches = [session for session in active if session.session_id == session_id]
            elif event.turn_id:
                matches = [
                    session
                    for session in active
                    if any(
                        retained.turn_id == event.turn_id
                        for retained in self.machine.retained_events(
                            f"{session.instance_id}:{session.session_id}"
                        )
                    )
                ]
            else:
                matches = active if len(active) == 1 else []
            if len(matches) != 1:
                unresolved += 1
                continue
            session = matches[0]
            routed[f"{session.instance_id}:{session.session_id}"].append(event)
        return routed, unresolved

    def _apply_hook_records(
        self,
        snapshots: list[InstanceSnapshot],
        records: list[tuple[str, NormalizedEvent]],
    ) -> int:
        sessions = [session for snapshot in snapshots for session in snapshot.sessions]
        routed, unresolved = self._route_hook_records(sessions, records)
        if not routed:
            return unresolved
        for snapshot in snapshots:
            refreshed: list[SessionHealth] = []
            refreshed_processes: dict[str, ProcessInfo] = {}
            for session in snapshot.sessions:
                key = f"{session.instance_id}:{session.session_id}"
                incoming = routed.get(key, [])
                if incoming:
                    incoming = self._with_compact_config(
                        incoming,
                        snapshot.auto_compact_token_limit,
                        snapshot.auto_compact_token_limit_scope,
                    )
                    self.machine.ingest(key, incoming)
                    activity = RolloutActivity(session.process.rollout_path, time.time())
                    observation = self._observation_pulse(
                        session,
                        session.process,
                        incoming,
                        activity,
                        session.network,
                        None,
                        full_sample=False,
                    )
                    session = self.machine.derive(
                        key,
                        session.process,
                        session.network,
                        observation=observation,
                    )
                    session.terminal_sessions = self.terminals.current_summaries(key)
                    self.live_sessions[key] = session
                refreshed.append(session)
                refreshed_processes[session.process.stable_key] = session.process
            snapshot.sessions = refreshed
            snapshot.processes = [
                refreshed_processes.get(process.stable_key, process)
                for process in snapshot.processes
            ]
            snapshot.protocol_capabilities = self._merge_protocol_capabilities(refreshed)
        return unresolved

    def _read_session_logs_once(
        self,
        sessions: list[SessionHealth],
    ) -> tuple[dict[str, list[NormalizedEvent]], dict[str, SessionLogReadResult]]:
        sessions_by_path: dict[str, list[SessionHealth]] = defaultdict(list)
        paths: dict[str, Path] = {}
        for session in sessions:
            path = self.session_log_paths.get(session.process.stable_key)
            if path is None:
                continue
            key = str(path)
            paths[key] = path
            sessions_by_path[key].append(session)

        routed: dict[str, list[NormalizedEvent]] = defaultdict(list)
        results: dict[str, SessionLogReadResult] = {}
        for path_key, candidates in sessions_by_path.items():
            default_session_id = candidates[0].session_id if len(candidates) == 1 else ""
            result = self.compact_evidence.read_session_log(
                paths[path_key],
                default_session_id=default_session_id,
            )
            results[path_key] = result
            for session_id, event in result.events:
                matches = [
                    session for session in candidates if session.session_id == session_id
                ]
                if len(matches) != 1:
                    continue
                session = matches[0]
                routed[f"{session.instance_id}:{session.session_id}"].append(event)
        return routed, results

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
        # A long-lived Codex TUI keeps the same PID when `/new` replaces the
        # conversation. Re-check its open rollout descriptors on every full
        # sample; a path-only cache would pin the process to the old session.
        candidates: list[tuple[bool, int, int, Path, str]] = []
        for path in open_rollout_paths(process.pid, sessions_dir, self.proc):
            session_id, is_subagent = rollout_identity(path)
            try:
                stat = path.stat()
            except OSError:
                modified_at = 0
                size = 0
            else:
                modified_at = stat.st_mtime_ns
                size = stat.st_size
            candidates.append((not is_subagent, modified_at, size, path, session_id))
        if not candidates:
            cached = self.rollout_path_cache.get(process.stable_key)
            if cached and cached[0] is not None and cached[0].exists():
                return cached
            result = (None, "")
            self.rollout_path_cache[process.stable_key] = result
            return result
        _, _, _, path, session_id = max(
            candidates,
            key=lambda item: (item[0], item[1], item[2], str(item[3])),
        )
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
            replacement = next(
                (
                    candidate
                    for candidate in current.values()
                    if candidate.process.stable_key == session.process.stable_key
                    and candidate.session_id != session.session_id
                ),
                None,
            )
            if replacement is not None:
                summary = "会话已由 /new 关闭"
                detail = (
                    f"PID {session.process.pid} 已切换到新会话 "
                    f"{replacement.session_id[:8]}"
                )
                network_reason = "当前 Codex 窗口已切换到新会话"
                source_id = (
                    f"session-replaced:{session.process.stable_key}:"
                    f"{session.session_id}:{replacement.session_id}"
                )
            else:
                summary = "进程已退出"
                detail = f"PID {session.process.pid} 已结束"
                network_reason = "Codex 进程已退出"
                source_id = f"process-exited:{session.process.stable_key}"
            exited = NormalizedEvent(
                timestamp=now,
                kind="SESSION_CLOSED" if replacement is not None else "PROCESS_EXITED",
                summary=summary,
                detail=detail,
                source="process",
                confidence=Confidence.HIGH,
                source_id=source_id,
                observed_at=now,
            )
            self.machine.ingest(key, [exited])
            network = NetworkEvidence(
                state=NetworkState.CLOSED,
                reason=network_reason,
            )
            retained = self.machine.derive(
                key,
                session.process,
                network,
                now,
                observation=session.observation,
            )
            self.terminals.mark_stale(key)
            retained.terminal_sessions = self.terminals.current_summaries(key)
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

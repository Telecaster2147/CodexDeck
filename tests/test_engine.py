from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.paths import ProcReader, ResolvedInstance  # noqa: E402
from codex.processes import DiscoveryResult  # noqa: E402
from codex.rollout import RolloutActivity  # noqa: E402
from codex.terminal import TerminalUpdate  # noqa: E402
from engine import MonitorEngine  # noqa: E402
from app import exit_code  # noqa: E402
from models import (  # noqa: E402
    AdapterResult,
    AdapterStatus,
    CodexPaths,
    ChildProcessActivity,
    LifecycleState,
    Confidence,
    DiscoverySummary,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    ProcessTreeActivity,
    SessionHealth,
    SocketInfo,
)
from presentation.projection import primitive_value  # noqa: E402
from utils import CommandError, CommandExecutionResult  # noqa: E402


class FakeDiscovery:
    def __init__(self, result: DiscoveryResult) -> None:
        self.result = result
        self.calls = 0

    def discover(self, selected_pids=None, selected_homes=None) -> DiscoveryResult:
        self.calls += 1
        return self.result


class SequencedDiscovery:
    def __init__(self, results: list[DiscoveryResult]) -> None:
        self.results = results
        self.calls = 0

    def discover(self, selected_pids=None, selected_homes=None) -> DiscoveryResult:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


class SuccessThenFailDiscovery:
    def __init__(self, result: DiscoveryResult) -> None:
        self.result = result
        self.calls = 0

    def discover(self, selected_pids=None, selected_homes=None) -> DiscoveryResult:
        self.calls += 1
        if self.calls > 1:
            raise CommandError("ps timed out")
        return self.result


class SuccessThenOverflowDiscovery(SuccessThenFailDiscovery):
    def discover(self, selected_pids=None, selected_homes=None) -> DiscoveryResult:
        self.calls += 1
        if self.calls > 1:
            result = CommandExecutionResult(
                "ps",
                exit_code=-15,
                reason="stdout_byte_budget",
                stdout_bytes_read=8 * 1024 * 1024 + 1,
                stdout_bytes_retained=1024,
                records_retained=4,
            )
            raise CommandError("stdout_byte_budget", "ps", result)
        return self.result


class FakeSockets:
    def __init__(self, snapshots: list[dict[int, list[SocketInfo]]]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def snapshot(self, pids: set[int]) -> dict[int, list[SocketInfo]]:
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


class FailingSockets:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, pids: set[int]) -> dict[int, list[SocketInfo]]:
        self.calls += 1
        if self.calls > 1:
            raise CommandError("ss timed out")
        return {}


class SuccessThenFailSockets:
    def __init__(self, snapshot: dict[int, list[SocketInfo]]) -> None:
        self.value = snapshot
        self.calls = 0

    def snapshot(self, pids: set[int]) -> dict[int, list[SocketInfo]]:
        self.calls += 1
        if self.calls > 1:
            raise CommandError("ss timed out")
        return self.value


class SuccessThenOverflowSockets(SuccessThenFailSockets):
    def __init__(self, snapshot: dict[int, list[SocketInfo]]) -> None:
        super().__init__(snapshot)
        self.last_command_result: CommandExecutionResult | None = None

    def snapshot(self, pids: set[int]) -> dict[int, list[SocketInfo]]:
        self.calls += 1
        if self.calls > 1:
            self.last_command_result = CommandExecutionResult(
                "ss",
                exit_code=-15,
                reason="stdout_byte_budget",
                stdout_bytes_read=16 * 1024 * 1024 + 1,
                stdout_bytes_retained=2048,
                records_retained=2,
            )
            raise CommandError("stdout_byte_budget", "ss", self.last_command_result)
        return self.value


class FakeProc:
    def fd_targets(self, pid: int):
        return []


class SharedRolloutProc(FakeProc):
    def __init__(self, rollout: Path) -> None:
        self.rollout = rollout

    def fd_targets(self, pid: int):
        return [self.rollout]


class SwitchingRolloutProc(FakeProc):
    def __init__(self, rollouts: list[Path]) -> None:
        self.rollouts = rollouts

    def fd_targets(self, pid: int):
        return list(self.rollouts)


class FixedProcessActivity:
    def __init__(self, activity: ProcessTreeActivity) -> None:
        self.activity = activity

    def snapshot(self, identity: ProcessIdentity) -> ProcessTreeActivity:
        return self.activity

    def prune(self, active_identities: set[str]) -> None:
        return None


class SequencedProcessActivity:
    def __init__(self, activities: list[ProcessTreeActivity]) -> None:
        self.activities = activities
        self.calls = 0

    def snapshot(self, identity: ProcessIdentity) -> ProcessTreeActivity:
        activity = self.activities[min(self.calls, len(self.activities) - 1)]
        self.calls += 1
        return activity

    def prune(self, active_identities: set[str]) -> None:
        return None


def create_instance(
    root: Path,
    pid: int,
    session_id: str,
    failed: bool,
    event_age_seconds: float = 0,
) -> tuple[ResolvedInstance, ProcessInfo]:
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-{session_id}.jsonl"
    timestamp = (
        datetime.fromtimestamp(time.time() - event_age_seconds, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    records = [
        {"timestamp": timestamp, "type": "session_meta", "payload": {"id": session_id}},
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": f"turn-{session_id}"},
        },
    ]
    if failed:
        records.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": f"turn-{session_id}",
                    "error": {
                        "message": f"failure for {session_id}",
                        "codex_error_info": "server_overloaded",
                    },
                },
            }
        )
    else:
        records.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "working"}],
                },
            }
        )
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))

    state = sqlite3.connect(root / "state_5.sqlite")
    state.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, "
        "title TEXT, model TEXT, reasoning_effort TEXT, preview TEXT, first_user_message TEXT)"
    )
    state.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)",
        (
            session_id,
            str(rollout),
            str(root),
            f"Title {session_id}",
            "gpt-test",
            "high",
            "task",
            "first",
        ),
    )
    state.commit()
    state.close()

    logs = sqlite3.connect(root / "logs_2.sqlite")
    logs.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
        "level TEXT, target TEXT, thread_id TEXT, process_uuid TEXT, message TEXT)"
    )
    logs.execute(
        "INSERT INTO logs VALUES (1,?,?,?,?,?,?,?)",
        (int(time.time()), 0, "INFO", "test", session_id, f"pid:{pid}:uuid", "start"),
    )
    logs.commit()
    logs.close()

    paths = CodexPaths(
        root,
        root,
        root / "state_5.sqlite",
        root / "logs_2.sqlite",
        root / "session_index.jsonl",
        sessions,
    )
    instance = ResolvedInstance(f"instance-{pid}", paths, "environment")
    process = ProcessInfo(
        ProcessIdentity(pid, pid * 10),
        1,
        "codex",
        20,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        cwd=str(root),
        instance_id=instance.instance_id,
        discovery_method="environment",
    )
    return instance, process


class EngineTests(unittest.TestCase):
    def test_prepare_initial_snapshot_drains_rollout_backlog_before_publication(self) -> None:
        engine = MonitorEngine(2.0, 30, 900)
        session = SessionHealth("instance", "session", ProcessInfo(
            ProcessIdentity(1, 1),
            0,
            "codex",
            0,
            0.0,
            "S",
            "",
            "codex",
            "session",
            instance_id="instance",
            session_id="session",
        ))
        snapshot = SimpleNamespace(sessions=[session])
        key = session.session_identity
        cursor = SimpleNamespace(offset=0)
        engine.rollouts.cursors["rollout"] = cursor
        engine.machine.coverage_backlog[key] = True

        def refresh(current: object) -> object:
            cursor.offset += 1
            if cursor.offset == 2:
                engine.machine.coverage_backlog[key] = False
            return current

        with (
            patch.object(engine, "baseline") as baseline,
            patch.object(engine, "sample", return_value=snapshot) as sample,
            patch.object(engine, "refresh_events", side_effect=refresh) as refresh_events,
        ):
            result = engine.prepare_initial_snapshot()

        self.assertIs(result, snapshot)
        baseline.assert_called_once_with()
        sample.assert_called_once_with()
        self.assertEqual(refresh_events.call_count, 2)

    def test_source_less_rollout_activity_cannot_reopen_bootstrap_gap(self) -> None:
        coverage = MonitorEngine._evidence_coverage(
            [RolloutActivity("missing-rollout", 10.0)],
            bootstrap_truncated=True,
        )

        self.assertEqual(coverage.source_epoch, "")
        self.assertFalse(coverage.bootstrap_truncated)

    def test_discovery_summary_and_indirect_confidence_reach_snapshot_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "workspace-a", 303, "session-discovery", False
            )
            process.discovery_method = "process-family"
            process.discovery_confidence = Confidence.MEDIUM
            process.discovery_evidence = ("trusted_ancestry",)
            summary = DiscoverySummary(candidates=2, confirmed=1, rejected=1)
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult(
                        [process],
                        {instance.instance_id: instance},
                        summary,
                    )
                ),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
            )

            engine.baseline()
            snapshot = engine.sample()

            self.assertEqual(snapshot.discovery, summary)
            finding = next(
                item
                for item in snapshot.sessions[0].diagnosis
                if item.conclusion == "进程发现依据为间接证据"
            )
            self.assertIn("confidence=medium", finding.reason)
            self.assertEqual(finding.evidence, ("trusted_ancestry",))
            engine.close()

    def test_terminal_association_incompleteness_is_attached_to_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "workspace-a", 304, "session-association", False
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}]),
                proc=FakeProc(),
            )
            session = SessionHealth(instance.instance_id, "session-association", process)
            key = session.session_identity
            engine.terminals.apply(
                key,
                (
                    TerminalUpdate(
                        "ambiguous",
                        1.0,
                        call_id="CALL_ID",
                        terminal_candidate=True,
                    ),
                ),
            )

            attached = engine._attach_terminal_snapshot(session, key)

            self.assertEqual(attached.terminal_association.ambiguous, 1)
            self.assertFalse(attached.completeness.terminal_ownership.complete)
            self.assertEqual(attached.diagnosis[-1].conclusion, "Terminal 关联不完整")
            engine.close()

    def test_terminal_retention_drops_do_not_degrade_current_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "workspace-a", 305, "session-retention", False
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}]),
                proc=FakeProc(),
            )
            session = SessionHealth(instance.instance_id, "session-retention", process)
            key = session.session_identity
            engine.terminals.apply(
                key,
                tuple(
                    TerminalUpdate(
                        f"source-{index}",
                        float(index),
                        process_id=f"PROCESS_{index}",
                        status="completed",
                        terminal_candidate=True,
                    )
                    for index in range(20)
                ),
            )

            attached = engine._attach_terminal_snapshot(session, key)

            self.assertGreater(attached.terminal_association.dropped, 0)
            self.assertEqual(attached.terminal_association.ambiguous, 0)
            self.assertEqual(attached.terminal_association.conflicting, 0)
            self.assertEqual(attached.terminal_association.unresolved, 0)
            self.assertTrue(attached.completeness.terminal_ownership.complete)
            self.assertNotIn(
                "Terminal 关联不完整", {item.conclusion for item in attached.diagnosis}
            )
            engine.close()

    def test_bootstrap_mid_record_is_not_a_runtime_ingress_gap(self) -> None:
        bootstrap = RolloutActivity(
            "/workspace-a/rollout.jsonl",
            10.0,
            gap_count=1,
            gap_reason="bootstrap_started_mid_record",
        )
        later_gap = replace(bootstrap, gap_count=2, gap_reason="oversize_jsonl_record")

        initial = MonitorEngine._evidence_coverage(
            [bootstrap], bootstrap_truncated=True
        )
        degraded = MonitorEngine._evidence_coverage(
            [later_gap], bootstrap_truncated=True
        )

        self.assertEqual(initial.gap_count, 0)
        self.assertEqual(degraded.gap_count, 1)

    def test_discovery_stage_groups_processes_and_reuses_cached_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 301, "session-discovery-stage", False
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=SuccessThenFailDiscovery(result),
                sockets=FakeSockets([{}]),
                proc=FakeProc(),
            )
            diagnostics: list[str] = []

            current = engine._collect_discovery_stage(100.0, diagnostics)
            stale = engine._collect_discovery_stage(105.0, diagnostics)

            self.assertIs(current.result, result)
            self.assertIs(stale.result, result)
            self.assertEqual(set(current.by_instance), {instance.identity})
            grouped = current.by_instance[instance.identity]
            self.assertEqual([item.stable_key for item in grouped], [process.stable_key])
            self.assertEqual(grouped[0].instance_identity, instance.identity)
            self.assertEqual(current.active_process_keys, {process.stable_key})
            self.assertFalse(current.stale)
            self.assertTrue(stale.stale)
            self.assertEqual(engine.discovery_stale_since, 105.0)
            self.assertEqual(diagnostics, ["进程列表已过期 0.0s：ps timed out"])
            engine.close()

    def test_process_overflow_retains_last_complete_set_and_publishes_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 302, "session-process-overflow", False
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=SuccessThenOverflowDiscovery(result),
                sockets=FakeSockets([{}]),
                proc=FakeProc(),
            )
            diagnostics: list[str] = []

            engine._collect_discovery_stage(100.0, diagnostics)
            stale = engine._collect_discovery_stage(101.0, diagnostics)
            health = next(item for item in engine.collectors.snapshot() if item.name == "process")

            self.assertEqual(stale.active_process_keys, {process.stable_key})
            self.assertTrue(stale.stale)
            self.assertEqual(health.command.reason, "stdout_byte_budget")
            self.assertEqual(health.command.stdout_bytes_read, 8 * 1024 * 1024 + 1)
            self.assertIn("stdout_byte_budget", diagnostics[-1])
            engine.close()

    def test_socket_stage_reuses_cached_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 302, "session-socket-stage", False
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            socket = SocketInfo(
                "ESTAB",
                0,
                0,
                "192.0.2.10:43122",
                "198.51.100.20:443",
                process.pid,
                route="external",
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=SuccessThenFailSockets({process.pid: [socket]}),
                proc=FakeProc(),
            )
            diagnostics: list[str] = []

            current = engine._collect_socket_stage(result, 100.0, diagnostics)
            stale = engine._collect_socket_stage(result, 105.0, diagnostics)

            self.assertFalse(current.stale)
            self.assertTrue(stale.stale)
            self.assertIs(stale.by_pid, engine.last_socket_by_pid)
            self.assertEqual(engine.socket_stale_since, 105.0)
            self.assertEqual(diagnostics, ["TCP 快照已过期 0.0s：ss timed out"])
            engine.close()

    def test_full_sample_tails_child_stdout_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(root / "one", 38, "session-file-tail", False)
            log = root / "one" / "server.log"
            log.write_text("ready\n")
            proc_root = root / "proc"
            fd_dir = proc_root / "4242" / "fd"
            fd_dir.mkdir(parents=True)
            (fd_dir / "1").symlink_to(log)
            child = ChildProcessActivity(
                ProcessIdentity(4242, 99), command="server", state="S", active=True
            )
            activity = ProcessTreeActivity(
                available=True,
                sampled_at=time.time(),
                child_count=1,
                children=(child,),
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}, {}]),
                proc=ProcReader(proc_root),
                process_activity=FixedProcessActivity(activity),
            )
            engine.baseline()
            first = engine.sample()

            terminal = first.sessions[0].terminal_sessions[0]
            self.assertEqual(terminal.capability.value, "FILE_TAIL")
            self.assertEqual(terminal.process_id, "os:4242:99")
            self.assertTrue(terminal.process_active)
            self.assertEqual(terminal.chunks[0].text, "ready\n")

            with log.open("a") as handle:
                handle.write("request\n")
            second = engine.sample()

            self.assertEqual(
                "".join(chunk.text for chunk in second.sessions[0].terminal_sessions[0].chunks),
                "ready\nrequest\n",
            )
            engine.close()

    def test_terminal_hides_during_process_probe_failure_and_recovers_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(
                root / "one", 381, "session-file-tail-recovery", False
            )
            log = root / "one" / "server.log"
            log.write_text("ready\n")
            proc_root = root / "proc"
            fd_dir = proc_root / "4242" / "fd"
            fd_dir.mkdir(parents=True)
            (fd_dir / "1").symlink_to(log)
            child = ChildProcessActivity(
                ProcessIdentity(4242, 99), command="server", state="S", active=True
            )
            available = ProcessTreeActivity(
                available=True,
                sampled_at=time.time(),
                child_count=1,
                children=(child,),
            )
            unavailable = ProcessTreeActivity(available=False, sampled_at=time.time())
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}, {}, {}]),
                proc=ProcReader(proc_root),
                process_activity=SequencedProcessActivity([available, unavailable, available]),
            )
            engine.baseline()

            visible = engine.sample().sessions[0].terminal_sessions
            hidden = engine.sample().sessions[0].terminal_sessions
            recovered = engine.sample().sessions[0].terminal_sessions

            self.assertEqual(len(visible), 1)
            self.assertTrue(visible[0].process_active)
            self.assertEqual(hidden, [])
            self.assertEqual(len(recovered), 1)
            self.assertTrue(recovered[0].process_active)
            self.assertEqual(
                "".join(chunk.text for chunk in recovered[0].chunks),
                "ready\n",
            )
            engine.close()

    def test_instance_reads_auto_compact_boundary_from_its_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 40, "session-config", False)
            (instance.paths.codex_home / "config.toml").write_text(
                "model_auto_compact_token_limit = 220_000\n",
                encoding="utf-8",
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
            )

            engine.baseline()
            snapshot = engine.sample()

            self.assertEqual(snapshot.instances[0].auto_compact_token_limit, 220_000)
            self.assertEqual(snapshot.instances[0].auto_compact_config_source, "config.toml")
            engine.close()

    def test_multi_instance_isolation_and_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, process_one = create_instance(root / "one", 41, "session-one", True)
            second, process_two = create_instance(root / "two", 42, "session-two", False)
            process_one.cwd = "/launcher/cwd"
            result = DiscoveryResult(
                [process_one, process_two],
                {first.instance_id: first, second.instance_id: second},
            )
            discovery = FakeDiscovery(result)
            sockets = FakeSockets([{}, {}])
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=discovery,
                sockets=sockets,
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            self.assertEqual(len(snapshot.instances), 2)
            sessions = {session.session_id: session for session in snapshot.sessions}
            self.assertEqual(sessions["session-one"].lifecycle, LifecycleState.FAILED)
            self.assertEqual(
                sessions["session-one"].current_failure.message,
                "failure for session-one",
            )
            self.assertNotEqual(sessions["session-two"].lifecycle, LifecycleState.FAILED)
            self.assertEqual(sessions["session-one"].process.cwd, str(first.paths.codex_home))
            self.assertEqual(discovery.calls, 2)
            self.assertEqual(sockets.calls, 2)

    def test_same_session_opened_by_two_processes_is_merged_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, first = create_instance(Path(temp) / "one", 421, "SESSION_DUPLICATE", False)
            first = replace(
                first,
                process_group_id=421,
                foreground_process_group_id=421,
                terminal="pts/1",
            )
            second = replace(
                first,
                identity=ProcessIdentity(422, first.identity.start_time + 1),
                elapsed_seconds=1,
                process_group_id=422,
                foreground_process_group_id=421,
            )
            result = DiscoveryResult(
                [first, second],
                {instance.instance_id: instance},
            )
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{}, {}]),
                proc=SharedRolloutProc(next(instance.paths.sessions_dir.glob("*.jsonl"))),
            )

            engine.baseline()
            snapshot = engine.sample()

            self.assertEqual(len(snapshot.sessions), 1)
            self.assertEqual(snapshot.sessions[0].session_id, "SESSION_DUPLICATE")
            self.assertEqual(snapshot.sessions[0].process.pid, 421)
            self.assertEqual(len(snapshot.instances[0].processes), 2)
            self.assertTrue(
                any(
                    "同一会话由 2 个 Codex 进程打开" in message and "列表已合并" in message
                    for message in snapshot.instances[0].diagnostics
                )
            )
            engine.close()

    def test_transient_thread_record_miss_preserves_last_known_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 43, "session-model", False)
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{}, {}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            first = engine.sample().sessions[0]
            self.assertEqual(first.process.model, "gpt-test")

            store = engine.store_cache[instance.identity]
            original = store.threads_result
            store.threads_result = lambda _: AdapterResult(
                status=AdapterStatus.FAILED,
                source="sqlite.state.threads",
                observed_at=time.time(),
                error_code="sqlite_io",
                complete=False,
                value={},
            )
            second = engine.sample().sessions[0]

            self.assertEqual(second.process.model, "gpt-test")
            self.assertEqual(second.process.reasoning_effort, "high")
            store.threads_result = original
            engine.close()

    def test_failed_log_query_does_not_advance_cursor_and_recovery_reads_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 45, "session-log", False)
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{}, {}, {}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            engine.sample()
            identity = instance.identity
            self.assertEqual(engine.log_cursors[identity], 0)
            with sqlite3.connect(instance.paths.log_db) as connection:
                connection.execute(
                    "INSERT INTO logs VALUES (2,?,?,?,?,?,?,?)",
                    (
                        int(time.time()),
                        0,
                        "WARN",
                        "codex_core::responses_retry",
                        "session-log",
                        "pid:45:uuid",
                        "retry",
                    ),
                )

            store = engine.store_cache[identity]
            original = store.logs_since_result
            store.logs_since_result = lambda *_: AdapterResult(
                status=AdapterStatus.FAILED,
                source="sqlite.logs.since",
                observed_at=time.time(),
                error_code="sqlite_busy",
                complete=False,
                value=[],
            )
            failed_snapshot = engine.sample()

            self.assertEqual(engine.log_cursors[identity], 0)
            self.assertTrue(
                any(
                    result.error_code == "sqlite_busy"
                    for result in failed_snapshot.instances[0].adapter_results
                )
            )
            store.logs_since_result = original
            engine.sample()
            self.assertEqual(engine.log_cursors[identity], 2)
            engine.close()

    def test_fast_event_refresh_observes_tool_start_without_full_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 44, "session-fast", False)
            discovery = FakeDiscovery(DiscoveryResult([process], {instance.instance_id: instance}))
            sockets = FakeSockets([{}, {}])
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=discovery,
                sockets=sockets,
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            self.assertIs(engine.refresh_events(snapshot), snapshot)
            discovery_calls = discovery.calls
            socket_calls = sockets.calls

            def forbidden(*args, **kwargs):
                self.fail("fast path invoked full collector")

            engine.terminal_files.read = forbidden  # type: ignore[method-assign]
            engine.process_activity.snapshot = forbidden  # type: ignore[method-assign]
            engine.codex_configs.read = forbidden  # type: ignore[method-assign]
            engine._store_for = forbidden  # type: ignore[method-assign]
            engine.snapshot_publisher.publish = forbidden  # type: ignore[method-assign]
            rollout = Path(snapshot.sessions[0].process.rollout_path)
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with rollout.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "response_item",
                            "payload": {
                                "type": "custom_tool_call",
                                "call_id": "call-fast",
                                "name": "write_file",
                            },
                        }
                    )
                    + "\n"
                )

            running = engine.refresh_events(snapshot)

            self.assertEqual(running.sessions[0].lifecycle, LifecycleState.RUNNING_TOOL)
            self.assertEqual(running.sessions[0].phase, "工具正在运行")
            self.assertEqual(running.sessions[0].current_operation.label, "写入文件")
            self.assertEqual(running.sessions[0].current_operation.category, "write")
            self.assertEqual(
                running.sessions[0].tool_executions[-1].tool_name,
                "write_file",
            )
            self.assertEqual(discovery.calls, discovery_calls)
            self.assertEqual(sockets.calls, socket_calls)

            with rollout.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "response_item",
                            "payload": {
                                "type": "custom_tool_call_output",
                                "call_id": "call-fast",
                            },
                        }
                    )
                    + "\n"
                )
            completed = engine.refresh_events(running)

            self.assertEqual(completed.sessions[0].phase, "工具已返回")
            self.assertEqual(
                completed.sessions[0].tool_executions[-1].display_name,
                "写入文件",
            )
            self.assertEqual(
                completed.sessions[0].tool_executions[-1].tool_name,
                "write_file",
            )
            self.assertEqual(discovery.calls, discovery_calls)
            self.assertEqual(sockets.calls, socket_calls)
            engine.close()

    def test_unchanged_fast_refresh_preserves_published_snapshot_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 144, "session-unchanged", False)
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            published = primitive_value(snapshot)

            refreshed = engine.refresh_events(snapshot)

            self.assertIs(refreshed, snapshot)
            self.assertEqual(primitive_value(snapshot), published)
            engine.close()

    def test_changed_fast_refresh_does_not_mutate_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 145, "session-changed", False)
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            published = primitive_value(snapshot)
            rollout = Path(snapshot.sessions[0].process.rollout_path)
            with rollout.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "thread_goal_updated"},
                        }
                    )
                    + "\n"
                )

            refreshed = engine.refresh_events(snapshot)

            self.assertIsNot(refreshed, snapshot)
            self.assertGreater(
                refreshed.sessions[0].observation.rollout_bytes_delta,
                0,
            )
            self.assertEqual(primitive_value(snapshot), published)
            engine.close()

    def test_full_sample_does_not_mutate_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 245, "session-full-sample", False
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            published = primitive_value(snapshot)

            refreshed = engine.sample()

            self.assertIsNot(refreshed, snapshot)
            self.assertEqual(primitive_value(snapshot), published)
            engine.close()

    def test_fast_refresh_publishes_rollout_growth_without_timeline_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 45, "session-growth", False)
            discovery = FakeDiscovery(DiscoveryResult([process], {instance.instance_id: instance}))
            sockets = FakeSockets([{}, {}])
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=discovery,
                sockets=sockets,
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            event_count = len(snapshot.sessions[0].events)
            discovery_calls = discovery.calls
            socket_calls = sockets.calls
            rollout = Path(snapshot.sessions[0].process.rollout_path)
            with rollout.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "thread_goal_updated"},
                        }
                    )
                    + "\n"
                )

            refreshed = engine.refresh_events(snapshot)

            self.assertIsNot(refreshed, snapshot)
            self.assertEqual(len(refreshed.sessions[0].events), event_count)
            self.assertGreater(
                refreshed.sessions[0].observation.rollout_bytes_delta,
                0,
            )
            self.assertEqual(discovery.calls, discovery_calls)
            self.assertEqual(sockets.calls, socket_calls)
            engine.close()

    def test_fast_refresh_appends_background_terminal_poll_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 46, "session-terminal", False)
            discovery = FakeDiscovery(DiscoveryResult([process], {instance.instance_id: instance}))
            sockets = FakeSockets([{}, {}])
            activity = ProcessTreeActivity(
                available=True,
                sampled_at=time.time() + 60,
                child_count=1,
                children=(
                    ChildProcessActivity(ProcessIdentity(7770, 1), command="server", state="S"),
                ),
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=discovery,
                sockets=sockets,
                proc=FakeProc(),
                process_activity=FixedProcessActivity(activity),
            )
            engine.baseline()
            snapshot = engine.sample()
            discovery_calls = discovery.calls
            socket_calls = sockets.calls
            rollout = Path(snapshot.sessions[0].process.rollout_path)
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            def append(payload: dict[str, object]) -> None:
                with rollout.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {"timestamp": timestamp, "type": "response_item", "payload": payload}
                        )
                        + "\n"
                    )

            append(
                {
                    "type": "function_call",
                    "call_id": "call-start",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "server --watch", "workdir": "/workspace-a"}),
                }
            )
            append(
                {
                    "type": "function_call_output",
                    "call_id": "call-start",
                    "output": (
                        "Script running with cell ID 777\nWall time 1 seconds\nOutput:\nready\n"
                    ),
                }
            )
            running = engine.refresh_events(snapshot)

            self.assertEqual(running.sessions[0].terminal_sessions, [])
            store_key = running.sessions[0].session_identity
            retained = engine.terminals.summaries(store_key)
            self.assertEqual(retained[0].process_id, "777")
            self.assertEqual(retained[0].chunks[0].text, "ready\n")
            self.assertFalse(retained[0].process_active)
            self.assertEqual(discovery.calls, discovery_calls)
            self.assertEqual(sockets.calls, socket_calls)

            confirmed = engine.sample()
            terminal = confirmed.sessions[0].terminal_sessions[0]
            self.assertEqual(terminal.process_id, "777")
            self.assertEqual(terminal.status, "running")
            self.assertTrue(terminal.process_active)
            discovery_calls = discovery.calls
            socket_calls = sockets.calls

            append(
                {
                    "type": "function_call",
                    "call_id": "call-poll",
                    "name": "write_stdin",
                    "arguments": json.dumps({"session_id": 777, "chars": ""}),
                }
            )
            append(
                {
                    "type": "function_call_output",
                    "call_id": "call-poll",
                    "output": "request complete\n",
                }
            )
            refreshed = engine.refresh_events(confirmed)

            terminal = refreshed.sessions[0].terminal_sessions[0]
            self.assertEqual(terminal.status, "running")
            self.assertEqual(
                "".join(chunk.text for chunk in terminal.chunks),
                "ready\nrequest complete\n",
            )
            self.assertEqual(discovery.calls, discovery_calls)
            self.assertEqual(sockets.calls, socket_calls)
            engine.close()

    def test_network_stall_requires_two_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 51, "session", False, event_age_seconds=10
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})

            def queued() -> SocketInfo:
                return SocketInfo(
                    "ESTAB",
                    0,
                    20,
                    "127.0.0.1:5000",
                    "203.0.113.1:443",
                    51,
                    route="external",
                )

            sockets = FakeSockets([{51: [queued()]}, {51: [queued()]}, {51: [queued()]}])
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=sockets,
                proc=FakeProc(),
            )
            engine.baseline()
            first = engine.sample().sessions[0]
            second = engine.sample().sessions[0]
            self.assertEqual(first.network.state, NetworkState.SUSPECT)
            self.assertEqual(second.network.state, NetworkState.STALLED)

    def test_network_progress_records_recovery_after_suspect_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                52,
                "session-recovery",
                False,
                event_age_seconds=10,
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})

            def socket(received: int) -> SocketInfo:
                return SocketInfo(
                    "ESTAB",
                    0,
                    20,
                    "127.0.0.1:5000",
                    "203.0.113.1:443",
                    52,
                    bytes_received=received,
                    route="external",
                )

            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets(
                    [
                        {52: [socket(0)]},
                        {52: [socket(0)]},
                        {52: [socket(10)]},
                    ]
                ),
                proc=FakeProc(),
            )
            engine.baseline()
            self.assertEqual(
                engine.sample().sessions[0].network.state,
                NetworkState.SUSPECT,
            )
            recovered = engine.sample().sessions[0]
            self.assertEqual(recovered.recovery.value, "RECOVERED")
            self.assertTrue(any(event.kind == "RECOVERED" for event in recovered.events))
            engine.close()

    def test_socket_timeout_reuses_snapshot_and_exposes_stale_age(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                55,
                "session-stale",
                False,
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FailingSockets(),
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            self.assertTrue(snapshot.sessions[0].network.stale)
            self.assertIsNotNone(snapshot.socket_data_stale_age_seconds)
            self.assertTrue(any("TCP 快照已过期" in item for item in snapshot.diagnostics))
            engine.close()

    def test_socket_overflow_retains_last_complete_set_and_publishes_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                56,
                "session-socket-overflow",
                False,
            )
            result = DiscoveryResult([process], {instance.instance_id: instance})
            socket = SocketInfo(
                "ESTAB",
                0,
                0,
                "192.0.2.10:43122",
                "198.51.100.20:443",
                process.pid,
                route="external",
            )
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=SuccessThenOverflowSockets({process.pid: [socket]}),
                proc=FakeProc(),
            )
            engine.baseline()
            snapshot = engine.sample()
            session = snapshot.sessions[0]
            self.assertTrue(session.network.stale)
            self.assertEqual(len(session.network.connections), 1)
            self.assertEqual(session.network.connections[0].state, "ESTAB")
            health = next(item for item in snapshot.collector_health if item.name == "socket")
            self.assertEqual(health.command.reason, "stdout_byte_budget")
            self.assertEqual(health.command.stdout_bytes_read, 16 * 1024 * 1024 + 1)
            engine.close()

    def test_exited_process_is_retained_without_health_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                61,
                "session-exited",
                False,
            )
            active = DiscoveryResult([process], {instance.instance_id: instance})
            empty = DiscoveryResult([], {})
            rollout = next(instance.paths.sessions_dir.glob("*.jsonl"))
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with rollout.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "call_id": "call-exited",
                                "name": "exec_command",
                                "arguments": json.dumps({"cmd": "server --watch"}),
                            },
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": "call-exited",
                                "output": (
                                    "Script running with cell ID 888\n"
                                    "Wall time 1 seconds\nOutput:\nready\n"
                                ),
                            },
                        }
                    )
                    + "\n"
                )
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=SequencedDiscovery([active, active, empty]),
                sockets=FakeSockets([{}, {}, {}]),
                proc=FakeProc(),
                process_activity=FixedProcessActivity(
                    ProcessTreeActivity(
                        available=True,
                        sampled_at=time.time(),
                        child_count=1,
                        children=(
                            ChildProcessActivity(
                                ProcessIdentity(8880, 1), command="server", state="S"
                            ),
                        ),
                    )
                ),
            )
            engine.baseline()
            active_snapshot = engine.sample()
            self.assertFalse(active_snapshot.sessions[0].process_exited)
            self.assertEqual(active_snapshot.sessions[0].terminal_sessions[0].process_id, "888")
            exited = engine.sample()
            self.assertTrue(exited.sessions[0].process_exited)
            self.assertEqual(exited.sessions[0].network.state, NetworkState.CLOSED)
            self.assertTrue(
                any(event.kind == "PROCESS_EXITED" for event in exited.sessions[0].events)
            )
            self.assertEqual(exited.sessions[0].terminal_sessions, [])
            store_key = active_snapshot.sessions[0].session_identity
            retained = engine.terminals.summaries(store_key)
            self.assertEqual(retained[0].process_id, "888")
            self.assertTrue(retained[0].stale)
            self.assertEqual(exit_code(exited), 0)
            engine.close()

    def test_new_command_replaces_session_without_reusing_old_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                62,
                "session-before-new",
                False,
            )
            discovery = DiscoveryResult([process], {instance.instance_id: instance})
            old_rollout = next(instance.paths.sessions_dir.glob("*.jsonl"))
            proc = SwitchingRolloutProc([old_rollout])
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(discovery),
                sockets=FakeSockets([{}, {}, {}]),
                proc=proc,
            )
            engine.baseline()
            before = engine.sample()
            self.assertEqual(before.sessions[0].session_id, "session-before-new")

            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            new_rollout = instance.paths.sessions_dir / "rollout-session-after-new.jsonl"
            new_rollout.write_text(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "type": "session_meta",
                        "payload": {"id": "session-after-new"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "new session task"}],
                        },
                    }
                )
                + "\n"
            )
            old_mtime = old_rollout.stat().st_mtime_ns
            os.utime(new_rollout, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))
            state = sqlite3.connect(instance.paths.state_db)
            state.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)",
                (
                    "session-after-new",
                    str(new_rollout),
                    str(instance.paths.codex_home),
                    "New session",
                    "gpt-test",
                    "high",
                    "new session task",
                    "new session task",
                ),
            )
            state.commit()
            state.close()
            # A short transition may expose both descriptors. The newer main
            # rollout must replace the still-existing cached path.
            proc.rollouts = [old_rollout, new_rollout]

            after = engine.sample()
            sessions = {session.session_id: session for session in after.sessions}
            current = sessions["session-after-new"]
            closed = sessions["session-before-new"]
            self.assertFalse(current.process_exited)
            self.assertEqual(current.process.rollout_path, str(new_rollout))
            self.assertTrue(closed.process_exited)
            self.assertEqual(closed.network.reason, "当前 Codex 窗口已切换到新会话")
            self.assertTrue(
                any(
                    event.kind == "SESSION_CLOSED" and event.summary == "会话已由 /new 关闭"
                    for event in closed.events
                )
            )
            engine.close()

    def test_resumed_session_clears_retained_exit_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one",
                71,
                "session-resumed",
                False,
            )
            active = DiscoveryResult([process], {instance.instance_id: instance})
            empty = DiscoveryResult([], {})
            resumed_process = ProcessInfo(
                ProcessIdentity(72, 720),
                process.ppid,
                process.command,
                process.elapsed_seconds,
                process.cpu_percent,
                process.process_state,
                process.wait_channel,
                process.args,
                process.role,
                cwd=process.cwd,
                instance_id=process.instance_id,
                discovery_method=process.discovery_method,
            )
            resumed = DiscoveryResult(
                [resumed_process],
                {instance.instance_id: instance},
            )
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=SequencedDiscovery([active, active, empty, resumed]),
                sockets=FakeSockets([{}, {}, {}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            engine.sample()
            self.assertTrue(engine.sample().sessions[0].process_exited)
            logs = sqlite3.connect(instance.paths.log_db)
            logs.execute(
                "INSERT INTO logs VALUES (2,?,?,?,?,?,?,?)",
                (
                    int(time.time()),
                    0,
                    "INFO",
                    "test",
                    "session-resumed",
                    "pid:72:uuid",
                    "resume",
                ),
            )
            logs.commit()
            logs.close()
            current = engine.sample().sessions[0]
            self.assertFalse(current.process_exited)
            self.assertTrue(any(event.kind == "PROCESS_RESUMED" for event in current.events))
            engine.close()


if __name__ == "__main__":
    unittest.main()

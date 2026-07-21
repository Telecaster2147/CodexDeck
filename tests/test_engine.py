from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.paths import ProcReader, ResolvedInstance  # noqa: E402
from codex.processes import DiscoveryResult  # noqa: E402
from engine import MonitorEngine  # noqa: E402
from history import HistoryStore  # noqa: E402
from app import exit_code  # noqa: E402
from models import (  # noqa: E402
    CodexPaths,
    ChildProcessActivity,
    LifecycleState,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    ProcessTreeActivity,
    SocketInfo,
)
from utils import CommandError  # noqa: E402


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


class SessionLogProc(FakeProc):
    def __init__(self, path: Path) -> None:
        self.path = path

    def environ(self, pid: int):
        return {
            "CODEX_TUI_RECORD_SESSION": "1",
            "CODEX_TUI_SESSION_LOG_PATH": str(self.path),
        }


class FakePacketInspector:
    def __init__(self) -> None:
        self.error = ""
        self.running = True
        self.starts = 0
        self.annotations = 0
        self.closed = False

    def start(self) -> bool:
        self.starts += 1
        return True

    def annotate(self, socket_by_pid: dict[int, list[SocketInfo]]) -> None:
        self.annotations += 1
        for sockets in socket_by_pid.values():
            for socket_info in sockets:
                socket_info.tls_server_name = "api.openai.com"
                socket_info.tls_alpn_protocols = ("h2",)
                socket_info.tls_versions = ("TLS 1.3",)
                socket_info.tls_observed_at = 100.0

    def close(self) -> None:
        self.closed = True


class UnavailablePacketInspector:
    error = "AF_PACKET 原始套接字不可用：Operation not permitted"
    running = False

    def start(self) -> bool:
        return False

    def annotate(self, socket_by_pid: dict[int, list[SocketInfo]]) -> None:
        return None

    def close(self) -> None:
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
    def test_optional_collectors_are_inert_when_disabled(self) -> None:
        with patch("engine.PacketInspector") as packet_inspector:
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(DiscoveryResult([], {})),
                sockets=FakeSockets([{}]),
                proc=FakeProc(),
            )

        packet_inspector.assert_not_called()
        self.assertIsNone(engine.packet_inspector)
        self.assertIsNone(engine.history)
        self.assertFalse(engine.compact_evidence.hooks.configured)
        with patch("pathlib.Path.stat") as stat:
            self.assertEqual(engine.compact_evidence.read_hooks(), [])
            session_log = engine.compact_evidence.read_session_log(None)
        stat.assert_not_called()
        self.assertFalse(session_log.configured)
        self.assertEqual(engine.compact_evidence.session_logs.cursors, {})
        engine.close()

    def test_full_sample_tails_child_stdout_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(
                root / "one", 38, "session-file-tail", False
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
                process_activity=SequencedProcessActivity(
                    [available, unavailable, available]
                ),
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

    def test_history_windows_are_attached_to_instance_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(root / "one", 39, "session-history", False)
            result = DiscoveryResult([process], {instance.instance_id: instance})
            history = HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None)
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
                history=history,
            )

            engine.baseline()
            snapshot = engine.sample()

            self.assertEqual(
                [window.label for window in snapshot.instances[0].history_windows],
                ["15m", "1h", "24h"],
            )
            self.assertTrue(
                all(window.sample_count >= 1 for window in snapshot.instances[0].history_windows)
            )
            calls = 0
            original_window_stats = history.window_stats

            def counted_window_stats(**kwargs):
                nonlocal calls
                calls += 1
                return original_window_stats(**kwargs)

            history.window_stats = counted_window_stats  # type: ignore[method-assign]
            engine.sample()
            engine.sample()
            self.assertLessEqual(calls, 1)
            engine.close()

    def test_instance_reads_auto_compact_boundary_from_its_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 40, "session-config", False
            )
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
            instance, first = create_instance(
                Path(temp) / "one", 421, "SESSION_DUPLICATE", False
            )
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
                    "同一会话由 2 个 Codex 进程打开" in message
                    and "列表已合并" in message
                    for message in snapshot.instances[0].diagnostics
                )
            )
            engine.close()

    def test_transient_thread_record_miss_preserves_last_known_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 43, "session-model", False
            )
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

            engine.store_cache[instance.instance_id].threads = lambda _: {}
            second = engine.sample().sessions[0]

            self.assertEqual(second.process.model, "gpt-test")
            self.assertEqual(second.process.reasoning_effort, "high")
            engine.close()

    def test_fast_event_refresh_observes_tool_start_without_full_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 44, "session-fast", False
            )
            discovery = FakeDiscovery(
                DiscoveryResult([process], {instance.instance_id: instance})
            )
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

    def test_fast_refresh_publishes_rollout_growth_without_timeline_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(
                Path(temp) / "one", 45, "session-growth", False
            )
            discovery = FakeDiscovery(
                DiscoveryResult([process], {instance.instance_id: instance})
            )
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
            instance, process = create_instance(
                Path(temp) / "one", 46, "session-terminal", False
            )
            discovery = FakeDiscovery(
                DiscoveryResult([process], {instance.instance_id: instance})
            )
            sockets = FakeSockets([{}, {}])
            activity = ProcessTreeActivity(
                available=True,
                sampled_at=time.time() + 60,
                child_count=1,
                children=(
                    ChildProcessActivity(
                        ProcessIdentity(7770, 1), command="server", state="S"
                    ),
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
                    "arguments": json.dumps(
                        {"cmd": "server --watch", "workdir": "/workspace-a"}
                    ),
                }
            )
            append(
                {
                    "type": "function_call_output",
                    "call_id": "call-start",
                    "output": (
                        "Script running with cell ID 777\n"
                        "Wall time 1 seconds\nOutput:\nready\n"
                    ),
                }
            )
            running = engine.refresh_events(snapshot)

            self.assertEqual(running.sessions[0].terminal_sessions, [])
            store_key = f"{running.sessions[0].instance_id}:{running.sessions[0].session_id}"
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

    def test_session_log_typed_compact_is_seen_on_fast_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(
                root / "one", 46, "session-typed-compact", False
            )
            session_log = root / "tui-session.jsonl"
            session_log.write_text("")
            discovery = FakeDiscovery(
                DiscoveryResult([process], {instance.instance_id: instance})
            )
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=discovery,
                sockets=FakeSockets([{}, {}]),
                proc=SessionLogProc(session_log),
            )
            engine.baseline()
            snapshot = engine.sample()
            with session_log.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "direction": "from_tui",
                            "kind": "op",
                            "op": {"type": "Compact"},
                            "session_id": "session-typed-compact",
                            "turn_id": "TURN_COMPACT",
                        }
                    )
                    + "\n"
                )

            refreshed = engine.refresh_events(snapshot)

            self.assertEqual(
                refreshed.sessions[0].compactions[-1].status,
                "requested",
            )
            self.assertEqual(
                refreshed.sessions[0].current_operation.category,
                "compact",
            )
            engine.close()

    def test_shared_session_log_routes_each_record_before_advancing_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance_a, process_a = create_instance(root / "one", 146, "session-a", False)
            instance_b, process_b = create_instance(root / "two", 147, "session-b", False)
            session_log = root / "shared-tui-session.jsonl"
            session_log.write_text("")
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult(
                        [process_a, process_b],
                        {
                            instance_a.instance_id: instance_a,
                            instance_b.instance_id: instance_b,
                        },
                    )
                ),
                sockets=FakeSockets([{}, {}]),
                proc=SessionLogProc(session_log),
            )
            engine.baseline()
            snapshot = engine.sample()
            with session_log.open("a") as handle:
                for session_id, turn_id in (("session-a", "TURN_A"), ("session-b", "TURN_B")):
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "direction": "from_tui",
                                "kind": "op",
                                "op": {"type": "Compact"},
                                "session_id": session_id,
                                "turn_id": turn_id,
                            }
                        )
                        + "\n"
                    )

            refreshed = engine.refresh_events(snapshot)

            statuses = {
                session.session_id: session.compactions[-1].status
                for session in refreshed.sessions
            }
            self.assertEqual(statuses, {"session-a": "requested", "session-b": "requested"})
            engine.close()

    def test_hook_only_session_refreshes_without_rollout_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, process = create_instance(
                root / "one", 47, "session-hook-only", False
            )
            hook_events = root / "compact-hooks.jsonl"
            hook_events.write_text("")
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult([process], {instance.instance_id: instance})
                ),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
                hook_events_path=hook_events,
            )
            engine.baseline()
            snapshot = engine.sample()
            session = snapshot.sessions[0]
            session.process = replace(session.process, rollout_path="")
            snapshot.instances[0].processes = [session.process]
            engine.live_sessions[f"{session.instance_id}:{session.session_id}"] = session
            hook_events.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "PostCompact",
                        "session_id": "session-hook-only",
                        "turn_id": "TURN_HOOK_ONLY",
                        "trigger": "auto",
                        "outcome": "failed",
                    }
                )
                + "\n"
            )

            refreshed = engine.refresh_events(snapshot)

            self.assertIsNot(refreshed, snapshot)
            self.assertEqual(refreshed.sessions[0].compactions[-1].status, "failed")
            self.assertEqual(
                refreshed.sessions[0].latest_failure.category,
                "compact_error",
            )
            self.assertEqual(
                refreshed.sessions[0].observation.last_evidence_source,
                "compact_hook",
            )
            engine.close()

    def test_hook_with_duplicate_session_id_across_homes_is_not_routed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance_a, process_a = create_instance(root / "one", 246, "duplicate", False)
            instance_b, process_b = create_instance(root / "two", 247, "duplicate", False)
            hook_events = root / "compact-hooks.jsonl"
            hook_events.write_text("")
            engine = MonitorEngine(
                2.0,
                30,
                900,
                discovery=FakeDiscovery(
                    DiscoveryResult(
                        [process_a, process_b],
                        {
                            instance_a.instance_id: instance_a,
                            instance_b.instance_id: instance_b,
                        },
                    )
                ),
                sockets=FakeSockets([{}, {}]),
                proc=FakeProc(),
                hook_events_path=hook_events,
            )
            engine.baseline()
            snapshot = engine.sample()
            hook_events.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "PreCompact",
                        "session_id": "duplicate",
                        "turn_id": "TURN_DUPLICATE",
                        "trigger": "manual",
                        "outcome": "success",
                    }
                )
                + "\n"
            )

            refreshed = engine.refresh_events(snapshot)

            self.assertTrue(
                any("缺少唯一可关联 session" in item for item in refreshed.diagnostics)
            )
            self.assertTrue(all(not session.compactions for session in refreshed.sessions))
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

    def test_packet_metadata_flows_from_collector_to_network_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 57, "session-packet", False)
            result = DiscoveryResult([process], {instance.instance_id: instance})

            def connected() -> SocketInfo:
                return SocketInfo(
                    "ESTAB",
                    0,
                    0,
                    "192.0.2.10:43122",
                    "198.51.100.20:443",
                    57,
                    route="external",
                )

            packets = FakePacketInspector()
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{57: [connected()]}, {57: [connected()]}]),
                proc=FakeProc(),
                packet_inspector=packets,
            )
            engine.baseline()
            connection = engine.sample().sessions[0].network.connections[0]
            self.assertEqual(packets.starts, 1)
            self.assertGreaterEqual(packets.annotations, 1)
            self.assertEqual(connection.tls_server_name, "api.openai.com")
            self.assertEqual(connection.tls_alpn_protocols, ("h2",))
            self.assertEqual(connection.tls_versions, ("TLS 1.3",))
            engine.close()
            self.assertTrue(packets.closed)

    def test_packet_permission_failure_is_reported_once_per_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance, process = create_instance(Path(temp) / "one", 58, "session-packet-error", False)
            result = DiscoveryResult([process], {instance.instance_id: instance})
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=FakeDiscovery(result),
                sockets=FakeSockets([{58: []}]),
                proc=FakeProc(),
                packet_inspector=UnavailablePacketInspector(),
            )
            snapshot = engine.sample()
            self.assertEqual(
                snapshot.diagnostics,
                ["网络解包不可用：AF_PACKET 原始套接字不可用：Operation not permitted"],
            )
            packet_health = next(item for item in snapshot.collector_health if item.name == "packet")
            self.assertEqual(packet_health.consecutive_failures, 1)
            engine.close()

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
            store_key = (
                f"{active_snapshot.sessions[0].instance_id}:"
                f"{active_snapshot.sessions[0].session_id}"
            )
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
                            "content": [
                                {"type": "input_text", "text": "new session task"}
                            ],
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
                    event.kind == "SESSION_CLOSED"
                    and event.summary == "会话已由 /new 关闭"
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
            self.assertTrue(
                any(event.kind == "PROCESS_RESUMED" for event in current.events)
            )
            engine.close()


if __name__ == "__main__":
    unittest.main()

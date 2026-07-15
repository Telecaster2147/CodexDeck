from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_net_health.codex.paths import ResolvedInstance  # noqa: E402
from codex_net_health.codex.processes import DiscoveryResult  # noqa: E402
from codex_net_health.engine import MonitorEngine  # noqa: E402
from codex_net_health.app import exit_code  # noqa: E402
from codex_net_health.models import (  # noqa: E402
    CodexPaths,
    LifecycleState,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    SocketInfo,
)
from codex_net_health.utils import CommandError  # noqa: E402


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
    def test_multi_instance_isolation_and_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, process_one = create_instance(root / "one", 41, "session-one", True)
            second, process_two = create_instance(root / "two", 42, "session-two", False)
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
            self.assertEqual(discovery.calls, 2)
            self.assertEqual(sockets.calls, 2)

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
            engine = MonitorEngine(
                0.1,
                30,
                900,
                discovery=SequencedDiscovery([active, active, empty]),
                sockets=FakeSockets([{}, {}, {}]),
                proc=FakeProc(),
            )
            engine.baseline()
            self.assertFalse(engine.sample().sessions[0].process_exited)
            exited = engine.sample()
            self.assertTrue(exited.sessions[0].process_exited)
            self.assertEqual(exited.sessions[0].network.state, NetworkState.CLOSED)
            self.assertTrue(
                any(event.kind == "PROCESS_EXITED" for event in exited.sessions[0].events)
            )
            self.assertEqual(exit_code(exited), 0)
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

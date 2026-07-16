from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.paths import resolve_instance  # noqa: E402
from codex.processes import ProcessDiscovery  # noqa: E402
from codex.rollout import RolloutReader  # noqa: E402
from codex.state_store import StateStore  # noqa: E402
from models import CodexPaths, ProcessIdentity  # noqa: E402


class FakeProc:
    def __init__(self, environment=None, cwd=Path("/work"), targets=None) -> None:
        self.environment = environment or {}
        self.workdir = cwd
        self.targets = targets or []

    def environ(self, pid: int):
        return self.environment

    def cwd(self, pid: int):
        return self.workdir

    def fd_targets(self, pid: int):
        return self.targets

    def identity(self, pid: int):
        return ProcessIdentity(pid, 99)


class FamilyProc(FakeProc):
    def environ(self, pid: int):
        return {"CODEX_HOME": "/custom/codex"} if pid == 11 else {}

    def identity(self, pid: int):
        return ProcessIdentity(pid, pid * 100)


class FakeRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, command, timeout=1.5):
        return self.output


def paths(root: Path) -> CodexPaths:
    return CodexPaths(
        root,
        root,
        root / "state_5.sqlite",
        root / "logs_2.sqlite",
        root / "session_index.jsonl",
        root / "sessions",
    )


class PathTests(unittest.TestCase):
    def test_relative_sqlite_home_uses_process_cwd(self) -> None:
        proc = FakeProc(
            {"CODEX_HOME": "/data/codex", "CODEX_SQLITE_HOME": "runtime/db"},
            Path("/workspace/project"),
        )
        resolved = resolve_instance(7, proc)
        self.assertEqual(resolved.paths.codex_home, Path("/data/codex"))
        self.assertEqual(resolved.paths.sqlite_home, Path("/workspace/project/runtime/db"))
        self.assertEqual(resolved.method, "environment")

    def test_paths_are_inferred_from_open_files(self) -> None:
        proc = FakeProc(
            targets=[
                Path("/custom/home/sessions/2026/07/15/rollout-x.jsonl"),
                Path("/runtime/sqlite/logs_2.sqlite"),
            ]
        )
        resolved = resolve_instance(7, proc)
        self.assertEqual(resolved.paths.codex_home, Path("/custom/home"))
        self.assertEqual(resolved.paths.sqlite_home, Path("/runtime/sqlite"))
        self.assertEqual(resolved.method, "file-descriptor")

    def test_unreadable_environment_without_files_is_marked_unresolved(self) -> None:
        proc = FakeProc()
        proc.environment = None
        resolved = resolve_instance(7, proc)
        self.assertEqual(resolved.method, "unresolved")

    def test_launcher_inherits_child_session_instance(self) -> None:
        output = (
            "10 1 node 100 0.0 S epoll node /usr/bin/codex\n"
            "11 10 codex 99 1.0 S futex /opt/codex resume\n"
        )
        result = ProcessDiscovery(FakeRunner(output), FamilyProc()).discover()
        by_pid = {process.pid: process for process in result.processes}
        self.assertEqual(by_pid[10].instance_id, by_pid[11].instance_id)
        self.assertEqual(by_pid[10].discovery_method, "process-family")
        self.assertEqual(len(result.instances), 1)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        state = sqlite3.connect(self.root / "state_5.sqlite")
        state.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, "
            "title TEXT, model TEXT, reasoning_effort TEXT, preview TEXT, first_user_message TEXT)"
        )
        state.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)",
            (
                "thread-1",
                "/tmp/rollout.jsonl",
                "/work",
                "Title",
                "gpt-test",
                "high",
                "preview",
                "first",
            ),
        )
        state.commit()
        state.close()
        logs = sqlite3.connect(self.root / "logs_2.sqlite")
        logs.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
            "level TEXT, target TEXT, thread_id TEXT, process_uuid TEXT, message TEXT)"
        )
        logs.execute(
            "INSERT INTO logs VALUES (1,100,500000000,'WARN','codex_core::responses_retry',"
            "'thread-1','pid:42:uuid','stream disconnected')"
        )
        logs.commit()
        logs.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capabilities_and_batch_thread_mapping(self) -> None:
        store = StateStore(paths(self.root))
        self.assertTrue(store.capabilities.threads)
        self.assertTrue(store.capabilities.logs)
        self.assertEqual(store.active_threads([42]), {42: "thread-1"})
        record = store.threads(["thread-1"])["thread-1"]
        self.assertEqual(record.model, "gpt-test")
        store.close()

    def test_optional_log_columns_are_not_required(self) -> None:
        store = StateStore(paths(self.root))
        rows = store.logs_since([42], 0, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].body, "stream disconnected")
        self.assertEqual(rows[0].timestamp, 100.5)
        store.close()

    def test_replaced_database_invalidates_cached_store(self) -> None:
        store = StateStore(paths(self.root))
        replacement = self.root / "state-new.sqlite"
        connection = sqlite3.connect(replacement)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        os.replace(replacement, self.root / "state_5.sqlite")
        self.assertFalse(store.is_current())
        store.close()


class RolloutTests(unittest.TestCase):
    def test_partial_jsonl_record_is_completed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            meta = {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "s"},
            }
            partial = (
                '{"timestamp":"2026-07-15T00:00:01Z","type":"event_msg",'
                '"payload":{"type":"task_started"'
            )
            path.write_text(json.dumps(meta) + "\n" + partial)
            reader = RolloutReader()
            self.assertEqual(reader.read(path), [])
            with path.open("a") as handle:
                handle.write(',"turn_id":"t1"}}\n')
            events = reader.read(path)
            self.assertEqual([event.kind for event in events], ["TURN_STARTED"])
            self.assertEqual(reader.read(path), [])

    def test_unknown_types_are_counted_but_known_non_health_events_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-15T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message"},
                },
                {
                    "timestamp": "2026-07-15T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "future_protocol_event"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            reader.read(path)
            self.assertEqual(
                reader.unknown_counts({str(path)}),
                {"event_msg:future_protocol_event": 1},
            )

    def test_truncation_resets_cursor_and_unknown_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            unknown = {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "future_protocol_event"},
            }
            path.write_text(json.dumps(unknown) + "\n")
            reader = RolloutReader()
            reader.read(path)
            completed = {
                "timestamp": "2026-07-15T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn"},
            }
            path.write_text(json.dumps(completed) + "\n")
            events = reader.read(path)
            self.assertEqual([event.kind for event in events], ["TURN_COMPLETED"])
            self.assertEqual(reader.unknown_counts({str(path)}), {})

    def test_bounded_bootstrap_marks_earlier_context_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-15T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "x" * 80},
                },
                {
                    "timestamp": "2026-07-15T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            with patch("codex.rollout.MAX_SESSION_TAIL", 100):
                reader.read(path)
            self.assertTrue(reader.has_truncated_context({str(path)}))


if __name__ == "__main__":
    unittest.main()

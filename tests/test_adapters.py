from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.paths import resolve_instance  # noqa: E402
from codex.ingress import (  # noqa: E402
    MAX_INGRESS_BYTES_PER_TICK,
    MAX_INGRESS_RECORDS_PER_TICK,
    MAX_JSONL_RECORD_BYTES,
)
from codex.processes import ProcessDiscovery  # noqa: E402
from codex.rollout import (  # noqa: E402
    MAX_PROTOCOL_FAMILY_COUNTERS,
    OTHER_PROTOCOL_FAMILY,
    BoundedFamilyCounter,
    RolloutReader,
)
from codex.state_store import StateStore  # noqa: E402
from models import AdapterStatus, CodexPaths, Confidence, ProcessIdentity  # noqa: E402
from network.sockets import SocketCollector  # noqa: E402
from utils import CommandError, CommandExecutionResult  # noqa: E402


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
        return {"CODEX_HOME": "/custom/codex"} if pid in {11, 21, 22, 31, 40} else {}

    def identity(self, pid: int):
        return ProcessIdentity(pid, pid * 100)


class MappingProc(FakeProc):
    def __init__(self, environments=None, targets=None) -> None:
        super().__init__()
        self.environments = environments or {}
        self.target_map = targets or {}

    def environ(self, pid: int):
        return self.environments.get(pid, {})

    def fd_targets(self, pid: int):
        return self.target_map.get(pid, [])

    def identity(self, pid: int):
        return ProcessIdentity(pid, pid * 100)


class FakeRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, command, timeout=1.5):
        return self.output


class BoundedFakeRunner:
    def __init__(self, output: str, stderr: str = "") -> None:
        self.output = output
        self.stderr = stderr
        self.command: list[str] = []

    def run_result(self, command, **kwargs):
        self.command = list(command)
        line_filter = kwargs.get("stdout_line_filter")
        retained: list[str] = []
        filtered = 0
        filtered_bytes = 0
        for line in self.output.splitlines(keepends=True):
            if line_filter is not None and not line_filter(line.rstrip("\r\n")):
                filtered += 1
                filtered_bytes += len(line.encode())
                continue
            retained.append(line)
        stdout = "".join(retained)
        return CommandExecutionResult(
            command_name=str(command[0]),
            stdout=stdout,
            stderr=self.stderr,
            exit_code=0,
            complete=True,
            stdout_bytes_read=len(self.output.encode()),
            stdout_bytes_retained=len(stdout.encode()),
            stdout_bytes_filtered=filtered_bytes,
            stdout_lines_read=len(self.output.splitlines()),
            stderr_bytes_read=len(self.stderr.encode()),
            stderr_bytes_retained=len(self.stderr.encode()),
            stderr_lines_read=len(self.stderr.splitlines()),
            records_retained=len(retained),
            records_filtered=filtered,
        )


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

    def test_configured_sqlite_home_precedes_environment_until_fd_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('sqlite_home = "configured-db"\n')
            proc = FakeProc(
                {
                    "CODEX_HOME": str(codex_home),
                    "CODEX_SQLITE_HOME": "environment-db",
                },
                root,
            )

            configured = resolve_instance(7, proc)
            proc.targets = [root / "opened-db" / "state_6.sqlite"]
            opened = resolve_instance(7, proc)

        self.assertEqual(configured.paths.sqlite_home, root / "configured-db")
        self.assertEqual(configured.method, "config")
        self.assertEqual(opened.paths.sqlite_home, root / "opened-db")
        self.assertEqual(opened.method, "file-descriptor")

    def test_unreadable_environment_without_files_is_marked_unresolved(self) -> None:
        proc = FakeProc()
        proc.environment = None
        resolved = resolve_instance(7, proc)
        self.assertEqual(resolved.method, "unresolved")

    def test_launcher_inherits_child_session_instance(self) -> None:
        output = (
            "10 1 1000 10 10 pts/1 node 100 0.0 S epoll node /usr/bin/codex\n"
            "11 10 1000 10 10 pts/1 codex 99 1.0 S futex /opt/codex resume\n"
            "12 1 2000 12 12 pts/2 codex 80 0.0 S futex codex --other-user\n"
        )
        result = ProcessDiscovery(FakeRunner(output), FamilyProc(), user_id=1000).discover()
        by_pid = {process.pid: process for process in result.processes}
        self.assertEqual(by_pid[10].instance_id, by_pid[11].instance_id)
        self.assertEqual(by_pid[10].discovery_method, "process-family")
        self.assertEqual(by_pid[10].discovery_confidence, Confidence.MEDIUM)
        self.assertEqual(by_pid[10].discovery_evidence, ("trusted_ancestry",))
        self.assertEqual(by_pid[11].discovery_confidence, Confidence.HIGH)
        self.assertEqual(result.summary.candidates, 2)
        self.assertEqual(result.summary.confirmed, 2)
        self.assertNotIn(12, by_pid)
        self.assertEqual(len(result.instances), 1)
        self.assertTrue(by_pid[11].foreground_active)
        self.assertEqual(by_pid[11].terminal, "pts/1")

    def test_process_command_filters_uid_and_non_candidates_before_retention(self) -> None:
        output = (
            "99 1 1000 99 99 ? python 10 0.0 S wait python PRIVATE_ARGUMENT\n"
            "11 1 1000 11 11 ? codex 9 0.0 S wait /opt/codex resume\n"
        )
        runner = BoundedFakeRunner(output)

        result = ProcessDiscovery(runner, FamilyProc(), user_id=1000).discover()

        self.assertEqual([process.pid for process in result.processes], [11])
        self.assertEqual(runner.command[:3], ["ps", "-U", "1000"])
        self.assertIsNotNone(result.command_result)
        self.assertEqual(result.command_result.records_filtered, 1)
        self.assertNotIn("PRIVATE_ARGUMENT", result.command_result.stdout)

    def test_socket_command_retains_only_target_flow_and_continuations(self) -> None:
        output = (
            'ESTAB 0 0 127.0.0.1:1 203.0.113.1:443 users:(("other",pid=99,fd=3))\n'
            " cubic bytes_sent:999\n"
            'ESTAB 0 0 127.0.0.1:2 203.0.113.2:443 users:(("codex",pid=42,fd=7))\n'
            " cubic bytes_sent:12 bytes_received:20\n"
        )
        runner = BoundedFakeRunner(output)
        collector = SocketCollector(runner)

        sockets = collector.snapshot({42})

        self.assertEqual(len(sockets[42]), 1)
        self.assertEqual(sockets[42][0].bytes_sent, 12)
        self.assertEqual(collector.last_command_result.records_filtered, 2)
        self.assertNotIn("bytes_sent:999", collector.last_command_result.stdout)

    def test_socket_stderr_with_zero_exit_is_incomplete(self) -> None:
        collector = SocketCollector(
            BoundedFakeRunner("", "Cannot open netlink socket: Operation not permitted\n")
        )

        with self.assertRaises(CommandError) as caught:
            collector.snapshot({42})

        self.assertEqual(caught.exception.reason, "stderr_output")
        self.assertFalse(caught.exception.result.complete)
        self.assertNotIn("Operation not permitted", str(caught.exception))

    def test_socket_stderr_warning_preserves_verified_stdout(self) -> None:
        output = (
            'ESTAB 0 0 127.0.0.1:2 203.0.113.2:443 '
            'users:(("codex",pid=42,fd=7))\n'
        )
        collector = SocketCollector(BoundedFakeRunner(output, "warning: partial namespace\n"))

        sockets = collector.snapshot({42})

        self.assertEqual(len(sockets[42]), 1)
        self.assertIsNotNone(collector.last_command_result)
        self.assertFalse(collector.last_command_result.complete)
        self.assertEqual(collector.last_command_result.reason, "stderr_warning")

    def test_process_stderr_warning_preserves_verified_stdout(self) -> None:
        output = "11 1 1000 11 11 ? codex 9 0.0 S wait /opt/codex resume\n"
        runner = BoundedFakeRunner(output, "warning: partial process metadata\n")

        result = ProcessDiscovery(runner, FamilyProc(), user_id=1000).discover()

        self.assertEqual([process.pid for process in result.processes], [11])
        self.assertIsNotNone(result.command_result)
        self.assertFalse(result.command_result.complete)
        self.assertEqual(result.command_result.reason, "stderr_warning")

    def test_foreground_status_distinguishes_background_and_headless_sessions(self) -> None:
        output = (
            "21 1 1000 21 99 pts/3 codex 20 0.0 S futex codex resume background\n"
            "22 1 1000 22 -1 ? codex 10 0.0 S futex codex app-server\n"
        )

        result = ProcessDiscovery(FakeRunner(output), FamilyProc(), user_id=1000).discover()
        by_pid = {process.pid: process for process in result.processes}

        self.assertFalse(by_pid[21].foreground_active)
        self.assertIsNone(by_pid[22].foreground_active)

    def test_same_name_process_without_codex_evidence_is_rejected(self) -> None:
        output = "30 1 1000 30 30 ? codex 10 0.0 S wait codex --serve-unrelated\n"

        result = ProcessDiscovery(FakeRunner(output), FakeProc(), user_id=1000).discover()

        self.assertEqual(result.processes, [])
        self.assertEqual(result.instances, {})
        self.assertEqual(result.summary.candidates, 1)
        self.assertEqual(result.summary.rejected, 1)
        self.assertEqual(result.summary.diagnostics[0].reason, "no_confirming_codex_evidence")

    def test_wrapper_and_renamed_app_server_inherit_or_confirm_identity(self) -> None:
        output = (
            "30 1 1000 30 30 ? bash 10 0.0 S wait bash /opt/codex-next\n"
            "31 30 1000 30 30 ? codex-next 9 0.0 S wait /opt/codex-next app-server\n"
        )

        result = ProcessDiscovery(FakeRunner(output), FamilyProc(), user_id=1000).discover()
        by_pid = {process.pid: process for process in result.processes}

        self.assertEqual(set(by_pid), {30, 31})
        self.assertEqual(by_pid[31].role, "app-server")
        self.assertEqual(by_pid[31].discovery_method, "environment")
        self.assertEqual(by_pid[30].discovery_method, "process-family")
        self.assertEqual(by_pid[30].instance_id, by_pid[31].instance_id)

    def test_unreadable_candidate_is_bounded_unresolved_diagnostic(self) -> None:
        proc = FakeProc()
        proc.environment = None
        output = "".join(
            f"{pid} 1 1000 {pid} {pid} ? codex 10 0.0 S wait codex task-{pid}\n"
            for pid in range(100, 170)
        )

        result = ProcessDiscovery(FakeRunner(output), proc, user_id=1000).discover()

        self.assertEqual(result.summary.unresolved, 70)
        self.assertEqual(len(result.summary.diagnostics), 64)
        self.assertEqual(result.summary.diagnostics[0].outcome, "unresolved")

    def test_conflicting_environment_and_rollout_identity_is_unresolved(self) -> None:
        proc = MappingProc(
            environments={40: {"CODEX_HOME": "/codex/home-a"}},
            targets={40: [Path("/codex/home-b/sessions/2026/rollout-x.jsonl")]},
        )
        output = "40 1 1000 40 40 ? codex 10 0.0 S wait codex resume\n"

        result = ProcessDiscovery(FakeRunner(output), proc, user_id=1000).discover()

        self.assertEqual(result.processes, [])
        self.assertEqual(result.summary.unresolved, 1)
        self.assertEqual(
            result.summary.diagnostics[0].reason,
            "conflicting_codex_home_evidence",
        )

    def test_container_node_and_future_name_candidates_keep_confirmed_forms(self) -> None:
        output = (
            "50 1 1000 50 50 ? bwrap 20 0.0 S wait bwrap -- /opt/bin/codex\n"
            "51 50 1000 50 50 ? node 19 0.0 S wait node /pkg/bin/codex\n"
            "52 1 1000 52 52 ? codex-v2 18 0.0 S wait /opt/bin/codex-v2 app-server\n"
        )
        proc = MappingProc(
            environments={
                51: {"CODEX_HOME": "/codex/container"},
                52: {"CODEX_HOME": "/codex/future"},
            }
        )

        result = ProcessDiscovery(FakeRunner(output), proc, user_id=1000).discover()
        by_pid = {process.pid: process for process in result.processes}

        self.assertEqual(set(by_pid), {50, 51, 52})
        self.assertEqual(by_pid[50].discovery_method, "process-family")
        self.assertEqual(by_pid[51].role, "launcher")
        self.assertEqual(by_pid[52].role, "app-server")

    def test_manually_labeled_sample_reports_precision_and_recall(self) -> None:
        output = (
            "60 1 1000 60 60 ? codex 10 0.0 S wait codex real\n"
            "61 1 1000 61 61 ? codex 10 0.0 S wait codex unrelated\n"
        )
        proc = MappingProc(environments={60: {"CODEX_HOME": "/codex/home"}})

        result = ProcessDiscovery(
            FakeRunner(output),
            proc,
            user_id=1000,
            labeled_codex_pids={60, 62},
        ).discover()

        self.assertEqual(result.summary.labeled_true_positive, 1)
        self.assertEqual(result.summary.labeled_false_positive, 0)
        self.assertEqual(result.summary.labeled_false_negative, 1)
        self.assertEqual(result.summary.precision, 1.0)
        self.assertEqual(result.summary.recall, 0.5)


class StateStoreTests(unittest.TestCase):
    def test_read_only_store_created_in_worker_can_close_on_app_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            state = home / "state_5.sqlite"
            logs = home / "logs_2.sqlite"
            with sqlite3.connect(state) as connection:
                connection.execute("CREATE TABLE threads (id TEXT)")
            with sqlite3.connect(logs) as connection:
                connection.execute("CREATE TABLE logs (id INTEGER, ts REAL, target TEXT)")
            paths = CodexPaths(
                home,
                home,
                state,
                logs,
                home / "session_index.jsonl",
                home / "sessions",
            )
            stores: list[StateStore] = []
            worker = threading.Thread(target=lambda: stores.append(StateStore(paths)))
            worker.start()
            worker.join()

            stores[0].close()

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

    def test_empty_query_is_absent_and_complete(self) -> None:
        store = StateStore(paths(self.root))

        result = store.threads_result(["missing-thread"])

        self.assertEqual(result.status, AdapterStatus.ABSENT)
        self.assertTrue(result.complete)
        self.assertEqual(result.error_code, "")
        store.close()

    def test_locked_database_reports_bounded_failure_and_recovers(self) -> None:
        store = StateStore(paths(self.root))
        locker = sqlite3.connect(self.root / "state_5.sqlite")
        locker.execute("BEGIN EXCLUSIVE")
        try:
            failed = store.threads_result(["thread-1"])
        finally:
            locker.rollback()
            locker.close()

        recovered = store.threads_result(["thread-1"])

        self.assertEqual(failed.status, AdapterStatus.FAILED)
        self.assertEqual(failed.error_code, "sqlite_busy")
        self.assertFalse(failed.complete)
        self.assertEqual(recovered.status, AdapterStatus.PRESENT)
        store.close()

    def test_schema_drift_is_not_reported_as_unsupported(self) -> None:
        with sqlite3.connect(self.root / "state_5.sqlite") as connection:
            connection.execute("DROP TABLE threads")
            connection.execute("CREATE TABLE threads (title TEXT)")

        store = StateStore(paths(self.root))

        self.assertEqual(store.initialization_results[0].status, AdapterStatus.FAILED)
        self.assertEqual(store.initialization_results[0].error_code, "sqlite_schema_drift")
        store.close()

    def test_corrupt_database_has_stable_error_code(self) -> None:
        (self.root / "state_5.sqlite").write_bytes(b"not-a-sqlite-database")

        store = StateStore(paths(self.root))

        result = store.initialization_results[0]
        self.assertEqual(result.status, AdapterStatus.FAILED)
        self.assertEqual(result.error_code, "sqlite_corrupt")
        self.assertNotIn(str(self.root), result.error_code)
        store.close()

    def test_transient_io_failure_does_not_poison_connection(self) -> None:
        class FailingConnection:
            def execute(self, *_args: object, **_kwargs: object) -> object:
                raise sqlite3.OperationalError("disk I/O error at /private/database.sqlite")

        store = StateStore(paths(self.root))
        connection = store.state_connection
        store.state_connection = FailingConnection()  # type: ignore[assignment]

        failed = store.threads_result(["thread-1"])
        store.state_connection = connection
        recovered = store.threads_result(["thread-1"])

        self.assertEqual(failed.status, AdapterStatus.FAILED)
        self.assertEqual(failed.error_code, "sqlite_io")
        self.assertNotIn("private", failed.error_code)
        self.assertEqual(recovered.status, AdapterStatus.PRESENT)
        store.close()

    def test_missing_databases_are_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(paths(Path(temp)))

            self.assertEqual(
                [result.status for result in store.initialization_results],
                [AdapterStatus.UNSUPPORTED, AdapterStatus.UNSUPPORTED],
            )
            store.close()

    def test_full_log_page_is_explicitly_incomplete(self) -> None:
        with sqlite3.connect(self.root / "logs_2.sqlite") as connection:
            connection.executemany(
                "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?)",
                (
                    (
                        row_id,
                        101,
                        0,
                        "INFO",
                        "codex_core::responses_retry",
                        "thread-1",
                        "pid:42:uuid",
                        "retry",
                    )
                    for row_id in range(2, 5001)
                ),
            )

        store = StateStore(paths(self.root))
        result = store.logs_since_result([42], 0, 0)

        self.assertEqual(result.status, AdapterStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "partial_page")
        self.assertEqual(result.partial_count, 5000)
        self.assertFalse(result.complete)
        store.close()


class RolloutTests(unittest.TestCase):
    def test_rollout_same_inode_rewrite_uses_new_generation_and_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"

            def record(kind: str) -> str:
                return (
                    json.dumps(
                        {
                            "timestamp": 10,
                            "type": "event_msg",
                            "payload": {"type": kind, "turn_id": "TURN_ID"},
                        }
                    )
                    + "\n"
                )

            path.write_text(record("task_started"))
            reader = RolloutReader()
            first = reader.read_with_activity(path)
            path.write_text(record("task_complete"))
            second = reader.read_with_activity(path)

            self.assertEqual([event.kind for event in second.events], ["TURN_COMPLETED"])
            self.assertEqual(second.activity.generation, 1)
            self.assertTrue(second.activity.stream_uncertain)
            self.assertTrue(second.activity.anchor_hash)
            self.assertNotEqual(first.events[0].source_id, second.events[0].source_id)
            self.assertEqual(second.events[0].metadata["stream_generation"], 1)
            self.assertEqual(second.events[0].metadata["stream_offset"], 0)

    def test_burst_is_consumed_in_bounded_ordered_quanta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": float(index + 1),
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"context_tokens": index}},
                }
                for index in range(MAX_INGRESS_RECORDS_PER_TICK + 40)
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            timestamps: list[float] = []
            activities = []

            while True:
                result = reader.read_with_activity(path)
                activities.append(result.activity)
                timestamps.extend(event.source_timestamp for event in result.events)
                if not result.activity.backlog_bytes:
                    break

            self.assertTrue(activities[0].budget_exceeded)
            self.assertGreater(activities[0].backlog_bytes, 0)
            self.assertTrue(
                all(item.bytes_read <= MAX_INGRESS_BYTES_PER_TICK for item in activities)
            )
            self.assertTrue(
                all(item.record_count <= MAX_INGRESS_RECORDS_PER_TICK for item in activities)
            )
            self.assertEqual(timestamps, sorted(timestamps))
            self.assertEqual(len(timestamps), len(records))
            self.assertEqual(activities[-1].backlog_bytes, 0)

    def test_oversize_unterminated_record_enters_bounded_skip_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_bytes(b"x" * (MAX_JSONL_RECORD_BYTES + 1))
            reader = RolloutReader()

            oversized = reader.read_with_activity(path)
            self.assertEqual(len(reader.cursors[str(path)].partial), 0)
            self.assertEqual(oversized.activity.oversize_record_count, 1)
            self.assertEqual(oversized.activity.gap_count, 1)
            self.assertGreater(oversized.activity.skipped_bytes, MAX_JSONL_RECORD_BYTES)
            self.assertTrue(oversized.activity.gap_hash)

            with path.open("ab") as handle:
                handle.write(
                    b"\n"
                    + json.dumps(
                        {
                            "timestamp": 10.0,
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "turn"},
                        }
                    ).encode()
                    + b"\n"
                )
            recovered = reader.read_with_activity(path)

            self.assertEqual([item.kind for item in recovered.events], ["TURN_STARTED"])
            self.assertEqual(recovered.activity.backlog_bytes, 0)
            self.assertFalse(reader.cursors[str(path)].skipping_oversize)

    def test_hot_rollout_quantum_does_not_block_another_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hot = root / "hot.jsonl"
            ready = root / "ready.jsonl"
            hot.write_bytes(b"x" * (MAX_INGRESS_BYTES_PER_TICK * 2))
            ready.write_text(
                json.dumps(
                    {
                        "timestamp": 20.0,
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "ready"},
                    }
                )
                + "\n"
            )
            reader = RolloutReader()

            hot_result = reader.read_with_activity(hot)
            ready_result = reader.read_with_activity(ready)

            self.assertLessEqual(hot_result.activity.bytes_read, MAX_INGRESS_BYTES_PER_TICK)
            self.assertGreater(hot_result.activity.backlog_bytes, 0)
            self.assertEqual([item.kind for item in ready_result.events], ["TURN_STARTED"])

    def test_parse_wall_budget_rewinds_before_unconsumed_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text(
                "".join(
                    json.dumps(
                        {
                            "timestamp": index + 1,
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": str(index)},
                        }
                    )
                    + "\n"
                    for index in range(3)
                )
            )
            reader = RolloutReader()

            with patch(
                "codex.rollout.time.monotonic",
                side_effect=[0.0, 0.0, 1.0, 1.0],
            ):
                limited = reader.read_with_activity(path)

            self.assertEqual(limited.activity.record_count, 1)
            self.assertTrue(limited.activity.budget_exceeded)
            self.assertGreater(limited.activity.backlog_records_lower_bound, 0)
            remainder = reader.read_with_activity(path)
            self.assertEqual([item.source_timestamp for item in remainder.events], [2.0, 3.0])

    def test_backlog_age_persists_until_source_is_caught_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_bytes(b"x" * (MAX_INGRESS_BYTES_PER_TICK * 3))
            reader = RolloutReader()

            with patch("codex.rollout.time.time", return_value=100.0):
                first = reader.read_with_activity(path)
            with patch("codex.rollout.time.time", return_value=110.0):
                second = reader.read_with_activity(path)

            self.assertEqual(first.activity.backlog_age_seconds, 0.0)
            self.assertEqual(second.activity.backlog_age_seconds, 10.0)
            self.assertGreater(second.activity.backlog_bytes, 0)

    def test_manual_compact_empty_task_is_detected_across_incremental_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 216_402},
                            "model_context_window": 353_400,
                        },
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "turn_id": "old"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            reader.read(path)

            with path.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-17T00:00:02Z",
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "compact-turn"},
                        }
                    )
                    + "\n"
                )
            started = reader.read(path)

            self.assertEqual(
                [event.kind for event in started],
                ["TURN_STARTED"],
            )

            with path.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-17T00:01:02Z",
                            "type": "compacted",
                            "payload": {"window_number": 2},
                        }
                    )
                    + "\n"
                )
            completed = reader.read(path)
            self.assertEqual(
                [event.kind for event in completed],
                ["COMPACTING", "COMPACT_COMPLETED"],
            )
            compacting, compacted = completed
            self.assertEqual(compacting.timestamp, 1784246402.0)
            self.assertEqual(compacting.metadata["trigger"], "manual")
            self.assertAlmostEqual(compacting.metadata["context_ratio"], 216_402 / 353_400)
            self.assertEqual(compacted.metadata["trigger"], "manual")

    def test_user_message_prevents_empty_task_compact_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 216_402},
                            "model_context_window": 353_400,
                        },
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_complete", "turn_id": "old"},
                },
                {
                    "timestamp": "2026-07-17T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "继续工作"},
                },
                {
                    "timestamp": "2026-07-17T00:00:03Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "normal-turn"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))

            events = RolloutReader().read(path)

            self.assertEqual(
                [event.kind for event in events],
                ["TOKEN_USAGE", "TURN_COMPLETED", "TURN_STARTED"],
            )

    def test_normal_task_started_before_turn_context_is_not_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_complete", "turn_id": "old"},
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "normal-turn"},
                },
                {
                    "timestamp": "2026-07-17T00:00:01.010Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "normal-turn", "model": "gpt-test"},
                },
                {
                    "timestamp": "2026-07-17T00:00:01.020Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))

            events = RolloutReader().read(path)

            self.assertEqual(
                [event.kind for event in events],
                ["TURN_COMPLETED", "TURN_STARTED", "MODEL_CONFIG"],
            )
            self.assertFalse(any(event.kind.startswith("COMPACT") for event in events))

    def test_compacted_and_legacy_companion_emit_one_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "normal-turn"},
                },
                {
                    "timestamp": "2026-07-17T00:00:00.010Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "compacted",
                    "payload": {"message": "summary"},
                },
                {
                    "timestamp": "2026-07-17T00:00:01.010Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()

            events = reader.read(path)

            completions = [event for event in events if event.kind == "COMPACT_COMPLETED"]
            self.assertEqual(len(completions), 1)
            self.assertEqual(completions[0].metadata["trigger"], "auto")
            self.assertEqual(reader.unknown_counts({str(path)}), {})

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

    def test_invalid_utf8_record_is_ignored_without_losing_later_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            valid = json.dumps(
                {
                    "timestamp": "2026-07-15T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "t1"},
                }
            ).encode()
            path.write_bytes(b'{"invalid":"\xe6"}\n' + valid + b"\n")

            result = RolloutReader().read_with_activity(path)

            self.assertEqual([event.kind for event in result.events], ["TURN_STARTED"])
            self.assertEqual(result.activity.ignored_record_count, 1)

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
                    "timestamp": "2026-07-15T00:00:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "system", "content": []},
                },
                {
                    "timestamp": "2026-07-15T00:00:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "developer", "content": []},
                },
                {
                    "timestamp": "2026-07-15T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "future_protocol_event"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            events = reader.read(path)
            self.assertEqual([event.kind for event in events], ["UNPARSED_PAYLOAD"])
            self.assertEqual(events[0].unparsed.source_type, "event_msg:future_protocol_event")
            self.assertEqual(len(events[0].unparsed.sha256), 64)
            self.assertIsNotNone(events[0].observed_at)
            self.assertEqual(events[0].source_timestamp, events[0].timestamp)
            self.assertEqual(
                reader.unknown_counts({str(path)}),
                {"event_msg:future_protocol_event": 1},
            )

    def test_protocol_family_counters_bound_cardinality_without_losing_total(self) -> None:
        counter = BoundedFamilyCounter(max_families=4)
        for _ in range(20):
            counter.add("dominant")
        for index in range(20):
            counter.add(f"family-{index}")

        snapshot = counter.snapshot()
        self.assertLessEqual(len(counter.counts), 4)
        self.assertEqual(sum(snapshot.values()), 40)
        self.assertGreater(snapshot[OTHER_PROTOCOL_FAMILY], 0)
        self.assertIn("dominant", snapshot)
        self.assertGreater(counter.dropped_family_count, 0)

    def test_rollout_unknown_family_overflow_remains_visible_as_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-15T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": f"future_family_{index}"},
                }
                for index in range(MAX_PROTOCOL_FAMILY_COUNTERS + 12)
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            reader = RolloutReader()
            reader.read(path)

            unknown = reader.unknown_counts({str(path)})
            summary = reader.family_counter_summary({str(path)})
            self.assertLessEqual(len(unknown), MAX_PROTOCOL_FAMILY_COUNTERS + 1)
            self.assertEqual(sum(unknown.values()), len(records))
            self.assertGreater(unknown[OTHER_PROTOCOL_FAMILY], 0)
            self.assertEqual(summary["unknown_total"], len(records))
            self.assertGreater(summary["unknown_dropped_family_count"], 0)

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

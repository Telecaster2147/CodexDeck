from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex.process_activity import ProcessActivityCollector
from codex.rollout import RolloutReader
from models import (
    LifecycleState,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProcessIdentity,
    ProcessInfo,
    ProcessTreeActivity,
    SilenceState,
)
from state_machine import SessionStateMachine


def _process() -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(100, 1000),
        1,
        "codex",
        10,
        0.0,
        "S",
        "wait",
        "codex",
        "session",
        cwd="workspace-a",
        instance_id="INSTANCE_ID",
        session_id="SESSION_ID",
    )


def _stat(pid: int, ppid: int, start: int, utime: int, stime: int, state: str = "S") -> str:
    fields = [state, str(ppid), *("0" for _ in range(9)), str(utime), str(stime)]
    fields.extend("0" for _ in range(6))
    fields.append(str(start))
    fields.extend("0" for _ in range(8))
    return f"{pid} (proc-{pid}) " + " ".join(fields) + "\n"


def _write_proc(
    root: Path,
    pid: int,
    *,
    ppid: int,
    start: int,
    utime: int,
    stime: int,
    io_bytes: int,
    io_operations: int = 0,
    children: str = "",
) -> None:
    directory = root / str(pid)
    (directory / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_text(_stat(pid, ppid, start, utime, stime))
    (directory / "io").write_text(
        f"rchar: {io_bytes}\nwchar: {io_bytes}\nread_bytes: 0\nwrite_bytes: 0\n"
        f"syscr: {io_operations}\nsyscw: {io_operations}\n"
    )
    (directory / "status").write_text(
        "Threads:\t2\nvoluntary_ctxt_switches:\t3\n"
        "nonvoluntary_ctxt_switches:\t1\n"
    )
    (directory / "task" / str(pid) / "children").write_text(children)


class RolloutActivityTests(unittest.TestCase):
    def test_growth_and_partial_line_are_reported_without_normalized_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            reader = RolloutReader()
            path.write_text(
                '{"type":"event_msg","payload":{"type":"thread_goal_updated"}}\n'
            )
            first = reader.read_with_activity(path)
            self.assertEqual(first.events, ())
            self.assertGreater(first.activity.bytes_read, 0)
            self.assertEqual(first.activity.complete_record_count, 1)
            self.assertEqual(first.activity.normalized_count, 0)

            with path.open("ab") as handle:
                handle.write(b'{"type":"event_msg"')
            partial = reader.read_with_activity(path)
            self.assertTrue(partial.activity.changed)
            self.assertGreater(partial.activity.partial_bytes, 0)
            self.assertEqual(partial.events, ())

            with path.open("ab") as handle:
                handle.write(b',"payload":{"type":"thread_goal_updated"}}\n')
            completed = reader.read_with_activity(path)
            self.assertEqual(completed.activity.partial_bytes, 0)
            self.assertEqual(completed.activity.complete_record_count, 1)


class ProcessActivityTests(unittest.TestCase):
    def test_cpu_io_and_child_tree_are_aggregated_from_proc_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uptime").write_text("1000.0 0.0\n")
            _write_proc(
                root,
                100,
                ppid=1,
                start=1000,
                utime=10,
                stime=5,
                io_bytes=100,
            )
            collector = ProcessActivityCollector(root)
            first = collector.snapshot(ProcessIdentity(100, 1000))
            self.assertTrue(first.available)
            self.assertFalse(first.active)

            _write_proc(
                root,
                100,
                ppid=1,
                start=1000,
                utime=30,
                stime=10,
                io_bytes=300,
                io_operations=10,
                children="200",
            )
            _write_proc(
                root,
                200,
                ppid=100,
                start=2000,
                utime=1,
                stime=1,
                io_bytes=20,
                io_operations=2,
            )
            second = collector.snapshot(ProcessIdentity(100, 1000))
            self.assertTrue(second.active)
            self.assertGreater(second.cpu_seconds_delta, 0)
            self.assertGreater(second.io_bytes_delta, 0)
            self.assertGreater(second.io_operations_delta, 0)
            self.assertEqual(second.child_count, 1)
            self.assertEqual(second.children_created, 1)
            self.assertEqual(second.children[0].command, "proc-200")


class SilenceAssessmentTests(unittest.TestCase):
    def _derive(
        self,
        lifecycle_event: NormalizedEvent,
        pulse: ObservationPulse,
        now: float,
        network: NetworkState = NetworkState.IDLE,
    ):
        machine = SessionStateMachine(900)
        machine.ingest("key", [lifecycle_event])
        return machine.derive(
            "key",
            _process(),
            NetworkEvidence(network),
            now,
            observation=pulse,
        )

    def test_quiet_active_unknown_stall_and_observer_blind_are_distinct(self) -> None:
        now = 500.0
        event = NormalizedEvent(now - 130, "MODEL_PROGRESS", "progress")
        active = self._derive(
            event,
            ObservationPulse(
                last_network_progress_at=now - 2,
                network_probe_at=now,
                process_probe_at=now,
                rollout_probe_at=now,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(active.silence.state, SilenceState.QUIET_ACTIVE)

        unknown = self._derive(
            NormalizedEvent(now - 40, "MODEL_PROGRESS", "progress"),
            ObservationPulse(
                quiet_full_samples=1,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(unknown.silence.state, SilenceState.QUIET_UNKNOWN)

        stalled = self._derive(
            event,
            ObservationPulse(
                quiet_full_samples=2,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(stalled.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(stalled.silence.state, SilenceState.STALL_SUSPECT)

        severe = self._derive(
            NormalizedEvent(now - 310, "MODEL_PROGRESS", "progress"),
            ObservationPulse(
                quiet_full_samples=2,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(severe.silence.severity, "severe")
        self.assertEqual(severe.alert_level, "严重")

        historical = self._derive(
            event,
            ObservationPulse(
                quiet_full_samples=2,
                silence_baseline_samples=5,
                silence_p95_seconds=100,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(historical.silence.state, SilenceState.QUIET_UNKNOWN)

        blind = self._derive(
            event,
            ObservationPulse(
                collector_stale=True,
                collector_stale_reason="process collector stale",
            ),
            now,
        )
        self.assertEqual(blind.silence.state, SilenceState.OBSERVER_BLIND)

    def test_waiting_upstream_requires_idle_established_evidence(self) -> None:
        now = 100.0
        waiting = self._derive(
            NormalizedEvent(now - 40, "REQUEST_SENT", "request"),
            ObservationPulse(
                quiet_full_samples=2,
                process_activity=ProcessTreeActivity(available=True),
            ),
            now,
        )
        self.assertEqual(waiting.silence.state, SilenceState.WAITING_UPSTREAM)


if __name__ == "__main__":
    unittest.main()

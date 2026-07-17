from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex.rollout import RolloutReader
from codex.terminal import (
    RegularFileTailCollector,
    TerminalStore,
    TerminalUpdate,
    sanitize_terminal_text,
)
from models import ChildProcessActivity, ProcessIdentity, TerminalCapability


class TerminalTranscriptTests(unittest.TestCase):
    def test_background_exec_and_polls_form_one_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-start",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {"cmd": "make watch", "workdir": "/workspace-a"}
                        ),
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-start",
                        "output": (
                            "Script running with cell ID 321\n"
                            "Wall time 1 seconds\nOutput:\nserver ready\n"
                        ),
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-poll",
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 321, "chars": ""}),
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-poll",
                        "output": "request complete\n",
                    },
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            result = RolloutReader().read_with_activity(path)

        store = TerminalStore()
        self.assertTrue(store.apply("instance:session", result.terminal_updates))
        terminals = store.summaries("instance:session")

        self.assertEqual(len(terminals), 1)
        terminal = terminals[0]
        self.assertEqual(terminal.process_id, "321")
        self.assertEqual(terminal.command, "make watch")
        self.assertEqual(terminal.cwd, "/workspace-a")
        self.assertEqual(terminal.capability, TerminalCapability.POLL_TRANSCRIPT)
        self.assertEqual(
            "".join(chunk.text for chunk in terminal.chunks),
            "server readyrequest complete\n",
        )

    def test_direct_command_completion_marks_final_transcript(self) -> None:
        store = TerminalStore()
        updates = (
            TerminalUpdate(
                "begin",
                1.0,
                call_id="call-1",
                command="printf done",
                status="running",
                terminal_candidate=True,
            ),
            TerminalUpdate(
                "end",
                2.0,
                call_id="call-1",
                command="printf done",
                status="completed",
                exit_code=0,
                output="done\n",
                capability=TerminalCapability.FINAL_TRANSCRIPT,
                terminal_candidate=True,
                cumulative=True,
            ),
        )

        store.apply("session", updates)
        terminal = store.summaries("session")[0]

        self.assertEqual(terminal.status, "completed")
        self.assertEqual(terminal.exit_code, 0)
        self.assertEqual(terminal.capability, TerminalCapability.FINAL_TRANSCRIPT)
        self.assertEqual(terminal.chunks[0].text, "done\n")

    def test_duplicate_sources_and_delayed_running_do_not_duplicate_or_resurrect(self) -> None:
        store = TerminalStore()
        terminal = TerminalUpdate(
            "same",
            2.0,
            call_id="call-1",
            status="completed",
            output="done",
            capability=TerminalCapability.FINAL_TRANSCRIPT,
            terminal_candidate=True,
        )
        store.apply("session", (terminal, terminal))
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "old",
                    1.0,
                    call_id="call-1",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )

        summary = store.summaries("session")[0]
        self.assertEqual(summary.status, "completed")
        self.assertEqual(len(summary.chunks), 1)

    def test_terminal_control_sequences_are_not_replayed(self) -> None:
        text = sanitize_terminal_text(
            "start\x1b[2J\x1b]52;c;SECRET\x07\x1b[31mred\x1b[0m\rnext"
        )

        self.assertEqual(text, "startred\nnext")

    def test_regular_stdout_file_is_tailed_without_reading_pipe_or_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "server.log"
            log.write_text("ready\n")
            fd_dir = root / "proc" / "42" / "fd"
            fd_dir.mkdir(parents=True)
            (fd_dir / "1").symlink_to(log)
            (fd_dir / "2").symlink_to("pipe:[123]")
            child = ChildProcessActivity(
                ProcessIdentity(42, 7), command="server", state="S"
            )
            collector = RegularFileTailCollector(root / "proc")

            first = collector.read("session", str(workspace), (child,), 1.0)
            with log.open("a") as handle:
                handle.write("request\n")
            second = collector.read("session", str(workspace), (child,), 2.0)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].capability, TerminalCapability.FILE_TAIL)
        self.assertEqual(first[0].stream, "stdout")
        self.assertEqual(first[0].output, "ready\n")
        self.assertEqual(second[0].output, "request\n")


if __name__ == "__main__":
    unittest.main()

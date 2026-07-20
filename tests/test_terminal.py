from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex.rollout import RolloutReader
from codex.terminal import (
    RegularFileTailCollector,
    TerminalStore,
    TerminalUpdate,
    extract_terminal_updates,
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
        self.assertEqual(terminal.status, "running")
        self.assertEqual(terminal.capability, TerminalCapability.POLL_TRANSCRIPT)
        self.assertEqual(
            "".join(chunk.text for chunk in terminal.chunks),
            "server ready\nrequest complete\n",
        )

    def test_array_content_parts_preserve_background_output_and_nested_tool_identity(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-start",
                    "name": "exec",
                    "input": (
                        'const result = await tools.exec_command('
                        '{"cmd":"npm run dev","workdir":"/workspace-a"});'
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-start",
                    "output": [
                        {
                            "type": "output_text",
                            "text": "Script completed\nWall time 1.0 seconds\nOutput:\n",
                        },
                        {
                            "type": "output_text",
                            "text": (
                                "Process running with session ID 777\n"
                                "Wall time: 1 seconds\nOutput:\nready\n"
                            ),
                        },
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-poll",
                    "name": "exec",
                    "input": (
                        'const result = await tools.write_stdin('
                        '{"session_id":777,"chars":""});'
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-poll",
                    "output": [
                        {
                            "type": "output_text",
                            "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                        },
                        {"type": "output_text", "text": "request complete\n"},
                    ],
                },
            },
        ]
        updates = tuple(
            update
            for index, record in enumerate(records)
            for update in extract_terminal_updates(record, f"source-{index}", float(index))
        )
        store = TerminalStore()

        store.apply("session", updates)
        terminal = store.summaries("session")[0]

        self.assertEqual(terminal.process_id, "777")
        self.assertEqual(terminal.command, "npm run dev")
        self.assertEqual(terminal.cwd, "/workspace-a")
        self.assertEqual(terminal.status, "running")
        self.assertEqual(
            "".join(chunk.text for chunk in terminal.chunks),
            "ready\nrequest complete\n",
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

    def test_paginated_command_item_is_extracted_with_process_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-17T00:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": "turn-1",
                            "item": {
                                "type": "command_execution",
                                "id": "item-1",
                                "process_id": "901",
                                "command": ["printf", "done"],
                                "cwd": "/workspace-a",
                                "source": "unified_exec_startup",
                                "status": "completed",
                                "stdout": "done\n",
                                "stderr": "warning\n",
                                "aggregated_output": "done\nwarning\n",
                                "exit_code": 0,
                                "duration": 1.25,
                            },
                        },
                    }
                )
                + "\n"
            )
            result = RolloutReader().read_with_activity(path)

        store = TerminalStore()
        store.apply("session", result.terminal_updates)
        terminal = store.summaries("session")[0]
        self.assertEqual(terminal.process_id, "901")
        self.assertEqual(terminal.command, "printf done")
        self.assertEqual(terminal.exit_code, 0)
        self.assertEqual(
            [(chunk.stream, chunk.text) for chunk in terminal.chunks],
            [("stdout", "done\n"), ("stderr", "warning\n")],
        )
        self.assertEqual([event.kind for event in result.events], ["TOOL_COMPLETED"])
        self.assertEqual(result.events[0].metadata["command"], "printf done")
        self.assertEqual(result.events[0].metadata["command_source"], "unified_exec_startup")
        self.assertEqual(result.events[0].metadata["duration_seconds"], 1.25)

    def test_current_unified_exec_running_shape_correlates_later_poll(self) -> None:
        store = TerminalStore()
        initial = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-start",
                    "output": (
                        "Process running with session ID 777\n"
                        "Wall time: 1 seconds\nOutput:\nready\n"
                    ),
                },
            },
            "initial",
            1.0,
        )
        poll_call = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-poll",
                    "name": "write_stdin",
                    "arguments": json.dumps({"session_id": 777, "chars": ""}),
                },
            },
            "poll-call",
            2.0,
        )
        poll_output = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-poll",
                    "output": "complete\n",
                },
            },
            "poll-output",
            3.0,
        )

        store.apply("session", initial + poll_call + poll_output)
        terminals = store.summaries("session")

        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].process_id, "777")
        self.assertEqual(terminals[0].status, "running")
        self.assertEqual(terminals[0].capability, TerminalCapability.POLL_TRANSCRIPT)
        self.assertEqual(
            "".join(chunk.text for chunk in terminals[0].chunks),
            "ready\ncomplete\n",
        )

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

    def test_global_buffer_limit_marks_dropped_bytes(self) -> None:
        store = TerminalStore()
        with patch("codex.terminal.MAX_GLOBAL_TERMINAL_BYTES", 10):
            for index in range(2):
                store.apply(
                    f"session-{index}",
                    (
                        TerminalUpdate(
                            f"source-{index}",
                            float(index),
                            call_id=f"call-{index}",
                            status="completed",
                            output="12345678",
                            capability=TerminalCapability.FINAL_TRANSCRIPT,
                            terminal_candidate=True,
                        ),
                    ),
                )

        summaries = store.summaries("session-0") + store.summaries("session-1")
        self.assertLessEqual(sum(item.retained_bytes for item in summaries), 10)
        self.assertGreater(sum(item.dropped_bytes for item in summaries), 0)

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

    def test_regular_file_tail_preserves_utf8_split_across_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "server.log"
            log.write_bytes("好".encode("utf-8"))
            fd_dir = root / "proc" / "42" / "fd"
            fd_dir.mkdir(parents=True)
            (fd_dir / "1").symlink_to(log)
            child = ChildProcessActivity(ProcessIdentity(42, 7), command="server")
            collector = RegularFileTailCollector(root / "proc", max_read_bytes=1)

            updates = [
                collector.read("session", str(workspace), (child,), float(index))
                for index in range(3)
            ]

        self.assertEqual("".join(batch[0].output for batch in updates), "好")


if __name__ == "__main__":
    unittest.main()

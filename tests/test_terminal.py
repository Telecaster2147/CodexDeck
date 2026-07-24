from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex.rollout import RolloutReader, TerminalMetadataBackfillCursor
from codex.terminal import (
    RegularFileTailCollector,
    TerminalProtocolParser,
    TerminalStore,
    TerminalUpdate,
    extract_terminal_updates,
    sanitize_terminal_text,
)
from models import (
    ChildProcessActivity,
    InstanceIdentity,
    ProcessIdentity,
    RolloutIdentity,
    SessionIdentity,
    TerminalCapability,
)


class TerminalTranscriptTests(unittest.TestCase):
    def test_terminal_private_state_bounds_fail_closed_and_recover_by_scope(self) -> None:
        store = TerminalStore()
        old_scope = RolloutIdentity(Path("/workspace-a/old.jsonl"), 1, 10, 0)
        new_scope = RolloutIdentity(Path("/workspace-a/new.jsonl"), 1, 11, 0)
        with patch("codex.terminal.MAX_TERMINAL_SOURCE_IDS_PER_SCOPE", 3):
            for index in range(3):
                store.apply(
                    "session",
                    (
                        TerminalUpdate(
                            f"source-{index}",
                            float(index),
                            process_id="PROCESS_ID",
                            output=f"accepted-{index}\n",
                            terminal_candidate=True,
                            scope=old_scope,
                        ),
                    ),
                )
            store.apply(
                "session",
                (
                    TerminalUpdate(
                        "source-over-limit",
                        4.0,
                        process_id="PROCESS_ID",
                        output="DROPPED_SENTINEL\n",
                        terminal_candidate=True,
                        scope=old_scope,
                    ),
                ),
            )
            store.apply(
                "session",
                (
                    TerminalUpdate(
                        "source-over-limit",
                        5.0,
                        process_id="PROCESS_ID",
                        output="REPLAY_SENTINEL\n",
                        terminal_candidate=True,
                        scope=old_scope,
                    ),
                ),
            )

        retained = "".join(
            chunk.text for summary in store.summaries("session") for chunk in summary.chunks
        )
        private = store.private_state_summary("session")
        association = store.association_summary("session")
        self.assertNotIn("DROPPED_SENTINEL", retained)
        self.assertNotIn("REPLAY_SENTINEL", retained)
        self.assertEqual(private["source_entries"], 3)
        self.assertEqual(private["saturated_scopes"], 1)
        self.assertGreaterEqual(association.private_state_dropped, 2)
        self.assertIn(("dedupe_source_limit", 1), association.private_state_reasons)

        store.prune_scopes({new_scope})
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "new-source",
                    6.0,
                    process_id="NEW_PROCESS",
                    output="new generation\n",
                    terminal_candidate=True,
                    scope=new_scope,
                ),
            ),
        )
        recovered = store.association_summary("session")
        self.assertEqual(store.private_state_summary("session")["saturated_scopes"], 0)
        self.assertEqual(recovered.private_state_recoveries, 1)
        self.assertIn("new generation\n", store.summaries("session")[-1].chunks[-1].text)

    def test_terminal_correlation_aliases_are_bounded_per_retained_terminal(self) -> None:
        store = TerminalStore()
        scope = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10, 0)
        with patch("codex.terminal.MAX_TERMINAL_ALIASES_PER_TERMINAL", 2):
            for index in range(8):
                store.apply(
                    "session",
                    (
                        TerminalUpdate(
                            f"source-{index}",
                            float(index),
                            call_id=f"call-{index}",
                            process_id="PROCESS_ID",
                            terminal_candidate=True,
                            continuation=True,
                            wait_for_completion=True,
                            scope=scope,
                        ),
                    ),
                )

        private = store.private_state_summary("session")
        association = store.association_summary("session")
        self.assertEqual(private["call_entries"], 2)
        self.assertEqual(private["continuation_entries"], 2)
        self.assertEqual(private["wait_entries"], 2)
        self.assertLessEqual(private["process_entries"], 2)
        self.assertGreater(association.private_state_evictions, 0)
        self.assertIn("call_alias_limit", dict(association.private_state_reasons))

    def test_association_summary_reports_confirmed_ambiguous_and_unresolved(self) -> None:
        store = TerminalStore()
        scope = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10)
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "confirmed",
                    1.0,
                    call_id="CALL_CONFIRMED",
                    terminal_candidate=True,
                    scope=scope,
                ),
                TerminalUpdate(
                    "ambiguous",
                    2.0,
                    call_id="CALL_AMBIGUOUS",
                    terminal_candidate=True,
                ),
                TerminalUpdate("unresolved", 3.0, terminal_candidate=True),
            ),
        )

        summaries = {item.terminal_id: item for item in store.summaries("session")}
        association = store.association_summary("session")

        self.assertEqual(summaries["CALL_CONFIRMED"].association_status, "confirmed")
        self.assertEqual(
            summaries["CALL_CONFIRMED"].correlation_source,
            "rollout_scoped_call_id",
        )
        self.assertEqual(summaries["CALL_AMBIGUOUS"].association_status, "ambiguous")
        self.assertEqual(summaries["unresolved"].association_status, "unresolved")
        self.assertEqual(association.eligible_operations, 3)
        self.assertEqual(association.associated_operations, 2)
        self.assertAlmostEqual(association.association_coverage, 2 / 3)
        self.assertAlmostEqual(association.unresolved_rate, 1 / 3)
        self.assertIsNone(association.precision)

    def test_conflicting_process_and_call_identity_drops_update_fail_closed(self) -> None:
        store = TerminalStore()
        scope = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10)
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "process",
                    1.0,
                    process_id="PROCESS_ID",
                    terminal_candidate=True,
                    scope=scope,
                ),
                TerminalUpdate(
                    "call",
                    2.0,
                    call_id="CALL_ID",
                    terminal_candidate=True,
                    scope=scope,
                ),
            ),
        )
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "conflict",
                    3.0,
                    process_id="PROCESS_ID",
                    call_id="CALL_ID",
                    output="CONFLICT_TRANSCRIPT_SENTINEL",
                    terminal_candidate=True,
                    scope=scope,
                ),
            ),
        )

        association = store.association_summary("session")
        retained = "".join(
            chunk.text for summary in store.summaries("session") for chunk in summary.chunks
        )
        self.assertNotIn("CONFLICT_TRANSCRIPT_SENTINEL", retained)
        self.assertEqual(association.conflicting, 1)
        self.assertEqual(association.dropped, 1)
        self.assertIn(("process_call_identity_conflict", 1), association.reasons)

    def test_labeled_association_metrics_only_compute_precision_with_ground_truth(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (TerminalUpdate("source", 1.0, process_id="PROCESS_ID", terminal_candidate=True),),
        )

        unlabeled = store.association_summary("session")
        labeled = store.association_summary("session", labeled_correct=3, labeled_incorrect=1)

        self.assertIsNone(unlabeled.precision)
        self.assertEqual(labeled.precision, 0.75)

    def test_terminal_association_isolated_across_home_and_workspace_sessions(self) -> None:
        session_a = SessionIdentity(
            InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A")),
            "SESSION_ID",
        )
        session_b = SessionIdentity(
            InstanceIdentity(Path("/CODEX_HOME_B"), Path("/SQLITE_HOME_B")),
            "SESSION_ID",
        )
        scope_a = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10)
        scope_b = RolloutIdentity(Path("/workspace-b/rollout.jsonl"), 1, 10)
        store = TerminalStore()

        for session, scope, output in (
            (session_a, scope_a, "workspace-a\n"),
            (session_b, scope_b, "workspace-b\n"),
        ):
            store.apply(
                session,
                (
                    TerminalUpdate(
                        "SAME_SOURCE",
                        1.0,
                        call_id="SAME_CALL",
                        process_id="SAME_PROCESS",
                        output=output,
                        terminal_candidate=True,
                        scope=scope,
                    ),
                ),
            )

        transcript_a = "".join(chunk.text for chunk in store.summaries(session_a)[0].chunks)
        transcript_b = "".join(chunk.text for chunk in store.summaries(session_b)[0].chunks)
        self.assertEqual(transcript_a, "workspace-a\n")
        self.assertEqual(transcript_b, "workspace-b\n")
        self.assertEqual(store.association_summary(session_a).confirmed, 1)
        self.assertEqual(store.association_summary(session_b).confirmed, 1)

    def test_store_scopes_reused_call_and_process_ids_by_rollout(self) -> None:
        session = SessionIdentity(
            InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A")),
            "SESSION_ID",
        )
        first_scope = RolloutIdentity(Path("/workspace-a/first.jsonl"), 1, 10)
        second_scope = RolloutIdentity(Path("/workspace-a/second.jsonl"), 1, 11)
        store = TerminalStore()

        for ordinal, scope in enumerate((first_scope, second_scope), start=1):
            store.apply(
                session,
                (
                    TerminalUpdate(
                        "SAME_SOURCE_ID",
                        float(ordinal),
                        call_id="CALL_ID",
                        process_id="PROCESS_ID",
                        command="make watch",
                        status="running",
                        terminal_candidate=True,
                        scope=scope,
                    ),
                ),
            )

        summaries = store.summaries(session)
        self.assertEqual(len(summaries), 2)
        self.assertEqual({item.command for item in summaries}, {"make watch"})
        self.assertEqual(len({item.terminal_id for item in summaries}), 2)
        self.assertTrue(all(item.identity is not None for item in summaries))
        self.assertEqual(len({item.identity for item in summaries}), 2)

    def test_copy_truncate_starts_new_terminal_parser_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            invocation = {
                "timestamp": 1,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "CALL_ID",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "old-command --watch"}),
                },
            }
            path.write_text(json.dumps(invocation) + "\n" + (" " * 256))
            reader = RolloutReader()
            first = reader.read_with_activity(path)
            self.assertEqual(first.terminal_updates[0].command, "old-command --watch")

            completion = {
                "timestamp": 2,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "CALL_ID",
                    "output": (
                        "Script running with cell ID PROCESS_ID\n"
                        "Wall time 1 seconds\nOutput:\nnew output\n"
                    ),
                },
            }
            path.write_text(json.dumps(completion) + "\n")
            second = reader.read_with_activity(path)

        self.assertTrue(second.activity.truncated or second.activity.copy_truncated)
        self.assertEqual(second.activity.generation, 1)
        self.assertTrue(second.activity.stream_uncertain)
        self.assertEqual(second.terminal_updates[0].command, "")
        self.assertNotEqual(
            first.terminal_updates[0].source_id,
            second.terminal_updates[0].source_id,
        )
        self.assertNotEqual(
            first.terminal_updates[0].scope,
            second.terminal_updates[0].scope,
        )

    def test_newer_rollout_scope_takes_over_same_os_child(self) -> None:
        session = SessionIdentity(
            InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A")),
            "SESSION_ID",
        )
        first_scope = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10, 0)
        second_scope = RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10, 1)
        store = TerminalStore()
        for observed_at, scope in ((1.0, first_scope), (2.0, second_scope)):
            store.apply(
                session,
                (
                    TerminalUpdate(
                        "SAME_SOURCE_ID",
                        observed_at,
                        call_id="CALL_ID",
                        process_id="PROCESS_ID",
                        command="make watch",
                        status="running",
                        terminal_candidate=True,
                        scope=scope,
                    ),
                ),
            )
        child = ChildProcessActivity(
            ProcessIdentity(42, 100),
            command="make watch",
            state="S",
        )

        store.reconcile_children(session, (child,), 3.0)

        summaries = store.summaries(session)
        self.assertEqual(len(summaries), 2)
        self.assertFalse(summaries[0].process_active)
        self.assertTrue(summaries[1].process_active)
        self.assertEqual(store.current_summaries(session), [summaries[1]])

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
                        "arguments": json.dumps({"cmd": "make watch", "workdir": "/workspace-a"}),
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
                        "const result = await tools.exec_command("
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
                        'const result = await tools.write_stdin({"session_id":777,"chars":""});'
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

    def test_completed_command_output_does_not_parse_embedded_process_fixture(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-source",
                    "name": "exec",
                    "input": (
                        "const result = await tools.exec_command("
                        '{"cmd":"sed -n 1,80p tests/test_terminal.py"});'
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-source",
                    "output": (
                        "Script completed\nWall time 0.1 seconds\nOutput:\n"
                        'fixture = "Process running with session ID 321\\n'
                        'Wall time: 1 second\\nOutput:\\nready"\n'
                    ),
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
        self.assertEqual(terminal.process_id, "")
        self.assertEqual(terminal.status, "completed")
        self.assertEqual(store.current_summaries("session"), [])

    def test_unified_exec_json_result_exposes_background_session_id(self) -> None:
        updates = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-background",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 1.2 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "wall_time_seconds": 1.0,
                                    "session_id": 79038,
                                    "output": "ready\n",
                                }
                            ),
                        },
                    ],
                },
            },
            "source",
            1.0,
        )

        self.assertEqual(updates[0].process_id, "79038")
        self.assertEqual(updates[0].status, "running")
        self.assertEqual(updates[0].output, "ready\n")

    def test_parallel_mixed_tool_batch_preserves_each_terminal_identity(self) -> None:
        parser = TerminalProtocolParser()
        start = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-batch",
                "name": "exec",
                "input": """
const fixture = "tools.exec_command({cmd: 'not a real call'})";
// tools.exec_command({cmd: "also not real"});
const results = await Promise.all([
  tools.exec_command({cmd: "ruff check src", workdir: "/workspace-a"}),
  tools.view_image({path: "/workspace-a/screenshot.png"}),
  tools.exec_command({
    cmd: "python -m unittest discover -s tests -v",
    workdir: "/workspace-a"
  })
]);
""",
            },
        }
        output = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-batch",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 1.0 seconds\nOutput:\n",
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "wall_time_seconds": 0.2,
                                "exit_code": 0,
                                "output": "All checks passed!\n",
                            }
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps({"image_url": "data:image/png;base64,fixture"}),
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "wall_time_seconds": 1.0,
                                "session_id": 73002,
                                "output": "test_core ... ok\n",
                            }
                        ),
                    },
                ],
            },
        }

        initial = extract_terminal_updates(start, "start", 1.0, parser=parser)
        results = extract_terminal_updates(output, "output", 2.0, parser=parser)

        self.assertEqual(
            [(item.call_id, item.command) for item in initial],
            [
                ("call-batch:tool:0", "ruff check src"),
                (
                    "call-batch:tool:2",
                    "python -m unittest discover -s tests -v",
                ),
            ],
        )
        self.assertEqual([item.call_id for item in results], [item.call_id for item in initial])
        self.assertEqual(results[1].process_id, "73002")

        store = TerminalStore()
        store.apply("session", (*initial, *results))
        summaries = {item.command: item for item in store.summaries("session")}
        self.assertEqual(summaries["ruff check src"].status, "completed")
        test_command = "python -m unittest discover -s tests -v"
        self.assertEqual(summaries[test_command].process_id, "73002")
        self.assertEqual(summaries[test_command].status, "running")

        child = ChildProcessActivity(
            ProcessIdentity(42, 7),
            command=f"/bin/bash -c {test_command}",
            state="S",
        )
        store.reconcile_children("session", (child,), 3.0)
        self.assertEqual(
            [item.command for item in store.current_summaries("session")],
            [test_command],
        )
        store.reconcile_children("session", (), 4.0)
        store.reconcile_children("session", (), 5.0)
        self.assertEqual(store.current_summaries("session"), [])

    def test_rollout_reader_pairs_parallel_batch_across_incremental_reads(self) -> None:
        start = {
            "timestamp": "2026-07-17T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-batch",
                "name": "exec",
                "input": (
                    "const values = await Promise.all(["
                    'tools.exec_command({cmd:"ruff check src"}),'
                    'tools.exec_command({cmd:"python -m unittest discover -s tests -v"})'
                    "]);"
                ),
            },
        }
        output = {
            "timestamp": "2026-07-17T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-batch",
                "output": [
                    {"type": "input_text", "text": "Script completed\nOutput:\n"},
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"wall_time_seconds": 0.1, "exit_code": 0, "output": "ok\n"}
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "wall_time_seconds": 1.0,
                                "session_id": 73002,
                                "output": "test_core ... ok\n",
                            }
                        ),
                    },
                ],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(json.dumps(start) + "\n")
            reader = RolloutReader()
            initial = reader.read_with_activity(path)
            with path.open("a") as handle:
                handle.write(json.dumps(output) + "\n")
            completed = reader.read_with_activity(path)

        self.assertEqual(len(initial.terminal_updates), 2)
        self.assertEqual(len(completed.terminal_updates), 2)
        self.assertEqual(
            completed.terminal_updates[1].command,
            "python -m unittest discover -s tests -v",
        )
        self.assertEqual(completed.terminal_updates[1].process_id, "73002")

    def test_parser_scopes_identical_call_ids_to_their_rollout_stream(self) -> None:
        parser = TerminalProtocolParser()

        def invocation(command: str) -> dict[str, object]:
            return {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "shared-call",
                    "name": "exec",
                    "input": f'await tools.exec_command({{"cmd":{json.dumps(command)}}});',
                },
            }

        def result(process_id: int) -> dict[str, object]:
            return {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "shared-call",
                    "output": [
                        {"type": "input_text", "text": "Script completed\nOutput:\n"},
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "wall_time_seconds": 1.0,
                                    "session_id": process_id,
                                    "output": "ready\n",
                                }
                            ),
                        },
                    ],
                },
            }

        parser.parse(invocation("worker-a"), "a-start", 1.0, "rollout-a")
        parser.parse(invocation("worker-b"), "b-start", 1.0, "rollout-b")
        result_b = parser.parse(result(202), "b-output", 2.0, "rollout-b")
        result_a = parser.parse(result(101), "a-output", 2.0, "rollout-a")

        self.assertEqual((result_a[0].command, result_a[0].process_id), ("worker-a", "101"))
        self.assertEqual((result_b[0].command, result_b[0].process_id), ("worker-b", "202"))

    def test_parser_bounds_pending_batches_and_keeps_newest(self) -> None:
        parser = TerminalProtocolParser(max_pending_batches=2)
        for index in range(3):
            parser.parse(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": f"call-{index}",
                        "name": "exec",
                        "input": f'await tools.exec_command({{"cmd":"worker-{index}"}});',
                    },
                },
                f"source-{index}",
                float(index),
                "rollout",
            )

        self.assertEqual(
            [call_id for _scope, call_id in parser.pending_batches],
            ["call-1", "call-2"],
        )
        self.assertEqual(parser.pending_batch_evictions, 1)
        self.assertEqual(parser.pending_batch_eviction_reason, "pending_batch_limit")

    def test_wrapper_output_fragments_keep_explicit_session_with_single_call(self) -> None:
        parser = TerminalProtocolParser()
        start = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-fragments",
                "name": "exec",
                "input": (
                    "const r = await tools.exec_command({"
                    'cmd:"python -m unittest discover -s tests -v"});'
                    "text(r.output);"
                    "if (r.session_id) text(`SESSION_ID=${r.session_id}`);"
                ),
            },
        }
        output = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-fragments",
                "output": [
                    {"type": "input_text", "text": "Script completed\nOutput:\n"},
                    {"type": "input_text", "text": "test_core ... ok\n"},
                    {"type": "input_text", "text": "SESSION_ID=73001"},
                ],
            },
        }

        parser.parse(start, "start", 1.0, "rollout")
        updates = parser.parse(output, "output", 2.0, "rollout")

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].process_id, "73001")
        self.assertEqual(updates[0].status, "running")
        self.assertEqual(updates[0].command, "python -m unittest discover -s tests -v")
        self.assertIn("test_core ... ok", updates[0].output)
        self.assertNotIn("SESSION_ID", updates[0].output)

    def test_stateless_wrapper_fragments_keep_outer_call_and_transcript(self) -> None:
        updates = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-fragments",
                    "output": [
                        {"type": "input_text", "text": "Script completed\nOutput:\n"},
                        {"type": "input_text", "text": "test_core ... ok\n"},
                        {"type": "input_text", "text": "SESSION_ID=73001"},
                    ],
                },
            },
            "output",
            2.0,
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].call_id, "call-fragments")
        self.assertEqual(updates[0].process_id, "73001")
        self.assertIn("test_core ... ok", updates[0].output)

    def test_plain_json_command_output_is_not_a_background_result(self) -> None:
        updates = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-json",
                    "output": (
                        "Script completed\nWall time 0.1 seconds\nOutput:\n"
                        '{"session_id": 42, "output": "application data"}'
                    ),
                },
            },
            "source",
            1.0,
        )

        self.assertEqual(updates[0].process_id, "")
        self.assertEqual(updates[0].status, "completed")

    def test_cold_start_backfills_terminal_metadata_without_old_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-07-16T23:59:58Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "unrelated-start",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "old watch"}),
                    },
                },
                {
                    "timestamp": "2026-07-16T23:59:59Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "unrelated-start",
                        "output": (
                            "Script running with cell ID 999\n"
                            "Wall time 1 seconds\nOutput:\nunrelated output\n"
                        ),
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-start",
                        "name": "exec",
                        "input": (
                            'await tools.exec_command({cmd:"npm run dev", workdir:"/workspace-a"});'
                        ),
                    },
                },
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-start",
                        "output": (
                            "Script running with cell ID 321\n"
                            "Wall time 1 seconds\nOutput:\nold output\n"
                        ),
                    },
                },
            ]
            records.extend(
                {
                    "timestamp": "2026-07-17T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "padding": "x" * 120},
                }
                for _ in range(12)
            )
            records.extend(
                [
                    {
                        "timestamp": "2026-07-17T00:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call-poll",
                            "name": "exec",
                            "input": 'await tools.write_stdin({session_id:321, chars:""});',
                        },
                    },
                    {
                        "timestamp": "2026-07-17T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-poll",
                            "output": "recent output\n",
                        },
                    },
                ]
            )
            path.write_text("".join(json.dumps(record) + "\n" for record in records))

            with (
                patch("codex.rollout.MAX_SESSION_TAIL", 700),
                patch("codex.rollout.MAX_TERMINAL_METADATA_BACKFILL", 16 * 1024),
            ):
                reader = RolloutReader()
                result = reader.read_with_activity(path)

        store = TerminalStore()
        store.apply("session", result.terminal_updates)
        terminals = store.summaries("session")
        self.assertEqual(len(terminals), 1)
        terminal = terminals[0]
        self.assertTrue(reader.has_truncated_context({str(path)}))
        self.assertEqual(terminal.process_id, "321")
        self.assertEqual(terminal.command, "npm run dev")
        self.assertEqual(terminal.cwd, "/workspace-a")
        self.assertEqual("".join(chunk.text for chunk in terminal.chunks), "recent output\n")

    def test_metadata_backfill_is_chunked_and_disabled_on_fast_refresh(self) -> None:
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
                            {"cmd": "python worker.py", "workdir": "/workspace-a"}
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
                            "Wall time 1 seconds\nOutput:\nold output\n"
                        ),
                    },
                },
            ]
            records.extend(
                {
                    "timestamp": "2026-07-17T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "padding": "x" * 100},
                }
                for _ in range(30)
            )
            records.extend(
                [
                    {
                        "timestamp": "2026-07-17T00:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-poll",
                            "name": "write_stdin",
                            "arguments": json.dumps({"session_id": 321, "chars": ""}),
                        },
                    },
                    {
                        "timestamp": "2026-07-17T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-poll",
                            "output": "recent output\n",
                        },
                    },
                ]
            )
            path.write_text("".join(json.dumps(record) + "\n" for record in records))

            with (
                patch("codex.rollout.MAX_SESSION_TAIL", 700),
                patch("codex.rollout.MAX_TERMINAL_METADATA_BACKFILL", 8 * 1024),
                patch("codex.rollout.MAX_TERMINAL_METADATA_BACKFILL_CHUNK", 256),
                patch("codex.rollout.TERMINAL_METADATA_LINE_OVERLAP", 512),
            ):
                reader = RolloutReader()
                store = TerminalStore()
                first = reader.read_with_activity(path)
                store.apply("session", first.terminal_updates)
                self.assertEqual(store.summaries("session")[0].command, "")
                pending = reader.terminal_metadata_backfills[str(path)]
                after_first_chunk = pending.next_end

                fast = reader.read_with_activity(
                    path,
                    allow_terminal_metadata_backfill=False,
                )
                store.apply("session", fast.terminal_updates)
                self.assertEqual(pending.next_end, after_first_chunk)
                self.assertEqual(fast.terminal_updates, ())

                for _ in range(30):
                    result = reader.read_with_activity(path)
                    store.apply("session", result.terminal_updates)
                    if store.summaries("session")[0].command:
                        break

        terminal = store.summaries("session")[0]
        self.assertEqual(terminal.command, "python worker.py")
        self.assertEqual(terminal.cwd, "/workspace-a")
        self.assertEqual("".join(chunk.text for chunk in terminal.chunks), "recent output\n")

    def test_metadata_backfill_skips_invalid_utf8_and_keeps_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            valid = json.dumps(
                {
                    "timestamp": "2026-07-17T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-start",
                        "output": "Script running with cell ID 321\nWall time 1 seconds\nOutput:\n",
                    },
                }
            ).encode()
            path.write_bytes(b'{"output":"Script completed \xff"}\n' + valid + b"\n")
            stat = path.stat()
            state = TerminalMetadataBackfillCursor(
                inode=stat.st_ino,
                next_end=stat.st_size,
                floor=0,
                process_ids={"321"},
                process_call_ids={"321": set()},
            )

            updates, _finished = RolloutReader()._terminal_metadata_backfill_step(
                path,
                state,
                512 * 1024,
            )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].process_id, "321")

    def test_metadata_backfill_private_identity_state_is_bounded_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("{}\n")
            stat = path.stat()
            reader = RolloutReader()
            reader.bootstrap_truncated.add(str(path))
            updates = tuple(
                TerminalUpdate(
                    f"source-{index}",
                    float(index),
                    process_id=f"PROCESS_{index}",
                    terminal_candidate=True,
                    scope=RolloutIdentity(path, stat.st_dev, stat.st_ino, 0),
                )
                for index in range(6)
            )

            with patch("codex.rollout.MAX_TERMINAL_METADATA_PROCESS_IDS", 2):
                reader._advance_terminal_metadata_backfill(
                    path,
                    stat.st_size,
                    updates,
                    inode=stat.st_ino,
                    generation=0,
                    allow=False,
                    max_bytes=0,
                )
                state = reader.terminal_metadata_backfills[str(path)]
                first_dropped = reader.terminal_metadata_dropped[str(path)]
                reader._advance_terminal_metadata_backfill(
                    path,
                    stat.st_size,
                    (
                        TerminalUpdate(
                            "later-source",
                            10.0,
                            process_id="LATER_PROCESS",
                            terminal_candidate=True,
                            scope=RolloutIdentity(path, stat.st_dev, stat.st_ino, 0),
                        ),
                    ),
                    inode=stat.st_ino,
                    generation=0,
                    allow=False,
                    max_bytes=0,
                )

            self.assertEqual(len(state.process_ids), 2)
            self.assertIn(str(path), reader.terminal_metadata_saturated)
            self.assertGreaterEqual(first_dropped, 4)
            self.assertGreater(reader.terminal_metadata_dropped[str(path)], first_dropped)

    def test_later_unknown_process_can_start_backfill_after_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            initial = [
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-start",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {"cmd": "python worker.py", "workdir": "/workspace-a"}
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
                            "Script running with cell ID 321\nWall time 1 seconds\nOutput:\nready\n"
                        ),
                    },
                },
            ]
            initial.extend(
                {
                    "timestamp": "2026-07-17T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "padding": "x" * 120},
                }
                for _ in range(20)
            )
            path.write_text("".join(json.dumps(record) + "\n" for record in initial))

            with patch("codex.rollout.MAX_SESSION_TAIL", 700):
                reader = RolloutReader()
                first = reader.read_with_activity(path)
                self.assertEqual(first.terminal_updates, ())

                later = [
                    {
                        "timestamp": "2026-07-17T00:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-poll",
                            "name": "write_stdin",
                            "arguments": json.dumps({"session_id": 321, "chars": ""}),
                        },
                    },
                    {
                        "timestamp": "2026-07-17T00:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-poll",
                            "output": "request complete\n",
                        },
                    },
                ]
                with path.open("a") as handle:
                    handle.write("".join(json.dumps(record) + "\n" for record in later))
                result = reader.read_with_activity(path)

        store = TerminalStore()
        store.apply("session", result.terminal_updates)
        terminal = store.summaries("session")[0]
        self.assertEqual(terminal.process_id, "321")
        self.assertEqual(terminal.command, "python worker.py")
        self.assertEqual(terminal.cwd, "/workspace-a")

    def test_wait_completion_removes_background_process_from_running_state(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-start",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "python worker.py"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-start",
                    "output": (
                        "Script running with cell ID 321\nWall time 1 seconds\nOutput:\nready\n"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-wait",
                    "name": "wait",
                    "arguments": json.dumps({"cell_id": 321}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-wait",
                    "output": "Script completed\nWall time 1 second\nOutput:\ndone\n",
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
        self.assertEqual(terminal.process_id, "321")
        self.assertEqual(terminal.status, "completed")
        self.assertEqual(terminal.completed_at, 3.0)

    def test_process_reconciliation_has_hysteresis_and_preserves_rollout_evidence(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "python",
                    1.0,
                    call_id="call-python",
                    process_id="cell-python",
                    command="python worker.py",
                    status="running",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "npm",
                    2.0,
                    call_id="call-npm",
                    process_id="cell-npm",
                    command="npm run dev",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        python_child = ChildProcessActivity(
            ProcessIdentity(42, 7),
            command="python3.11",
            state="S",
        )
        npm_child = ChildProcessActivity(
            ProcessIdentity(43, 8),
            command="npm",
            state="S",
        )

        self.assertFalse(store.reconcile_children("session", (python_child, npm_child), 3.0))
        matched = {item.process_id: item for item in store.summaries("session")}
        self.assertTrue(matched["cell-python"].process_active)
        self.assertTrue(matched["cell-npm"].process_active)
        self.assertEqual(
            {item.process_id for item in store.current_summaries("session")},
            {"cell-python", "cell-npm"},
        )
        self.assertFalse(store.reconcile_children("session", (python_child,), 4.0))
        missing_once = {item.process_id: item for item in store.summaries("session")}
        self.assertTrue(missing_once["cell-python"].process_active)
        self.assertFalse(missing_once["cell-npm"].process_active)
        self.assertEqual(missing_once["cell-npm"].status, "running")
        self.assertEqual(
            [item.process_id for item in store.current_summaries("session")],
            ["cell-python"],
        )
        self.assertTrue(store.reconcile_children("session", (python_child,), 5.0))
        summaries = {item.process_id: item for item in store.summaries("session")}
        self.assertEqual(summaries["cell-python"].status, "running")
        self.assertTrue(summaries["cell-python"].process_active)
        self.assertEqual(summaries["cell-npm"].status, "completed")
        self.assertFalse(summaries["cell-npm"].process_active)

        transient_store = TerminalStore()
        transient_store.apply(
            "session",
            (
                TerminalUpdate(
                    "rollout",
                    1.0,
                    process_id="cell-1",
                    command="server --watch",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        server = ChildProcessActivity(ProcessIdentity(42, 7), command="server", state="S")
        self.assertFalse(transient_store.reconcile_children("session", (server,), 2.0))
        self.assertFalse(transient_store.reconcile_children("session", (), 3.0))
        self.assertFalse(transient_store.reconcile_children("session", (server,), 4.0))
        self.assertFalse(transient_store.reconcile_children("session", (), 5.0))
        self.assertTrue(transient_store.reconcile_children("session", (), 6.0))
        self.assertEqual(transient_store.summaries("session")[0].status, "completed")

        unrelated_store = TerminalStore()
        unrelated_store.apply(
            "session",
            (
                TerminalUpdate(
                    "rollout",
                    1.0,
                    process_id="cell-unrelated",
                    command="python worker.py",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        unrelated_child = ChildProcessActivity(
            ProcessIdentity(99, 1), command="mcp-server", state="S"
        )
        self.assertFalse(unrelated_store.reconcile_children("session", (unrelated_child,), 2.0))
        self.assertFalse(unrelated_store.summaries("session")[0].process_active)

        unconfirmed_store = TerminalStore()
        unconfirmed_store.apply(
            "session",
            (
                TerminalUpdate(
                    "rollout",
                    1.0,
                    process_id="cell-1",
                    command="server",
                    status="running",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "file",
                    1.0,
                    process_id="os:42:7",
                    command="logger",
                    status="running",
                    capability=TerminalCapability.FILE_TAIL,
                    terminal_candidate=True,
                    source="file-tail",
                ),
            ),
        )

        self.assertFalse(unconfirmed_store.reconcile_children("session", (), 2.0))
        self.assertFalse(unconfirmed_store.reconcile_children("session", (), 3.0))
        summaries = {item.process_id: item for item in unconfirmed_store.summaries("session")}
        self.assertEqual(summaries["cell-1"].status, "running")
        self.assertFalse(summaries["cell-1"].process_active)
        self.assertEqual(summaries["os:42:7"].status, "running")
        self.assertTrue(summaries["os:42:7"].process_active)
        self.assertEqual(
            [item.process_id for item in unconfirmed_store.current_summaries("session")],
            ["os:42:7"],
        )

        newer_store = TerminalStore()
        newer_store.apply(
            "session",
            (
                TerminalUpdate(
                    "new-process",
                    4.0,
                    process_id="cell-new",
                    command="server",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )

        self.assertFalse(
            newer_store.reconcile_children(
                "session",
                (),
                5.0,
                evidence_cutoff=3.0,
            )
        )
        self.assertEqual(newer_store.summaries("session")[0].status, "running")

        store.mark_process_unavailable("session")
        unavailable = {item.process_id: item for item in store.summaries("session")}
        self.assertFalse(unavailable["cell-python"].process_active)
        self.assertEqual(store.current_summaries("session"), [])

        in_progress_store = TerminalStore()
        in_progress_store.apply(
            "session",
            (
                TerminalUpdate(
                    "progress",
                    1.0,
                    process_id="cell-progress",
                    command="server",
                    status="in_progress",
                    terminal_candidate=True,
                ),
            ),
        )
        progress = in_progress_store.sessions["session"]["cell-progress"]
        progress.process_active = True
        in_progress_store.mark_stale("session")
        progress_summary = in_progress_store.summaries("session")[0]
        self.assertTrue(progress_summary.stale)
        self.assertFalse(progress_summary.process_active)
        self.assertEqual(in_progress_store.current_summaries("session"), [])

    def test_process_reconciliation_publishes_unbackfilled_background_job_roots(
        self,
    ) -> None:
        store = TerminalStore()
        children = (
            ChildProcessActivity(
                ProcessIdentity(100, 10),
                parent_pid=1,
                command="bwrap --new-session -- python",
                state="S",
                elapsed_seconds=60.0,
            ),
            ChildProcessActivity(
                ProcessIdentity(101, 11),
                parent_pid=100,
                command="bash -c .venv/bin/python -m unittest tests.test_tui -q",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(102, 12),
                parent_pid=101,
                command=".venv/bin/python -m unittest tests.test_tui -q",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(200, 20),
                parent_pid=1,
                command="codex-linux-sandbox -- sleep 600",
                state="S",
                elapsed_seconds=60.0,
            ),
            ChildProcessActivity(
                ProcessIdentity(201, 21),
                parent_pid=200,
                command="sleep 600",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(300, 30),
                parent_pid=1,
                command="codex-code-mode-host",
                state="S",
            ),
        )

        self.assertTrue(
            store.reconcile_children(
                "session",
                children,
                30.0,
                workspace="/workspace",
            )
        )
        current = store.current_summaries("session")
        self.assertEqual(len(current), 2)
        self.assertEqual(
            {item.process_id for item in current},
            {"os:100:10", "os:200:20"},
        )
        self.assertEqual(
            {item.command for item in current},
            {
                ".venv/bin/python -m unittest tests.test_tui -q",
                "sleep 600",
            },
        )
        self.assertTrue(
            all(item.capability == TerminalCapability.METADATA_ONLY for item in current)
        )
        self.assertTrue(all(item.source == "process" for item in current))
        self.assertTrue(all(item.cwd == "/workspace" for item in current))

        self.assertTrue(store.reconcile_children("session", (children[-1],), 31.0))
        self.assertEqual(store.current_summaries("session"), [])

    def test_process_reconciliation_matches_shell_wrapped_command(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "running",
                    1.0,
                    process_id="cell-1",
                    command="python worker.py",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        child = ChildProcessActivity(
            ProcessIdentity(42, 7),
            command="/bin/bash -c python worker.py",
            state="S",
        )

        store.reconcile_children("session", (child,), 2.0)

        terminal = store.current_summaries("session")[0]
        self.assertEqual(terminal.process_id, "cell-1")
        self.assertTrue(terminal.process_active)

    def test_process_reconciliation_matches_expanded_shell_c_script(self) -> None:
        command = (
            'sh -c \'printf "codexdeck-live-probe-start\\n"; sleep 90; '
            'printf "codexdeck-live-probe-end\\n"\''
        )
        child_command = (
            '/sandbox -- /bin/bash -c sh -c printf "codexdeck-live-probe-start\\n"; '
            'sleep 90; printf "codexdeck-live-probe-end\\n"'
        )

        self.assertTrue(TerminalStore._command_matches_child(command, child_command))
        self.assertFalse(
            TerminalStore._command_matches_child(
                "sh -c 'printf other-probe; sleep 30'",
                child_command,
            )
        )

    def test_process_reconciliation_does_not_match_shared_runtime_tokens(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "old-suite",
                    1.0,
                    process_id="cell-old",
                    command="python -m unittest discover -s tests -v",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        child = ChildProcessActivity(
            ProcessIdentity(42, 7),
            command="python -m unittest tests.test_tui -q",
            state="S",
        )

        store.reconcile_children("session", (child,), 2.0)

        self.assertEqual(store.current_summaries("session"), [])

    def test_file_tail_descendant_merges_into_rollout_terminal(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "rollout",
                    1.0,
                    call_id="call-watch",
                    process_id="cell-watch",
                    command="for module in tests; do run-test $module; done",
                    status="running",
                    capability=TerminalCapability.POLL_TRANSCRIPT,
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "file",
                    2.0,
                    process_id="os:102:12",
                    command="run-test tests.test_tui",
                    status="running",
                    output="progress\n",
                    capability=TerminalCapability.FILE_TAIL,
                    terminal_candidate=True,
                    source="file-tail",
                ),
            ),
        )
        children = (
            ChildProcessActivity(
                ProcessIdentity(100, 10),
                command=(
                    "codex-linux-sandbox -- /bin/bash -c "
                    "for module in tests; do run-test $module; done"
                ),
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(101, 11),
                parent_pid=100,
                command="/bin/bash -c run-test tests.test_tui",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(102, 12),
                parent_pid=101,
                command="run-test tests.test_tui",
                state="S",
            ),
        )

        self.assertTrue(store.reconcile_children("session", children, 3.0))

        terminals = store.current_summaries("session")
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].process_id, "cell-watch")
        self.assertEqual(terminals[0].capability, TerminalCapability.FILE_TAIL)
        self.assertEqual(terminals[0].chunks[0].text, "progress\n")

    def test_os_child_reopens_completed_nested_exec_without_protocol_process_id(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "old-call",
                    0.5,
                    call_id="call-old",
                    command="git status --short",
                    status="completed",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "call",
                    1.0,
                    call_id="call-1",
                    command="printf ready; while :; do sleep 30; done",
                    status="running",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "outer-result",
                    2.0,
                    call_id="call-1",
                    status="completed",
                    output="ready\n",
                    capability=TerminalCapability.FINAL_TRANSCRIPT,
                    terminal_candidate=True,
                ),
            ),
        )
        child = ChildProcessActivity(
            ProcessIdentity(157982, 77),
            command=(
                "/usr/bin/codex-linux-sandbox -- /bin/bash -lc "
                "printf ready; while :; do sleep 30; done"
            ),
            state="S",
        )

        self.assertFalse(store.reconcile_children("session", (child,), 3.0))
        self.assertEqual(store.current_summaries("session"), [])
        self.assertTrue(store.reconcile_children("session", (child,), 4.0))

        terminal = store.current_summaries("session")[0]
        self.assertEqual(terminal.process_id, "os:157982:77")
        self.assertEqual(terminal.status, "running")
        self.assertTrue(terminal.process_active)
        self.assertEqual(terminal.chunks[0].text, "ready\n")
        old_terminal = next(
            item for item in store.summaries("session") if item.command == "git status --short"
        )
        self.assertEqual(old_terminal.status, "completed")
        self.assertFalse(old_terminal.process_active)

    def test_reconciliation_uses_one_process_tree_root_per_background_job(self) -> None:
        store = TerminalStore()
        command = "while :; do sleep 30; done"
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "old-result",
                    1.0,
                    call_id="call-old",
                    command=command,
                    status="completed",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "new-result",
                    2.0,
                    call_id="call-new",
                    command=command,
                    status="completed",
                    terminal_candidate=True,
                ),
            ),
        )
        children = (
            ChildProcessActivity(
                ProcessIdentity(50, 5),
                parent_pid=10,
                command="codex-code-mode-host",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(100, 10),
                parent_pid=50,
                command=f"codex-linux-sandbox -- /bin/bash -lc {command}",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(101, 11),
                parent_pid=100,
                command=f"bwrap -- /bin/bash -lc {command}",
                state="S",
            ),
            ChildProcessActivity(
                ProcessIdentity(102, 12),
                parent_pid=101,
                command=f"/bin/bash -lc {command}",
                state="S",
            ),
        )

        store.reconcile_children("session", children, 3.0)
        self.assertEqual(store.current_summaries("session"), [])
        store.reconcile_children("session", children, 4.0)

        current = store.current_summaries("session")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].root_call_id, "call-new")
        self.assertEqual(current[0].process_id, "os:100:10")

    def test_running_call_without_background_result_is_not_published(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "call",
                    1.0,
                    call_id="call-foreground",
                    command="make test",
                    status="running",
                    terminal_candidate=True,
                ),
            ),
        )
        child = ChildProcessActivity(
            ProcessIdentity(42, 7),
            parent_pid=10,
            command="codex-linux-sandbox -- make test",
            state="S",
        )

        store.reconcile_children("session", (child,), 2.0)

        self.assertEqual(store.current_summaries("session"), [])

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
        self.assertEqual(store.current_summaries("session"), [])
        self.assertEqual(
            "".join(chunk.text for chunk in terminals[0].chunks),
            "ready\ncomplete\n",
        )

    def test_serialized_background_process_id_stops_before_escape_delimiter(self) -> None:
        updates = extract_terminal_updates(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": (
                        "Process running with session ID 321\\n"
                        "Wall time: 1 seconds\\nOutput:\\nready\\n"
                    ),
                },
            },
            "serialized",
            1.0,
        )

        self.assertEqual(updates[0].process_id, "321")

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

    def test_session_retention_prefers_confirmed_evidence_over_metadata_ghosts(self) -> None:
        store = TerminalStore()
        store.apply(
            "session",
            (
                TerminalUpdate(
                    "completed-transcript",
                    1.0,
                    call_id="completed-transcript",
                    command="make completed",
                    status="completed",
                    output="done\n",
                    capability=TerminalCapability.FINAL_TRANSCRIPT,
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "protocol-process",
                    2.0,
                    call_id="protocol-process",
                    process_id="PROCESS_ID",
                    command="make watch",
                    status="running",
                    terminal_candidate=True,
                ),
                TerminalUpdate(
                    "active-file-tail",
                    3.0,
                    process_id="os:42:7",
                    command="tail worker.log",
                    status="running",
                    output="ready\n",
                    capability=TerminalCapability.FILE_TAIL,
                    terminal_candidate=True,
                    source="file-tail",
                ),
            ),
        )

        for ordinal in range(16):
            store.apply(
                "session",
                (
                    TerminalUpdate(
                        f"metadata-{ordinal}",
                        100.0 + ordinal,
                        call_id=f"metadata-{ordinal}",
                        command=f"metadata command {ordinal}",
                        status="running",
                        terminal_candidate=True,
                    ),
                ),
            )

        summaries = store.summaries("session")
        terminal_ids = {item.terminal_id for item in summaries}
        self.assertEqual(len(summaries), 16)
        self.assertIn("completed-transcript", terminal_ids)
        self.assertIn("PROCESS_ID", terminal_ids)
        self.assertIn("os:42:7", terminal_ids)
        self.assertNotIn("metadata-0", terminal_ids)
        self.assertNotIn("metadata-1", terminal_ids)
        self.assertNotIn("metadata-2", terminal_ids)

    def test_terminal_control_sequences_are_not_replayed(self) -> None:
        text = sanitize_terminal_text("start\x1b[2J\x1b]52;c;SECRET\x07\x1b[31mred\x1b[0m\rnext")

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
            child = ChildProcessActivity(ProcessIdentity(42, 7), command="server", state="S")
            collector = RegularFileTailCollector(root / "proc")

            first = collector.read("session", str(workspace), (child,), 1.0)
            with log.open("a") as handle:
                handle.write("request\n")
            second = collector.read("session", str(workspace), (child,), 2.0)
            closed = collector.read("session", str(workspace), (), 3.0)

        first_output = next(update for update in first if update.output)
        second_output = next(update for update in second if update.output)
        self.assertEqual(first_output.capability, TerminalCapability.FILE_TAIL)
        self.assertEqual(first_output.stream, "stdout")
        self.assertEqual(first_output.output, "ready\n")
        self.assertEqual(second_output.output, "request\n")
        self.assertTrue(any(update.status == "running" for update in first))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].process_id, "os:42:7")
        self.assertEqual(closed[0].status, "completed")

    def test_regular_file_tail_deduplicates_shared_file_and_skips_observer_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "shared.log"
            log.write_text("ready\n")
            for pid in (42, 43, 44, 50):
                fd_dir = root / "proc" / str(pid) / "fd"
                fd_dir.mkdir(parents=True)
                (fd_dir / "1").symlink_to(log)
            children = (
                ChildProcessActivity(ProcessIdentity(42, 7), command="worker", state="S"),
                ChildProcessActivity(
                    ProcessIdentity(43, 8), parent_pid=42, command="worker-child", state="S"
                ),
                ChildProcessActivity(
                    ProcessIdentity(44, 9), parent_pid=43, command="collector", state="S"
                ),
                ChildProcessActivity(
                    ProcessIdentity(50, 10), command="independent-worker", state="S"
                ),
            )
            collector = RegularFileTailCollector(root / "proc")

            with patch("codex.terminal.os.getpid", return_value=44):
                updates = collector.read("session", str(workspace), children, 1.0)

        active = [update for update in updates if update.status == "running"]
        outputs = [update for update in updates if update.output]
        self.assertEqual({update.process_id for update in active}, {"os:50:10"})
        self.assertEqual(len(outputs), 1)

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

        self.assertEqual(
            "".join(update.output for batch in updates for update in batch),
            "好",
        )

    def test_regular_file_tail_discards_descriptor_reuse_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            allowed = workspace / "allowed.log"
            replacement = workspace / "replacement.log"
            allowed.write_text("allowed\n")
            replacement.write_text("replacement\n")
            descriptor = root / "proc" / "42" / "fd" / "1"
            descriptor.parent.mkdir(parents=True)
            descriptor.symlink_to(allowed)
            child = ChildProcessActivity(ProcessIdentity(42, 7), command="server")
            collector = RegularFileTailCollector(root / "proc")
            original_open = Path.open

            def replace_before_open(path: Path, *args: object, **kwargs: object):
                if path == descriptor:
                    path.unlink()
                    path.symlink_to(replacement)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", replace_before_open):
                updates = collector.read("session", str(workspace), (child,), 1.0)

        self.assertEqual(updates, ())
        self.assertEqual(collector.cursors, {})
        self.assertEqual(len(collector.diagnostics), 1)
        self.assertEqual(collector.diagnostics[0].reason, "opened_identity_mismatch")
        self.assertEqual(collector.diagnostics[0].fds, (1,))

    def test_regular_file_tail_scopes_cursor_to_process_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "server.log"
            log.write_text("first\n")
            descriptor = root / "proc" / "42" / "fd" / "1"
            descriptor.parent.mkdir(parents=True)
            descriptor.symlink_to(log)
            collector = RegularFileTailCollector(root / "proc")

            first = collector.read(
                "session",
                str(workspace),
                (ChildProcessActivity(ProcessIdentity(42, 7), command="server"),),
                1.0,
            )
            log.write_text("second\n")
            second = collector.read(
                "session",
                str(workspace),
                (ChildProcessActivity(ProcessIdentity(42, 8), command="server"),),
                2.0,
            )

        first_processes = {update.process_id for update in first}
        second_processes = {update.process_id for update in second}
        self.assertEqual(first_processes, {"os:42:7"})
        self.assertIn("os:42:8", second_processes)
        self.assertIn("os:42:7", second_processes)
        self.assertEqual(
            "".join(update.output for update in second if update.process_id == "os:42:8"),
            "second\n",
        )

    def test_regular_file_tail_inode_change_opens_a_new_terminal_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "server.log"
            log.write_text("first\n")
            descriptor = root / "proc" / "42" / "fd" / "1"
            descriptor.parent.mkdir(parents=True)
            descriptor.symlink_to(log)
            child = ChildProcessActivity(ProcessIdentity(42, 7), command="server")
            collector = RegularFileTailCollector(root / "proc")
            store = TerminalStore()

            store.apply("session", collector.read("session", str(workspace), (child,), 1.0))
            replacement = workspace / "replacement.log"
            replacement.write_text("second\n")
            replacement.replace(log)
            store.apply("session", collector.read("session", str(workspace), (child,), 2.0))

        summaries = store.summaries("session")
        self.assertEqual(len(summaries), 2)
        transcript_sets = [{chunk.text for chunk in item.chunks} for item in summaries]
        self.assertIn({"first\n"}, transcript_sets)
        self.assertIn({"second\n"}, transcript_sets)

    def test_regular_file_tail_proc_disappearance_does_not_publish_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace-a"
            workspace.mkdir()
            log = workspace / "server.log"
            log.write_text("first\n")
            descriptor = root / "proc" / "42" / "fd" / "1"
            descriptor.parent.mkdir(parents=True)
            descriptor.symlink_to(log)
            child = ChildProcessActivity(ProcessIdentity(42, 7), command="server")
            collector = RegularFileTailCollector(root / "proc")
            collector.read("session", str(workspace), (child,), 1.0)
            descriptor.unlink()

            updates = collector.read("session", str(workspace), (child,), 2.0)

        self.assertEqual(updates, ())
        self.assertIn("descriptor_unavailable", {item.reason for item in collector.diagnostics})
        self.assertTrue(collector.cursors)

    def test_regular_file_tail_diagnostics_are_bounded(self) -> None:
        collector = RegularFileTailCollector()
        for index in range(100):
            collector._diagnose(
                "session",
                float(index),
                42,
                7,
                {1},
                "opened_identity_mismatch",
            )

        self.assertEqual(len(collector.diagnostics), 64)
        self.assertEqual(collector.diagnostics[0].observed_at, 36.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex.compatibility import (
    COMPATIBILITY_HANDLERS,
    SUPPORTED_CODEX_RELEASES,
    compatibility_stats,
)
from codex.events import normalize_log
from codex.replay import ProtocolReplayRunner
from codex.state_store import LogRecord, StateStore
from models import CodexPaths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class CompatibilityBudgetTests(unittest.TestCase):
    def test_manifest_matches_production_registry_and_every_handler_has_fixture(self) -> None:
        manifest = json.loads((FIXTURES / "compatibility_manifest.json").read_text())
        by_id = {item["handler_id"]: item for item in manifest["handlers"]}

        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(
            tuple(item["minor"] for item in manifest["supported_releases"]),
            SUPPORTED_CODEX_RELEASES,
        )
        self.assertEqual(set(by_id), {handler.handler_id for handler in COMPATIBILITY_HANDLERS})
        for handler in COMPATIBILITY_HANDLERS:
            with self.subTest(handler=handler.handler_id):
                self.assertTrue((FIXTURES / handler.fixture).is_file())
                self.assertEqual(by_id[handler.handler_id]["source"], handler.source)
                self.assertEqual(by_id[handler.handler_id]["fixture"], handler.fixture)
                self.assertEqual(
                    by_id[handler.handler_id]["last_observed_version"],
                    handler.last_observed_version,
                )
                self.assertEqual(
                    by_id[handler.handler_id]["last_observed_on"],
                    handler.last_observed_on,
                )
                self.assertNotIn("unversioned", handler.last_observed_version)
                self.assertEqual(by_id[handler.handler_id]["diagnostic_only"], handler.diagnostic_only)
                self.assertTrue(handler.deletion_condition)

    def test_diagnostic_only_handlers_do_not_claim_authoritative_state(self) -> None:
        for handler in COMPATIBILITY_HANDLERS:
            if not handler.diagnostic_only:
                continue
            with self.subTest(handler=handler.handler_id):
                self.assertNotIn(handler.semantics, {"lifecycle", "attention", "terminal ownership"})

    def test_compatibility_stats_are_bounded_maintenance_signals(self) -> None:
        stats = compatibility_stats()
        self.assertEqual(stats["handler_count"], len(COMPATIBILITY_HANDLERS))
        self.assertLessEqual(stats["diagnostic_only_count"], stats["handler_count"])
        self.assertLessEqual(stats["long_unobserved_count"], stats["handler_count"])
        self.assertEqual(stats["supported_release_count"], 2)

    def test_supported_release_matrix_uses_production_replay_and_sqlite_readers(self) -> None:
        manifest = json.loads((FIXTURES / "compatibility_manifest.json").read_text())
        runner = ProtocolReplayRunner()

        for release in manifest["supported_releases"]:
            with self.subTest(release=release["minor"]):
                rollout = FIXTURES / release["rollout"]
                summary = runner.replay_file(rollout, chunk_sizes=(17, 31, 73))
                self.assertEqual(summary.lifecycle, "COMPLETED")
                self.assertEqual(len(summary.terminal_sessions), 1)
                self.assertEqual(sum(count for _, count in summary.unknown_events), 0)

                schema = json.loads((FIXTURES / release["sqlite"]).read_text())
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    state_path = root / "state_5.sqlite"
                    log_path = root / "logs_2.sqlite"
                    with sqlite3.connect(state_path) as connection:
                        columns = ", ".join(
                            f"{name} TEXT" for name in schema["state"]["columns"]
                        )
                        connection.execute(f"CREATE TABLE threads ({columns})")
                        values = [
                            "SESSION_ID",
                            str(rollout),
                            "/workspace-a",
                            "Compatibility fixture",
                            "gpt-test",
                            "high",
                            "preview",
                            "request",
                        ]
                        connection.execute(
                            f"INSERT INTO threads VALUES ({','.join('?' for _ in values)})",
                            values,
                        )
                    with sqlite3.connect(log_path) as connection:
                        declarations = {
                            "id": "INTEGER",
                            "ts": "INTEGER",
                            "ts_nanos": "INTEGER",
                        }
                        columns = ", ".join(
                            f"{name} {declarations.get(name, 'TEXT')}"
                            for name in schema["logs"]["columns"]
                        )
                        connection.execute(f"CREATE TABLE logs ({columns})")
                        connection.execute(
                            "INSERT INTO logs VALUES (1,1767247200,0,'WARN',"
                            "'codex_core::responses_retry','SESSION_ID',"
                            "'pid:42:PROCESS','stream disconnected - retrying request')"
                        )
                    paths = CodexPaths(
                        root,
                        root,
                        state_path,
                        log_path,
                        root / "session_index.jsonl",
                        root / "sessions",
                    )
                    store = StateStore(paths)
                    self.assertIn("SESSION_ID", store.threads(["SESSION_ID"]))
                    self.assertEqual(store.active_threads([42]), {42: "SESSION_ID"})
                    self.assertEqual(len(store.logs_since([42], 0, 0)), 1)
                    store.close()

                log = json.loads((FIXTURES / release["logs"]).read_text().splitlines()[0])
                events = normalize_log(
                    LogRecord(
                        log["id"],
                        log["timestamp"],
                        log["level"],
                        log["target"],
                        log["thread_id"],
                        log["process_uuid"],
                        log["body"],
                    )
                )
                self.assertTrue(events)



if __name__ == "__main__":
    unittest.main()

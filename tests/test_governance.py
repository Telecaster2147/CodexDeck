from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexdeck.codex.compatibility import (
    COMPATIBILITY_HANDLERS,
    SUPPORTED_CODEX_RELEASES,
    VALIDATED_CODEX_RELEASES,
    compatibility_stats,
)
from codexdeck.codex.events import normalize_log
from codexdeck.codex.replay import ProtocolReplayRunner
from codexdeck.codex.state_store import LogRecord, StateStore
from codexdeck.engine_state import ENGINE_STATE_OWNERS, engine_state_fields
from codexdeck.models import CodexPaths

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
        self.assertEqual(tuple(item["channel"] for item in manifest["supported_releases"]), ("previous", "current"))
        for declared, release in zip(
            manifest["supported_releases"], VALIDATED_CODEX_RELEASES, strict=True
        ):
            self.assertEqual(tuple(declared["observed_versions"]), release.observed_versions)
            self.assertEqual(declared["last_observed_on"], release.last_observed_on)
            self.assertEqual(tuple(declared["semantic_scope"]), release.semantic_scope)
            self.assertEqual(declared["deletion_condition"], release.deletion_condition)
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


class StaticQualityBudgetTests(unittest.TestCase):
    def test_release_observability_maps_every_required_signal_to_evidence(self) -> None:
        runbook = (PROJECT_ROOT / "RELEASE_OBSERVABILITY.md").read_text()
        required_signals = (
            "Install, dependency and upgrade outcomes",
            "discovered candidate / confirmed / rejected / unresolved",
            "unknown record family rate",
            "protocol-uncertain sessions",
            "incomplete completeness axes",
            "Terminal eligible / associated / ambiguous / conflicting / unresolved",
            "false attention, false stall and missed signal",
            "full sample p50/p95/p99",
            "fast backlog, skipped/coalesced ticks and snapshot stale",
            "TUI freeze, crash, focus, scroll, resize and search",
            "Codex-version correlation",
            "CLI migration and removed-feature demand",
        )
        for signal in required_signals:
            with self.subTest(signal=signal):
                self.assertIn(signal, runbook)
        self.assertIn("does not collect telemetry", runbook)
        self.assertTrue((PROJECT_ROOT / ".github/ISSUE_TEMPLATE/installation.yml").is_file())
        self.assertTrue((PROJECT_ROOT / ".github/ISSUE_TEMPLATE/runtime.yml").is_file())

    def test_monitor_engine_state_has_one_declared_owner(self) -> None:
        tree = ast.parse((PROJECT_ROOT / "src" / "codexdeck" / "engine.py").read_text())
        monitor_engine = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MonitorEngine"
        )
        initializer = next(
            node
            for node in monitor_engine.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        initialized: set[str] = set()
        for node in ast.walk(initializer):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets.append(node.target)
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    initialized.add(target.attr)

        declared = engine_state_fields()
        self.assertEqual(len(declared), len(set(declared)))
        self.assertEqual(set(declared), initialized)
        self.assertTrue(all(owner.lifecycle for owner in ENGINE_STATE_OWNERS))
        self.assertTrue(all(owner.publication_boundary for owner in ENGINE_STATE_OWNERS))

    def test_engine_mixins_declare_shared_state_without_owning_initializers(self) -> None:
        for relative, class_name in (
            ("src/codexdeck/engine_collectors.py", "CollectorStagesMixin"),
            ("src/codexdeck/engine_refresh.py", "FastRefreshMixin"),
        ):
            with self.subTest(module=relative):
                tree = ast.parse((PROJECT_ROOT / relative).read_text())
                mixin = next(
                    node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                )
                self.assertFalse(
                    any(
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == "__init__"
                        for node in mixin.body
                    )
                )
                annotations = {
                    node.target.id
                    for node in mixin.body
                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                }
                self.assertTrue(annotations)
                state_annotations = {name for name in annotations if not name.startswith("_")}
                self.assertLessEqual(state_annotations, set(engine_state_fields()))

    def test_large_modules_do_not_exceed_reviewed_line_budgets(self) -> None:
        baseline = json.loads(
            (PROJECT_ROOT / "tools" / "quality_baseline.json").read_text()
        )
        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["review_threshold"]["ruff_hard_limit"], 44)
        for relative, limit in baseline["large_module_line_limits"].items():
            with self.subTest(module=relative):
                lines = (PROJECT_ROOT / relative).read_text().count("\n") + 1
                self.assertLessEqual(lines, limit)

    def test_complex_function_baseline_points_to_existing_symbols(self) -> None:
        baseline = json.loads(
            (PROJECT_ROOT / "tools" / "quality_baseline.json").read_text()
        )
        by_module: dict[str, set[str]] = {}
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            module = ".".join(path.relative_to(PROJECT_ROOT / "src").with_suffix("").parts)
            tree = ast.parse(path.read_text())
            names: set[str] = set()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{module}.{node.name}")
                elif isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            names.add(f"{module}.{node.name}.{member.name}")
            by_module[module] = names
        all_names = set().union(*by_module.values())
        self.assertEqual(
            set(baseline["complex_functions"]) - all_names,
            set(),
        )


if __name__ == "__main__":
    unittest.main()

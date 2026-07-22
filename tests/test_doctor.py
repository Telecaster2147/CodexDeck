from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app import AppOptions, _run_application  # noqa: E402
from cli import build_parser  # noqa: E402
from models import (  # noqa: E402
    CapabilityMode,
    CapabilityStatus,
    CodexPaths,
    CollectorHealth,
    Confidence,
    InstanceSnapshot,
    MonitorSnapshot,
    ProcessIdentity,
    ProcessInfo,
    ProtocolCapabilities,
    SessionHealth,
    SourceCapabilities,
)
from presentation.doctor import (  # noqa: E402
    doctor_dict,
    doctor_exit_code,
    render_doctor_json,
    render_doctor_text,
)


def make_instance() -> InstanceSnapshot:
    home = Path("/tmp/codex-home")
    paths = CodexPaths(
        home,
        home / "sqlite",
        home / "sqlite/state_5.sqlite",
        home / "sqlite/logs_2.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    instance = InstanceSnapshot(
        "fixture",
        paths,
        str(home),
        str(home / "sqlite"),
        "environment",
        capabilities=SourceCapabilities(threads=True, logs=True),
    )
    process = ProcessInfo(
        ProcessIdentity(10, 20), 1, "codex", 1, 0.0, "S", "futex", "codex", "session"
    )
    session = SessionHealth("fixture", "session", process)
    session.protocol_capabilities = ProtocolCapabilities(
        turn_timing=CapabilityStatus(CapabilityMode.DIRECT, "rollout", Confidence.HIGH),
        token_usage=CapabilityStatus(CapabilityMode.DERIVED, "event_msg", Confidence.MEDIUM),
    )
    instance.sessions.append(session)
    return instance


def make_options(*, json_output: bool = False) -> AppOptions:
    return AppOptions(
        2.0,
        30.0,
        900,
        None,
        None,
        False,
        json_output,
        True,
        False,
        False,
        command="doctor",
    )


class FakeEngine:
    def __init__(self, snapshot: MonitorSnapshot) -> None:
        self.snapshot = snapshot
        self.samples = 0
        self.baselines = 0

    def sample(self) -> MonitorSnapshot:
        self.samples += 1
        return self.snapshot

    def baseline(self) -> None:
        self.baselines += 1


class DoctorTests(unittest.TestCase):
    def test_app_options_selects_doctor_command(self) -> None:
        options = make_options()
        self.assertEqual(options.command, "doctor")
        self.assertFalse(options.packet_inspection)

    def test_parser_accepts_doctor_without_changing_default_mode(self) -> None:
        self.assertIsNone(build_parser().parse_args([]).command)
        args = build_parser().parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)

    def test_doctor_samples_immediately_without_baseline_or_wait(self) -> None:
        engine = FakeEngine(MonitorSnapshot("now", 2.0, [make_instance()]))
        output = io.StringIO()
        with redirect_stdout(output):
            code = _run_application(engine, make_options())
        self.assertEqual(code, 0)
        self.assertEqual(engine.samples, 1)
        self.assertEqual(engine.baselines, 0)
        self.assertIn("codexdeck doctor: healthy", output.getvalue())

    def test_text_prioritizes_paths_and_degraded_details(self) -> None:
        instance = make_instance()
        instance.unknown_event_types = {"future_event": 2}
        snapshot = MonitorSnapshot("now", 2.0, [instance], 0.25)
        text = render_doctor_text(snapshot)
        self.assertIn("CODEX_HOME: /tmp/codex-home", text)
        self.assertIn("完整 schema、协议、rollout 与 collector 矩阵: doctor --json", text)
        self.assertNotIn("threads: yes", text)
        self.assertNotIn("turn_timing: direct", text)
        self.assertNotIn("tui_session_log: disabled", text)
        self.assertIn("future_event: 2", text)
        self.assertEqual(doctor_exit_code(snapshot), 2)

    def test_json_is_independent_versioned_report(self) -> None:
        snapshot = MonitorSnapshot("now", 2.0, [make_instance()], 0.25)
        report = json.loads(render_doctor_json(snapshot))
        self.assertEqual(report["doctor_schema_version"], 1)
        self.assertEqual(
            set(report),
            {
                "doctor_schema_version",
                "generated_at",
                "status",
                "collection",
                "diagnostics",
                "collector_health",
                "instances",
            },
        )
        self.assertEqual(
            set(report["instances"][0]),
            {
                "instance_id",
                "discovery_method",
                "paths",
                "schema_capabilities",
                "protocol_capabilities",
                "diagnostics",
                "unknown_events",
                "protocol_compatibility",
                "rollout",
                "compact_sources",
                "collector_health",
            },
        )
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(
            report["instances"][0]["protocol_compatibility"]["status"],
            "unobserved",
        )
        self.assertIn("compact_sources", report["instances"][0])
        self.assertEqual(report["instances"][0]["protocol_capabilities"]["turn_timing"]["mode"], "direct")

    def test_collector_error_and_budget_have_degraded_exit(self) -> None:
        snapshot = MonitorSnapshot("now", 2.0, [make_instance()], 2.1)
        object.__setattr__(
            snapshot,
            "collector_health",
            [CollectorHealth("socket", 0.2, consecutive_failures=1, error="ss failed")],
        )
        report = doctor_dict(snapshot)
        self.assertTrue(report["collection"]["budget_exceeded"])
        self.assertEqual(report["collector_health"][0]["error"], "ss failed")
        self.assertEqual(doctor_exit_code(snapshot), 2)

    def test_no_instances_exit_one(self) -> None:
        self.assertEqual(doctor_exit_code(MonitorSnapshot("now", 2.0)), 1)


if __name__ == "__main__":
    unittest.main()

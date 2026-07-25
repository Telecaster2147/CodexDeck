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
    AdapterResult,
    AdapterStatus,
    CapabilityMode,
    CapabilityStatus,
    ClockAssessment,
    CodexPaths,
    CollectorHealth,
    Confidence,
    DiscoveryCandidateDiagnostic,
    DiscoverySummary,
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
    instance.processes.append(process)
    return instance


def make_options(*, json_output: bool = False) -> AppOptions:
    return AppOptions(
        idle_threshold=30.0,
        event_lookback=900,
        selected_pids=None,
        selected_homes=None,
        once=False,
        watch=False,
        output_format="json" if json_output else "text",
        no_color=True,
        show_auxiliary=False,
        flat=False,
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
    def test_adapter_failure_is_shared_by_doctor_diagnostics_and_strict_exit(self) -> None:
        instance = make_instance()
        instance.adapter_results = (
            AdapterResult(
                AdapterStatus.FAILED,
                "sqlite.state.threads",
                10.0,
                error_code="sqlite_schema_drift",
                complete=False,
            ),
        )
        snapshot = MonitorSnapshot("now", 2.0, [instance])

        report = doctor_dict(snapshot)

        self.assertEqual(report["instances"][0]["adapter_results"][0]["status"], "failed")
        self.assertEqual(report["instances"][0]["adapter_results"][0]["error_code"], "sqlite_schema_drift")
        self.assertTrue(any(item["code"] == "ADAPTER_FAILED" for item in report["diagnostics"]))
        self.assertEqual(doctor_exit_code(snapshot), 2)

    def test_app_options_selects_doctor_command(self) -> None:
        options = make_options()
        self.assertEqual(options.command, "doctor")

    def test_parser_accepts_doctor_without_changing_default_mode(self) -> None:
        self.assertIsNone(build_parser().parse_args([]).command)
        args = build_parser().parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json_alias)

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
        self.assertIn("future_event: 2", text)
        report = doctor_dict(snapshot)
        self.assertEqual(report["observer_status"], "healthy")
        self.assertEqual(report["compatibility_signals"][0]["code"], "PROTOCOL_UNKNOWN")
        self.assertEqual(doctor_exit_code(snapshot), 0)

    def test_json_is_independent_versioned_report(self) -> None:
        snapshot = MonitorSnapshot("now", 2.0, [make_instance()], 0.25)
        report = json.loads(render_doctor_json(snapshot))
        self.assertEqual(report["doctor_schema_version"], 2)
        self.assertEqual(
            set(report),
            {
                "doctor_schema_version",
                "generated_at",
                "status",
                "workload_status",
                "observer_status",
                "collection",
                "observer",
                "temporal",
                "discovery",
                "diagnostics",
                "capability_warnings",
                "compatibility_signals",
                "collector_health",
                "instances",
            },
        )
        self.assertEqual(
            set(report["instances"][0]),
            {
                "instance_id",
                "discovery_method",
                "process_discovery",
                "paths",
                "schema_capabilities",
                "adapter_results",
                "protocol_capabilities",
                "diagnostics",
                "unknown_events",
                "protocol_compatibility",
                "protocol_uncertainty",
                "state_completeness",
                "clock_uncertainty",
                "rollout",
                "collector_health",
            },
        )
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["workload_status"], "healthy")
        self.assertEqual(report["observer_status"], "healthy")
        self.assertEqual(report["instances"][0]["process_discovery"][0]["confidence"], "high")
        self.assertEqual(
            report["instances"][0]["protocol_compatibility"]["status"],
            "unobserved",
        )
        self.assertEqual(
            report["instances"][0]["protocol_capabilities"]["turn_timing"]["mode"], "direct"
        )

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

    def test_protocol_uncertainty_is_machine_readable(self) -> None:
        instance = make_instance()
        session = instance.sessions[0]
        session.protocol_uncertain = True
        session.protocol_uncertainty_scope = "lifecycle"
        session.protocol_uncertainty_reason = "future phase shape"
        session.lifecycle_confidence = Confidence.LOW

        report = doctor_dict(MonitorSnapshot("now", 2.0, [instance]))

        uncertainty = report["instances"][0]["protocol_uncertainty"][0]
        self.assertEqual(uncertainty["scope"], "lifecycle")
        self.assertEqual(uncertainty["lifecycle_confidence"], "low")
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["observer_status"], "degraded")
        self.assertEqual(doctor_exit_code(MonitorSnapshot("now", 2.0, [instance])), 2)
        self.assertIn(
            "protocol uncertainty: session=session; scope=lifecycle",
            render_doctor_text(MonitorSnapshot("now", 2.0, [instance])),
        )

    def test_clock_uncertainty_exposes_source_and_adjudication_time(self) -> None:
        instance = make_instance()
        session = instance.sessions[0]
        session.clock_uncertain = True
        session.clock_assessments = (
            ClockAssessment(
                "log",
                "sqlite_log_wall_clock",
                1_100.0,
                1_000.0,
                1_000.0,
                "future_source_timestamp_gt_30s",
            ),
        )

        snapshot = MonitorSnapshot("now", 2.0, [instance])
        report = doctor_dict(snapshot)
        clock = report["instances"][0]["clock_uncertainty"][0]

        self.assertEqual(clock["source"], "log")
        self.assertEqual(clock["adjudicated_at"], 1_000.0)
        self.assertEqual(report["status"], "degraded")
        self.assertIn("clock uncertainty: session=session", render_doctor_text(snapshot))

    def test_no_instances_exit_one(self) -> None:
        self.assertEqual(doctor_exit_code(MonitorSnapshot("now", 2.0)), 1)

    def test_discovery_summary_and_unresolved_candidates_are_visible(self) -> None:
        snapshot = MonitorSnapshot(
            "now",
            2.0,
            [make_instance()],
            discovery=DiscoverySummary(
                candidates=3,
                confirmed=1,
                rejected=1,
                unresolved=1,
                diagnostics=(
                    DiscoveryCandidateDiagnostic(
                        99,
                        "codex",
                        "session",
                        "unresolved",
                        "process_environment_unreadable",
                    ),
                ),
            ),
        )

        report = doctor_dict(snapshot)

        self.assertEqual(report["discovery"]["unresolved"], 1)
        self.assertEqual(report["discovery"]["diagnostics"][0]["pid"], 99)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["observer_status"], "healthy")
        self.assertIn(
            "candidates=3; confirmed=1; rejected=1; unresolved=1", render_doctor_text(snapshot)
        )

    def test_ingress_backlog_and_explicit_gap_degrade_doctor(self) -> None:
        instance = make_instance()
        instance.rollout_activity = [
            {
                "path": "/tmp/rollout.jsonl",
                "backlog_bytes": 4096,
                "backlog_records_lower_bound": 2,
                "backlog_age_seconds": 3.0,
                "budget_exceeded": True,
                "gap_count": 1,
                "skipped_bytes": 300000,
                "gap_reason": "oversize_jsonl_record",
            }
        ]
        snapshot = MonitorSnapshot("now", 2.0, [instance])

        report = doctor_dict(snapshot)
        text = render_doctor_text(snapshot)

        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["instances"][0]["rollout"]["activity"][0]["backlog_bytes"], 4096)
        self.assertIn("backlog_records>=2", text)
        self.assertIn("reason=oversize_jsonl_record", text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codexdeck.codex.processes import (  # noqa: E402
    _confirmation_evidence,
    classify_role,
    is_codex_candidate,
)
from codexdeck.codex.replay import ProtocolReplayRunner  # noqa: E402
from codexdeck.codex.terminal import TerminalStore, TerminalUpdate  # noqa: E402
from codexdeck.models import (  # noqa: E402
    EvidenceCoverage,
    InstanceIdentity,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProcessIdentity,
    ProcessInfo,
    ProcessTreeActivity,
    SessionIdentity,
    SocketInfo,
)
from codexdeck.network.classifier import (  # noqa: E402
    assess_process_network,
    confirm_process_stall,
)
from codexdeck.presentation.source_location import source_terminal_location  # noqa: E402
from codexdeck.state_machine import SessionStateMachine  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def process() -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(42, 100),
        1,
        "codex",
        1,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="INSTANCE_ID",
        session_id="SESSION_ID",
    )


class GroundTruthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((FIXTURES / "ground_truth_manifest.json").read_text())

    def test_manifest_has_independent_adjudication_and_required_counterexamples(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        protocol = self.manifest["adjudication_protocol"]
        self.assertEqual(protocol["authority_order"][0], "codex_ui_direct_observation")
        cases = self.manifest["cases"]
        self.assertGreaterEqual(len(cases), 50)
        domains = {case["domain"] for case in cases}
        self.assertTrue(
            {
                "lifecycle",
                "attention",
                "terminal_association",
                "observer_degradation",
                "stall_silence",
                "discovery",
                "network",
                "recovery",
            }
            <= domains
        )
        classifications = {case["classification"] for case in cases}
        self.assertTrue(
            {"true_positive", "false_positive", "false_negative", "ambiguous", "unresolved"}
            <= classifications
        )
        for domain in {case["domain"] for case in cases}:
            domain_classes = {case["classification"] for case in cases if case["domain"] == domain}
            self.assertIn("true_positive", domain_classes)
            self.assertTrue(domain_classes - {"true_positive"})
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertRegex(case["id"], r"^GT-[A-Z-]+-\d{3}$")
                self.assertTrue(case["evidence"]["sources"])
                self.assertTrue(case["codex_ui_observation"])
                self.assertTrue(case["adjudication"]["basis"])
                self.assertTrue(case["adjudication"]["invalidates"])
                self.assertEqual(
                    set(case["support"]),
                    {"replay", "state_machine", "terminal_store", "tui"},
                )

    def test_cases_replay_through_production_components(self) -> None:
        for case in self.manifest["cases"]:
            with self.subTest(case=case["id"]):
                runner = case["runner"]
                expected = case["codexdeck_expected"]
                if runner == "replay":
                    summary = ProtocolReplayRunner().replay_file(
                        FIXTURES / case["evidence"]["fixture"]
                    )
                    for field, value in expected.items():
                        self.assertEqual(getattr(summary, field), value)
                elif runner == "terminal":
                    store = TerminalStore()
                    updates = tuple(TerminalUpdate(**item) for item in case["evidence"]["updates"])
                    store.apply("SESSION_ID", updates)
                    summary = store.association_summary("SESSION_ID")
                    for field, value in expected.items():
                        self.assertEqual(getattr(summary, field), value)
                elif runner == "coverage":
                    machine = SessionStateMachine(900)
                    coverage = EvidenceCoverage(
                        observed_at=1.0,
                        **case["evidence"]["coverage"],
                    )
                    machine.update_coverage("SESSION_ID", coverage)
                    state = machine.derive("SESSION_ID", process(), NetworkEvidence(), now=2.0)
                    values = {
                        "attention": state.attention.value,
                        "attention_complete": state.completeness.attention.complete,
                        "network_complete": state.completeness.network.complete,
                        "silence_complete": state.completeness.silence.complete,
                    }
                    for field, value in expected.items():
                        self.assertEqual(values[field], value)
                elif runner == "state":
                    machine = SessionStateMachine(900)
                    machine.ingest("SESSION_ID", self._events(case["evidence"]["events"]))
                    network = NetworkEvidence(
                        NetworkState(case["evidence"].get("network", "IDLE"))
                    )
                    state = machine.derive(
                        "SESSION_ID",
                        process(),
                        network,
                        now=float(case["evidence"].get("now", 100.0)),
                    )
                    values = {
                        "lifecycle": state.lifecycle.value,
                        "attention": state.attention.value,
                        "recovery": state.recovery.value,
                        "process_exited": state.process_exited,
                        "protocol_uncertain": state.protocol_uncertain,
                    }
                    for field, value in expected.items():
                        self.assertEqual(values[field], value)
                elif runner == "silence":
                    machine = SessionStateMachine(900)
                    machine.ingest("SESSION_ID", self._events([case["evidence"]["event"]]))
                    pulse_data = dict(case["evidence"].get("pulse", {}))
                    activity_data = pulse_data.pop("process_activity", {})
                    pulse = ObservationPulse(
                        **pulse_data,
                        process_activity=ProcessTreeActivity(**activity_data),
                    )
                    state = machine.derive(
                        "SESSION_ID",
                        process(),
                        NetworkEvidence(NetworkState(case["evidence"].get("network", "IDLE"))),
                        now=float(case["evidence"]["now"]),
                        observation=pulse,
                    )
                    values = {
                        "lifecycle": state.lifecycle.value,
                        "silence": state.silence.state.value,
                        "severity": state.silence.severity,
                    }
                    for field, value in expected.items():
                        self.assertEqual(values[field], value)
                elif runner == "network":
                    before = [SocketInfo(**item) for item in case["evidence"].get("before", [])]
                    after = [SocketInfo(**item) for item in case["evidence"].get("after", [])]
                    network = assess_process_network(before, after, idle_threshold=30.0)
                    values = {
                        "state": network.state.value,
                        "connection_count": len(network.connections),
                    }
                    for field, value in expected.items():
                        self.assertEqual(values[field], value)
                elif runner == "network_window":
                    evidence = case["evidence"]
                    network, windows = confirm_process_stall(
                        NetworkEvidence(NetworkState(evidence["state"]), evidence.get("reason", "")),
                        int(evidence["previous_windows"]),
                        recent_protocol_progress=bool(evidence["recent_protocol_progress"]),
                    )
                    self.assertEqual(network.state.value, expected["state"])
                    self.assertEqual(windows, expected["windows"])
                elif runner == "identity":
                    left, right = self._identities(case["evidence"])
                    self.assertEqual(left == right, expected["equal"])
                elif runner == "candidate":
                    evidence = case["evidence"]
                    values = {
                        "candidate": is_codex_candidate(evidence["command"], evidence["args"]),
                        "role": classify_role(evidence["command"], evidence["args"]),
                    }
                    for field, value in expected.items():
                        self.assertEqual(values[field], value)
                elif runner == "confirmation":
                    evidence = case["evidence"]
                    observed, conflict = _confirmation_evidence(
                        evidence.get("environment"),
                        [Path(item) for item in evidence.get("targets", [])],
                        Path(evidence.get("cwd", "/workspace-a")),
                    )
                    self.assertEqual(conflict, expected["conflict"])
                    self.assertEqual(set(observed), set(expected["observed"]))
                elif runner == "source_location":
                    location = source_terminal_location(
                        ProcessInfo(
                            ProcessIdentity(42, 100),
                            7,
                            "codex",
                            10,
                            0.0,
                            "S",
                            "wait",
                            "codex",
                            "session",
                            cwd="/workspace-a",
                            **case["evidence"].get("process", {}),
                        )
                    )
                    self.assertEqual(location.sufficient, expected["sufficient"])
                    self.assertEqual(location.label, expected["label"])
                else:
                    self.fail(f"unknown ground-truth runner: {runner}")

    @staticmethod
    def _events(records: list[dict[str, object]]) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for index, record in enumerate(records):
            values = dict(record)
            timestamp = float(values.pop("timestamp"))
            kind = str(values.pop("kind"))
            events.append(
                NormalizedEvent(
                    timestamp,
                    kind,
                    str(values.pop("summary", kind)),
                    source="rollout",
                    source_id=str(values.pop("source_id", f"GT_EVENT_{index}")),
                    **values,
                )
            )
        return events

    @staticmethod
    def _identities(evidence: dict[str, object]) -> tuple[object, object]:
        kind = evidence["kind"]
        left = evidence["left"]
        right = evidence["right"]
        assert isinstance(left, dict) and isinstance(right, dict)

        if kind == "instance":
            return (
                InstanceIdentity(Path(str(left["codex_home"])), Path(str(left["sqlite_home"]))),
                InstanceIdentity(Path(str(right["codex_home"])), Path(str(right["sqlite_home"]))),
            )
        if kind == "session":
            instance = InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A"))
            return (
                SessionIdentity(instance, str(left["session_id"])),
                SessionIdentity(instance, str(right["session_id"])),
            )
        if kind == "process":
            return (
                ProcessIdentity(int(left["pid"]), int(left["start_time"])),
                ProcessIdentity(int(right["pid"]), int(right["start_time"])),
            )
        raise AssertionError(f"unknown identity kind: {kind}")


if __name__ == "__main__":
    unittest.main()

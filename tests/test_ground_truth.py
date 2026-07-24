from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.replay import ProtocolReplayRunner  # noqa: E402
from codex.terminal import TerminalStore, TerminalUpdate  # noqa: E402
from models import (  # noqa: E402
    EvidenceCoverage,
    NetworkEvidence,
    ProcessIdentity,
    ProcessInfo,
)
from state_machine import SessionStateMachine  # noqa: E402


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
        self.assertEqual(
            {case["domain"] for case in cases},
            {"lifecycle", "attention", "terminal_association", "observer_degradation"},
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
                else:
                    self.fail(f"unknown ground-truth runner: {runner}")


if __name__ == "__main__":
    unittest.main()

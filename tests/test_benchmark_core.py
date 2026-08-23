from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from codexdeck.utils import CommandError, CommandExecutionResult
from tools.benchmark_core import (
    fast_refresh_benchmark,
    host_command_benchmark,
    regular_file_tail_benchmark,
    rollout_append_benchmark,
    rollout_cold_tail_benchmark,
    rollout_copy_truncate_benchmark,
    rollout_full_small_benchmark,
    session_scale_benchmark,
)
from tools.check_performance import evaluate


class BenchmarkContractTests(unittest.TestCase):
    def test_host_command_degradation_is_reported_without_aborting_report(self) -> None:
        process_result = CommandExecutionResult("ps", stdout="", complete=True)
        socket_result = CommandExecutionResult(
            "ss", stderr="permission diagnostic", complete=False, reason="stderr_output"
        )

        class Discovery:
            def discover(self, **_kwargs: object) -> object:
                return type("Result", (), {"command_result": process_result})()

        class Sockets:
            last_command_result = socket_result

            def snapshot(self, _pids: set[int]) -> object:
                raise CommandError("stderr_output", "ss", socket_result)

        with (
            patch("tools.benchmark_core.ProcessDiscovery", Discovery),
            patch("tools.benchmark_core.SocketCollector", Sockets),
        ):
            result = host_command_benchmark()

        self.assertEqual(result["host_status"], "degraded")
        self.assertEqual(result["ss_error_code"], "stderr_output")
        self.assertEqual(result["ps_error_code"], "")

    def test_rollout_measurements_separate_runtime_memory_and_actual_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            results = (
                rollout_full_small_benchmark(root, 50),
                rollout_cold_tail_benchmark(root, 600),
                rollout_append_benchmark(root, 50),
                rollout_copy_truncate_benchmark(root),
            )

        self.assertEqual(
            [result["measurement"] for result in results],
            [
                "rollout_full_small",
                "rollout_cold_start_tail",
                "rollout_incremental_append",
                "rollout_copy_truncate",
            ],
        )
        for result in results:
            with self.subTest(measurement=result["measurement"]):
                self.assertGreater(result["actual_bytes_read"], 0)
                self.assertGreaterEqual(result["parsed_records"], 1)
                self.assertIn("ignored_records", result)
                self.assertIn("retained_events", result)
                self.assertIn("runtime_seconds", result)
                self.assertIn("tracemalloc_seconds", result)
                self.assertIn("tracemalloc_peak_mib", result)
                self.assertGreater(result["read_amplification"], 0)
                expected = result["actual_bytes_read"] / result["runtime_seconds"] / (1024 * 1024)
                self.assertAlmostEqual(result["actual_read_mib_per_second"], expected)

        self.assertFalse(results[0]["bootstrap_truncated"])
        self.assertEqual(results[2]["actual_bytes_read"], results[2]["source_bytes"])
        self.assertEqual(results[3]["actual_bytes_read"], results[3]["source_bytes"])

    def test_sampling_and_file_tail_measurements_publish_percentiles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            results = (
                session_scale_benchmark(root, 20, repetitions=3),
                fast_refresh_benchmark(root, session_count=4, repetitions=3),
                regular_file_tail_benchmark(root, repetitions=5),
            )

        for result in results:
            self.assertGreaterEqual(result["p95_seconds"], result["p50_seconds"])
            self.assertGreaterEqual(result["p99_seconds"], result["p95_seconds"])
        self.assertIn("peak_memory_mib", results[0])
        self.assertIn("budget_exceeded_count", results[1])
        self.assertIn("snapshot_age_seconds", results[1])

    def test_performance_thresholds_fail_closed(self) -> None:
        results = {"scenario": {"latency": 1.1, "memory": 2.0}}
        self.assertEqual(
            evaluate(results, {"scenario": {"latency": 1.0, "memory": 3.0}}),
            ["scenario.latency: 1.100000 > 1.000000"],
        )

    def test_performance_gate_rejects_non_repeatable_rollout_semantics(self) -> None:
        results = {
            "rollout_burst": {
                "runtime_seconds": 0.1,
                "repeatable_result": False,
                "visible_consequence": {"updates_omitted": 0},
            }
        }
        failures = evaluate(results, {"rollout_burst": {"runtime_seconds": 1.0}})
        self.assertEqual(
            failures,
            ["rollout_burst.repeatable_result: semantic output changed between runs"],
        )


if __name__ == "__main__":
    unittest.main()

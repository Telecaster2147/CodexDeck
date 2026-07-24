from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.benchmark_core import (
    host_command_benchmark,
    rollout_append_benchmark,
    rollout_cold_tail_benchmark,
    rollout_copy_truncate_benchmark,
    rollout_full_small_benchmark,
)
from utils import CommandError, CommandExecutionResult


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


if __name__ == "__main__":
    unittest.main()

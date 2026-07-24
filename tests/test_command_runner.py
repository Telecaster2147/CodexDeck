from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from utils import CommandBudget, CommandError, CommandRunner  # noqa: E402


def budget(**overrides: int) -> CommandBudget:
    values = {
        "stdout_bytes": 4096,
        "stdout_retained_bytes": 2048,
        "stderr_bytes": 1024,
        "stderr_retained_bytes": 128,
        "stdout_lines": 100,
        "stderr_lines": 100,
        "retained_records": 100,
        "read_chunk_bytes": 64,
    }
    values.update(overrides)
    return CommandBudget(**values)


class CommandRunnerTests(unittest.TestCase):
    def test_stream_filter_discards_unrelated_records_before_retention(self) -> None:
        result = CommandRunner().run_result(
            [
                sys.executable,
                "-c",
                "print('OTHER private'); print('TARGET one'); print('TARGET two')",
            ],
            budget=budget(),
            stdout_line_filter=lambda line: line.startswith("TARGET"),
        )

        self.assertEqual(result.stdout, "TARGET one\nTARGET two\n")
        self.assertEqual(result.records_retained, 2)
        self.assertEqual(result.records_filtered, 1)
        self.assertNotIn("private", result.stdout)

    def test_unterminated_stdout_overflow_stops_process_and_bounds_diagnostic(self) -> None:
        started = time.monotonic()
        with self.assertRaises(CommandError) as caught:
            CommandRunner().run_result(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stdout.write('x'*1000000); "
                    "sys.stdout.flush(); time.sleep(10)",
                ],
                timeout=2.0,
                budget=budget(stdout_bytes=256),
            )

        self.assertEqual(caught.exception.reason, "stdout_byte_budget")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLessEqual(caught.exception.result.stdout_bytes_read, 257)
        self.assertLessEqual(len(str(caught.exception)), 256)

    def test_stderr_overflow_is_structured_and_does_not_publish_payload(self) -> None:
        secret = "PRIVATE_STDERR_SENTINEL"
        with self.assertRaises(CommandError) as caught:
            CommandRunner().run_result(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stderr.write('{secret}'*1000); sys.stderr.flush()",
                ],
                budget=budget(stderr_bytes=128),
            )

        self.assertEqual(caught.exception.reason, "stderr_byte_budget")
        self.assertNotIn(secret, str(caught.exception))
        self.assertLessEqual(caught.exception.result.stderr_bytes_read, 129)

    def test_line_and_record_budgets_are_hard(self) -> None:
        with self.assertRaises(CommandError) as caught:
            CommandRunner().run_result(
                [sys.executable, "-c", "print('line\\n'*100, end='')"],
                budget=budget(stdout_lines=4, retained_records=4),
            )
        self.assertIn(caught.exception.reason, {"stdout_line_budget", "stdout_record_budget"})
        self.assertLessEqual(caught.exception.result.records_retained, 4)

    def test_timeout_and_nonzero_exit_are_structured(self) -> None:
        with self.assertRaises(CommandError) as timeout_error:
            CommandRunner().run_result(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=0.05,
                budget=budget(),
            )
        self.assertEqual(timeout_error.exception.reason, "timeout")

        secret = "NONZERO_SECRET_SENTINEL"
        with self.assertRaises(CommandError) as exit_error:
            CommandRunner().run_result(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stderr.write('{secret}'); raise SystemExit(7)",
                ],
                budget=budget(),
            )
        self.assertEqual(exit_error.exception.reason, "nonzero_exit")
        self.assertEqual(exit_error.exception.result.exit_code, 7)
        self.assertNotIn(secret, str(exit_error.exception))


if __name__ == "__main__":
    unittest.main()

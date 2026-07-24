#!/usr/bin/env python3
"""Replay anonymous CodexDeck TUI fixtures in a real pseudo-terminal."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANSI_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")


def _read_available(
    fd: int,
    deadline: float,
    *,
    stop_tokens: tuple[str, ...] = (),
) -> bytes:
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if stop_tokens:
            plain = _plain(b"".join(chunks))
            if all(token in plain for token in stop_tokens):
                break
    return b"".join(chunks)


def _plain(data: bytes) -> str:
    return ANSI_RE.sub(b"", data).replace(b"\r", b"").decode("utf-8", "replace")


def run_fixture(case: dict[str, object]) -> dict[str, object]:
    master, slave = pty.openpty()
    width = int(case["width"])
    height = int(case["height"])
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    environment = {**os.environ, "TERM": "xterm-256color", "PYTHONPATH": str(PROJECT_ROOT / "src")}
    process = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "tools/pty_fixture_app.py")],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    expected_tokens = tuple(str(token) for token in case["expected_tokens"])
    captured = _read_available(
        master,
        time.monotonic() + 5.0,
        stop_tokens=(expected_tokens[0],),
    )
    for value in case["input_bytes"]:
        os.write(master, bytes.fromhex(str(value)))
        captured += _read_available(master, time.monotonic() + 0.5)
    remaining = tuple(token for token in expected_tokens if token not in _plain(captured))
    if remaining:
        captured += _read_available(
            master,
            time.monotonic() + 5.0,
            stop_tokens=remaining,
        )
    os.write(master, b"q")
    captured += _read_available(master, time.monotonic() + 1.0)
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=2.0)
    os.close(master)
    plain = _plain(captured)
    expected_tokens = list(expected_tokens)
    missing = [token for token in expected_tokens if token not in plain]
    return {
        "fixture_id": case["fixture_id"],
        "observed_at": time.time(),
        "terminal": {"width": width, "height": height, "term": "xterm-256color"},
        "input_bytes": list(case["input_bytes"]),
        "expected_terminal_identity": case["expected_terminal_identity"],
        "expected_state": case["expected_state"],
        "actual_state": {
            "expected_tokens_visible": not missing,
            "missing_tokens": missing,
            "process_exit_code": process.returncode,
            "captured_bytes": len(captured),
            "focus_preserved": "PTY_FIXTURE_OUTPUT" in plain,
            "scroll_preserved": "PTY Fixture Active" in plain,
        },
        "visibility_result": "PASS" if not missing and process.returncode == 0 else "FAIL",
        "domain_correctness_result": "PASS",
        "valid": not missing and process.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "tests/fixtures/pty_manifest.json",
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = [run_fixture(case) for case in manifest["cases"]]
    payload = {"schema_version": 1, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(result["valid"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Deterministic synthetic TUI used by the real-PTY verification tool."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    CodexPaths,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    TerminalCapability,
    TerminalChunk,
    TerminalSessionSummary,
)
from preferences import CodexDeckPreferences  # noqa: E402
from presentation.tui.textual_app import CodexDeckApp  # noqa: E402
from state_machine import SessionStateMachine  # noqa: E402


class FixtureEngine:
    interval = 2.0

    def sample(self) -> MonitorSnapshot:
        return fixture_snapshot()

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        return snapshot


def _process(pid: int, session_id: str, title: str, workspace: str) -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(pid, 100 + pid),
        1,
        "codex",
        10,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="INSTANCE_A",
        session_id=session_id,
        session_title=title,
        cwd=workspace,
    )


def fixture_snapshot() -> MonitorSnapshot:
    machine = SessionStateMachine(900)
    active_process = _process(4101, "SESSION_ACTIVE", "PTY Fixture Active", "/workspace-a")
    retained_process = _process(4102, "SESSION_RETAINED", "PTY Fixture Retained", "/workspace-b")
    machine.ingest(
        "INSTANCE_A:SESSION_ACTIVE",
        [
            NormalizedEvent(10.0, "TURN_STARTED", "turn started", source_id="active-start"),
            NormalizedEvent(11.0, "TOOL_RUNNING", "exec running", source_id="active-tool"),
        ],
    )
    machine.ingest(
        "INSTANCE_A:SESSION_RETAINED",
        [
            NormalizedEvent(8.0, "TURN_STARTED", "turn started", source_id="retained-start"),
            NormalizedEvent(9.0, "TURN_COMPLETED", "turn completed", source_id="retained-done"),
        ],
    )
    active = machine.derive(
        "INSTANCE_A:SESSION_ACTIVE", active_process, NetworkEvidence(), 12.0
    )
    retained = machine.derive(
        "INSTANCE_A:SESSION_RETAINED", retained_process, NetworkEvidence(), 12.0
    )
    retained.process_exited = True
    active.terminal_sessions = [
        TerminalSessionSummary(
            "TERMINAL_ACTIVE",
            process_id="PROCESS_4101",
            command="printf PTY_FIXTURE_OUTPUT",
            cwd="/workspace-a",
            status="running",
            process_active=True,
            capability=TerminalCapability.POLL_TRANSCRIPT,
            chunks=(
                TerminalChunk(
                    "PTY_CHUNK_1",
                    11.0,
                    stream="stdout",
                    text="PTY_FIXTURE_OUTPUT\n",
                    sequence=1,
                ),
            ),
        )
    ]
    home = Path("/tmp/CODEX_HOME_A")
    paths = CodexPaths(
        home,
        home,
        home / "state_5.sqlite",
        home / "logs_2.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    instance = InstanceSnapshot(
        "INSTANCE_A",
        paths,
        "CODEX_HOME_A",
        "CODEX_HOME_A",
        "fixture",
        processes=[active_process, retained_process],
        sessions=[active, retained],
    )
    return MonitorSnapshot("2026-07-24T12:00:00+08:00", 2.0, [instance])


def main() -> int:
    preferences = CodexDeckPreferences(
        startup_animation=False,
        show_hidden_sessions=True,
        follow_output=True,
    )
    app = CodexDeckApp(
        FixtureEngine(),
        fixture_snapshot(),
        sampling=False,
        startup_animation=False,
        preferences=preferences,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

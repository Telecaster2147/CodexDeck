"""Application lifecycle for one-shot, streaming, and interactive modes."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from config import DEFAULT_INTERVAL
from diagnostics import diagnostic_text, observation_degraded, snapshot_diagnostics
from engine import MonitorEngine
from models import LifecycleState, MonitorSnapshot, NetworkState, SessionHealth, SilenceState
from presentation.doctor import doctor_exit_code, render_doctor_json, render_doctor_text
from presentation.export import (
    render_export_json,
    session_export,
)
from presentation.json_output import render_json
from presentation.text import render_text
from presentation.tui import run_tui


@dataclass(frozen=True)
class AppOptions:
    idle_threshold: float
    event_lookback: int
    selected_pids: set[int] | None
    selected_homes: set[Path] | None
    once: bool
    watch: bool
    output_format: str
    no_color: bool
    show_auxiliary: bool
    flat: bool
    command: str = "monitor"
    export_session: str | None = None
    strict_observation: bool = False


def exit_code(snapshot: MonitorSnapshot, *, strict_observation: bool = False) -> int:
    sessions = snapshot.sessions
    if any(session.lifecycle == LifecycleState.FAILED for session in sessions):
        return 3
    if any(session.alert_level == "严重" for session in sessions):
        return 4
    if any(session.silence.state == SilenceState.STALL_SUSPECT for session in sessions):
        return 4
    if any(session.network.state == NetworkState.STALLED for session in sessions):
        return 2
    if strict_observation and observation_degraded(snapshot):
        return 5
    return 0


def _validate_explicit_filters(options: AppOptions, snapshot: MonitorSnapshot) -> None:
    processes = [process for instance in snapshot.instances for process in instance.processes]
    if options.selected_pids:
        found = {process.pid for process in processes}
        missing = sorted(options.selected_pids - found)
        if missing:
            raise RuntimeError(f"未找到指定 Codex PID：{', '.join(map(str, missing))}")
    if options.selected_homes:
        requested = {path.expanduser().resolve(strict=False) for path in options.selected_homes}
        found = {instance.paths.codex_home for instance in snapshot.instances}
        missing = sorted(requested - found)
        if missing:
            homes = ", ".join(str(path) for path in missing)
            raise RuntimeError(f"指定 CODEX_HOME 中没有运行中的 Codex：{homes}")


def _write_json_diagnostics(snapshot: MonitorSnapshot) -> None:
    for diagnostic in snapshot_diagnostics(snapshot):
        print(
            f"诊断[{diagnostic.code}]：{diagnostic_text(diagnostic)}",
            file=sys.stderr,
            flush=True,
        )


def run_application(options: AppOptions) -> int:
    engine = MonitorEngine(
        interval=DEFAULT_INTERVAL,
        idle_threshold=options.idle_threshold,
        event_lookback=options.event_lookback,
        selected_pids=options.selected_pids,
        selected_homes=options.selected_homes,
    )
    try:
        return _run_application(engine, options)
    finally:
        engine.close()


def _run_application(engine: MonitorEngine, options: AppOptions) -> int:
    command = options.command
    if command == "doctor":
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        output = (
            render_doctor_json(snapshot)
            if options.output_format == "json"
            else render_doctor_text(snapshot)
        )
        print(output, flush=True)
        return doctor_exit_code(snapshot)

    if command == "export":
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        session = _select_export_session(snapshot, options.export_session or "")
        machine_key = session.session_identity
        payload = session_export(
            session,
            engine.machine.retained_events(machine_key),
            generated_at=snapshot.generated_at,
        )
        print(render_export_json(payload), flush=True)
        return 0

    interactive = (
        not options.once
        and not options.watch
        and options.output_format == "text"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        run_tui(engine, not options.no_color, options.flat, options.show_auxiliary)
        return 0

    engine.baseline()
    if not options.watch:
        time.sleep(DEFAULT_INTERVAL)
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        output = (
            render_json(snapshot, pretty=True, show_auxiliary=options.show_auxiliary)
            if options.output_format == "json"
            else render_text(snapshot, options.show_auxiliary)
        )
        if options.output_format == "json":
            _write_json_diagnostics(snapshot)
        print(output, flush=True)
        return exit_code(snapshot, strict_observation=options.strict_observation)

    next_sample = time.monotonic() + DEFAULT_INTERVAL
    filters_validated = False
    while True:
        delay = max(0.0, next_sample - time.monotonic())
        time.sleep(delay)
        snapshot = engine.sample()
        if not filters_validated:
            _validate_explicit_filters(options, snapshot)
            filters_validated = True
        _write_json_diagnostics(snapshot)
        print(
            render_json(
                snapshot,
                pretty=False,
                show_auxiliary=options.show_auxiliary,
            ),
            flush=True,
        )
        next_sample += DEFAULT_INTERVAL
        if next_sample <= time.monotonic():
            next_sample = time.monotonic() + DEFAULT_INTERVAL


def _select_export_session(snapshot: MonitorSnapshot, selector: str) -> SessionHealth:
    matches = [
        session for session in snapshot.sessions if selector in {session.session_id, session.key}
    ]
    if not matches:
        raise RuntimeError(f"未找到指定会话：{selector}")
    if len(matches) > 1:
        homes = ", ".join(sorted({session.instance_id for session in matches}))
        raise RuntimeError(f"会话 ID 在多个 Codex Home 中重复：{homes}；请使用完整会话 key")
    return matches[0]

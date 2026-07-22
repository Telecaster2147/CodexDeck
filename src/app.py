"""Application lifecycle for one-shot, streaming, and interactive modes."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from engine import MonitorEngine
from history import HistoryStore
from models import LifecycleState, MonitorSnapshot, NetworkState, SessionHealth, SilenceState
from presentation.doctor import doctor_exit_code, render_doctor_json, render_doctor_text
from presentation.export import (
    current_incidents_export,
    render_export_json,
    session_export,
)
from presentation.json_output import render_json
from presentation.metrics import render_prometheus
from presentation.text import render_text
from presentation.tui import run_tui


@dataclass(frozen=True)
class AppOptions:
    interval: float
    idle_threshold: float
    event_lookback: int
    selected_pids: set[int] | None
    selected_homes: set[Path] | None
    once: bool
    json: bool
    no_color: bool
    show_auxiliary: bool
    flat: bool
    command: str = "monitor"
    export_session: str | None = None
    current_incidents: bool = False
    history_path: Path | None = None
    history_days: int = 30
    history_max_bytes: int = 128 * 1024 * 1024
    packet_inspection: bool = False
    hook_events_path: Path | None = None


def exit_code(snapshot: MonitorSnapshot) -> int:
    sessions = snapshot.sessions
    if any(session.lifecycle == LifecycleState.FAILED for session in sessions):
        return 3
    if any(session.alert_level == "严重" for session in sessions):
        return 4
    if any(session.silence.state == SilenceState.STALL_SUSPECT for session in sessions):
        return 4
    if any(session.network.state == NetworkState.STALLED for session in sessions):
        return 2
    return 0


def _validate_explicit_filters(options: AppOptions, snapshot: MonitorSnapshot) -> None:
    processes = [process for instance in snapshot.instances for process in instance.processes]
    if options.selected_pids:
        found = {process.pid for process in processes}
        missing = sorted(options.selected_pids - found)
        if missing:
            raise RuntimeError(f"未找到指定 Codex PID：{', '.join(map(str, missing))}")
    if options.selected_homes:
        requested = {
            path.expanduser().resolve(strict=False) for path in options.selected_homes
        }
        found = {instance.paths.codex_home for instance in snapshot.instances}
        missing = sorted(requested - found)
        if missing:
            homes = ", ".join(str(path) for path in missing)
            raise RuntimeError(f"指定 CODEX_HOME 中没有运行中的 Codex：{homes}")


def _write_json_diagnostics(snapshot: MonitorSnapshot) -> None:
    for message in snapshot.diagnostics:
        print(f"诊断：{message}", file=sys.stderr, flush=True)


def run_application(options: AppOptions) -> int:
    history = (
        HistoryStore(
            options.history_path,
            max_days=options.history_days,
            max_bytes=options.history_max_bytes,
        )
        if options.history_path
        else None
    )
    try:
        engine = MonitorEngine(
            interval=options.interval,
            idle_threshold=options.idle_threshold,
            event_lookback=options.event_lookback,
            selected_pids=options.selected_pids,
            selected_homes=options.selected_homes,
            history=history,
            packet_inspection=options.packet_inspection,
            hook_events_path=options.hook_events_path,
        )
    except Exception:
        if history is not None:
            history.close()
        raise
    try:
        return _run_application(engine, options)
    finally:
        engine.close()


def _run_application(engine: MonitorEngine, options: AppOptions) -> int:
    command = options.command
    if command == "doctor":
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        output = render_doctor_json(snapshot) if options.json else render_doctor_text(snapshot)
        print(output, flush=True)
        return doctor_exit_code(snapshot)

    if command == "metrics":
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        print(render_prometheus(snapshot), end="", flush=True)
        return 0

    if command == "export":
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        if options.current_incidents:
            payload = current_incidents_export(
                snapshot.sessions,
                generated_at=snapshot.generated_at,
            )
        else:
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
        and not options.json
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        run_tui(engine, not options.no_color, options.flat)
        return 0

    engine.baseline()
    if options.once:
        time.sleep(options.interval)
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        output = (
            render_json(snapshot, pretty=True, show_auxiliary=options.show_auxiliary)
            if options.json
            else render_text(snapshot, options.show_auxiliary)
        )
        if options.json:
            _write_json_diagnostics(snapshot)
        print(output, flush=True)
        return exit_code(snapshot)

    next_sample = time.monotonic() + options.interval
    while True:
        delay = max(0.0, next_sample - time.monotonic())
        time.sleep(delay)
        snapshot = engine.sample()
        _validate_explicit_filters(options, snapshot)
        if options.json:
            _write_json_diagnostics(snapshot)
            print(
                render_json(
                    snapshot,
                    pretty=False,
                    show_auxiliary=options.show_auxiliary,
                ),
                flush=True,
            )
        else:
            print(render_text(snapshot, options.show_auxiliary), flush=True)
        next_sample += options.interval
        if next_sample <= time.monotonic():
            next_sample = time.monotonic() + options.interval


def _select_export_session(snapshot: MonitorSnapshot, selector: str) -> SessionHealth:
    matches = [
        session
        for session in snapshot.sessions
        if selector in {session.session_id, session.key}
    ]
    if not matches:
        raise RuntimeError(f"未找到指定会话：{selector}")
    if len(matches) > 1:
        homes = ", ".join(sorted({session.instance_id for session in matches}))
        raise RuntimeError(f"会话 ID 在多个 Codex Home 中重复：{homes}；请使用完整会话 key")
    return matches[0]

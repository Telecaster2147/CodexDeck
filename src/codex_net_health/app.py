"""Application lifecycle for one-shot, streaming, and interactive modes."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .engine import MonitorEngine
from .models import LifecycleState, MonitorSnapshot, NetworkState
from .presentation.json_output import render_json
from .presentation.text import render_text
from .presentation.tui import run_tui


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


def exit_code(snapshot: MonitorSnapshot) -> int:
    sessions = snapshot.sessions
    if any(session.lifecycle == LifecycleState.FAILED for session in sessions):
        return 3
    if any(session.alert_level == "严重" for session in sessions):
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
    engine = MonitorEngine(
        interval=options.interval,
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
    interactive = (
        not options.once
        and not options.json
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        run_tui(engine, not options.no_color, options.show_auxiliary, options.flat)
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

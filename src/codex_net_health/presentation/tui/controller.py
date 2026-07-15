"""Interactive sampling loop with stable selection and group state."""

from __future__ import annotations

import select
import shutil
import time

from ...engine import MonitorEngine
from ...models import MonitorSnapshot
from .terminal import RawTerminal, emit_frame
from .views import detail_view, find_session, help_view, main_view


def run_tui(
    engine: MonitorEngine,
    use_color: bool,
    show_auxiliary: bool,
    flat: bool,
) -> MonitorSnapshot:
    engine.baseline()
    snapshot = engine.sample()
    selected_key = ""
    collapsed: set[str] = set()
    grouped = not flat
    detail_key = ""
    help_open = False
    search_active = False
    search_query = ""
    follow = True
    event_scroll = 0
    next_sample = time.monotonic() + engine.interval
    dirty = True
    refs = []
    last_size = (0, 0)
    with RawTerminal() as terminal:
        while True:
            now = time.monotonic()
            if now >= next_sample:
                snapshot = engine.sample()
                active_instances = {instance.instance_id for instance in snapshot.instances}
                collapsed.intersection_update(active_instances)
                next_sample += engine.interval
                if next_sample <= now:
                    next_sample = now + engine.interval
                dirty = True
            width, height = shutil.get_terminal_size((120, 32))
            width, height = max(48, width), max(12, height)
            if (width, height) != last_size:
                dirty = True
                last_size = (width, height)
            if dirty:
                if help_open:
                    lines = help_view(width, height, use_color)
                    refs = []
                elif detail_key:
                    selected = find_session(snapshot, detail_key)
                    engine.pin_session(selected)
                    instance = (
                        next(
                            (
                                item
                                for item in snapshot.instances
                                if selected and item.instance_id == selected.instance_id
                            ),
                            None,
                        )
                    )
                    lines = (
                        detail_view(
                            selected,
                            width,
                            height,
                            use_color,
                            follow,
                            event_scroll,
                            instance,
                            engine.machine.lookback_seconds,
                        )
                        if selected
                        else []
                    )
                    refs = []
                else:
                    lines, refs = main_view(
                        snapshot,
                        width,
                        height,
                        selected_key,
                        collapsed,
                        grouped,
                        show_auxiliary,
                        use_color,
                        search_query,
                        search_active,
                        follow,
                    )
                    keys = [ref.key for ref in refs]
                    if selected_key not in keys:
                        selected_key = keys[0] if keys else ""
                        lines, refs = main_view(
                            snapshot,
                            width,
                            height,
                            selected_key,
                            collapsed,
                            grouped,
                            show_auxiliary,
                            use_color,
                            search_query,
                            search_active,
                            follow,
                        )
                    selected_ref = next(
                        (ref for ref in refs if ref.key == selected_key),
                        None,
                    )
                    selected_session = (
                        find_session(snapshot, selected_ref.session_key)
                        if selected_ref and selected_ref.kind == "session"
                        else None
                    )
                    engine.pin_session(selected_session)
                emit_frame(lines, width, height)
                dirty = False
            timeout = max(0.0, next_sample - time.monotonic())
            ready, _, _ = select.select([terminal.fd], [], [], min(timeout, 0.25))
            if not ready:
                continue
            key = terminal.read_key()
            if search_active:
                if key in {"\r", "\n"}:
                    search_active = False
                elif key == "\x1b":
                    search_active = False
                    search_query = ""
                elif key in {"\x7f", "\b"}:
                    search_query = search_query[:-1]
                elif key and key.isprintable():
                    search_query += key
                dirty = True
                continue
            if key == "q":
                return snapshot
            if help_open:
                if key in {"?", "\x1b", "\r", "\n"}:
                    help_open = False
                    dirty = True
                continue
            if detail_key:
                if key in {"\x1b", "\r", "\n"}:
                    detail_key = ""
                    event_scroll = 0
                    dirty = True
                elif key == "f":
                    follow = not follow
                    event_scroll = 0
                    dirty = True
                elif key in {"j", "\x1b[B"} and not follow:
                    event_scroll += 1
                    dirty = True
                elif key in {"k", "\x1b[A"}:
                    follow = False
                    event_scroll = max(0, event_scroll - 1)
                    dirty = True
                continue
            keys = [ref.key for ref in refs]
            if key in {"j", "\x1b[B"} and keys:
                index = keys.index(selected_key) if selected_key in keys else 0
                selected_key = keys[min(len(keys) - 1, index + 1)]
                dirty = True
            elif key in {"k", "\x1b[A"} and keys:
                index = keys.index(selected_key) if selected_key in keys else 0
                selected_key = keys[max(0, index - 1)]
                dirty = True
            elif key == "g":
                grouped = not grouped
                dirty = True
            elif key == "a":
                show_auxiliary = not show_auxiliary
                dirty = True
            elif key == "/":
                search_active = True
                dirty = True
            elif key == "?":
                help_open = True
                dirty = True
            elif key == "f":
                follow = not follow
                dirty = True
            elif key == "\t":
                anomalies = [
                    session
                    for session in snapshot.sessions
                    if (
                        session.current_failure
                        or session.alert_level == "严重"
                        or session.network.state.value == "STALLED"
                    )
                ]
                if anomalies:
                    search_query = ""
                    anomaly_keys = [f"session:{session.key}" for session in anomalies]
                    if selected_key in anomaly_keys:
                        index = (anomaly_keys.index(selected_key) + 1) % len(anomaly_keys)
                    else:
                        index = 0
                    selected = anomalies[index]
                    collapsed.discard(selected.instance_id)
                    selected_key = f"session:{selected.key}"
                    dirty = True
            elif key in {"\r", "\n"}:
                selected = next((ref for ref in refs if ref.key == selected_key), None)
                if selected and selected.kind == "group":
                    if selected.instance_id in collapsed:
                        collapsed.remove(selected.instance_id)
                    else:
                        collapsed.add(selected.instance_id)
                    dirty = True
                elif selected and selected.kind == "session":
                    detail_key = selected.session_key
                    event_scroll = 0
                    dirty = True

"""Interactive sampling loop with stable selection and group state."""

from __future__ import annotations

import select
import shutil
import time

from engine import MonitorEngine
from models import MonitorSnapshot
from .terminal import RawTerminal, emit_frame
from .views import (
    compare_layout,
    detail_layout,
    find_session,
    help_view,
    home_layout,
    main_layout,
)


UP = {"k", "\x1b[A"}
DOWN = {"j", "\x1b[B"}
PAGE_UP = {"\x1b[5~", "\x15"}
PAGE_DOWN = {"\x1b[6~", "\x04"}
HOME = {"\x1b[H", "\x1b[1~", "\x1bOH"}
END = {"G", "\x1b[F", "\x1b[4~", "\x1bOF"}


def detail_scroll_action(
    key: str,
    top: int,
    max_top: int,
    body_height: int,
    mode: str,
    follow: bool,
) -> tuple[int, bool]:
    """Apply one navigation key using view-computed visual-row boundaries."""
    current = top
    if key in UP:
        if follow and mode == "timeline":
            return max(0, max_top - 1), False
        current -= 1
    elif key in DOWN:
        current += 1
    elif key in PAGE_UP:
        current -= max(1, body_height)
        if mode == "timeline":
            follow = False
    elif key in PAGE_DOWN:
        current += max(1, body_height)
    elif key in HOME or key == "g":
        current = 0
        if mode == "timeline":
            follow = False
    elif key in END:
        current = max_top
        if mode == "timeline":
            follow = True
    return min(max_top, max(0, current)), follow


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
    home_key = ""
    home_selected_key = ""
    home_top = 0
    compare_open = False
    compare_top = 0
    help_open = False
    search_active = False
    search_query = ""
    follow = True
    detail_mode = "timeline"
    detail_scrolls = {"timeline": 0, "turns": 0, "evidence": 0}
    list_top = 0
    next_sample = time.monotonic() + engine.interval
    dirty = True
    refs = []
    last_size = (0, 0)
    first_frame = True
    layout = None
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
            # Never render beyond the real pane: forced minimum dimensions make
            # narrow or short terminals wrap and scroll away the first rows.
            width, height = max(1, width), max(3, height)
            if (width, height) != last_size:
                dirty = True
                last_size = (width, height)
            if dirty:
                if help_open:
                    lines = help_view(width, height, use_color)
                    refs = []
                elif compare_open:
                    layout = compare_layout(snapshot, width, height, use_color, compare_top)
                    compare_top = layout.top
                    lines, refs = layout.lines, []
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
                    if selected:
                        layout = detail_layout(
                            selected,
                            width,
                            height,
                            use_color,
                            detail_mode,
                            follow,
                            detail_scrolls[detail_mode],
                            instance,
                            engine.machine.lookback_seconds,
                        )
                        if not (follow and detail_mode == "timeline"):
                            detail_scrolls[detail_mode] = layout.top
                        lines = layout.lines
                    else:
                        lines = []
                        layout = None
                    refs = []
                elif home_key:
                    instance = next(
                        (item for item in snapshot.instances if item.instance_id == home_key),
                        None,
                    )
                    if instance:
                        layout = home_layout(
                            instance,
                            width,
                            height,
                            home_selected_key,
                            use_color,
                            home_top,
                            search_query,
                        )
                        keys = [ref.key for ref in layout.all_refs]
                        if home_selected_key not in keys:
                            home_selected_key = keys[0] if keys else ""
                            layout = home_layout(
                                instance,
                                width,
                                height,
                                home_selected_key,
                                use_color,
                                home_top,
                                search_query,
                            )
                        home_top = layout.top
                        lines, refs = layout.lines, layout.refs
                        selected_ref = next(
                            (ref for ref in layout.all_refs if ref.key == home_selected_key),
                            None,
                        )
                        engine.pin_session(
                            find_session(snapshot, selected_ref.session_key)
                            if selected_ref else None
                        )
                    else:
                        home_key = ""
                        lines, refs = [], []
                else:
                    layout = main_layout(
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
                        list_top,
                    )
                    keys = [ref.key for ref in layout.all_refs]
                    if selected_key not in keys:
                        selected_key = keys[0] if keys else ""
                        layout = main_layout(
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
                            list_top,
                        )
                    list_top = layout.top
                    lines, refs = layout.lines, layout.refs
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
                emit_frame(lines, width, height, clear=first_frame)
                first_frame = False
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
            if compare_open:
                if key in {"c", "\x1b"}:
                    compare_open = False
                    dirty = True
                elif layout and key in UP | DOWN | PAGE_UP | PAGE_DOWN | HOME | END:
                    compare_top, _ = detail_scroll_action(
                        key,
                        compare_top,
                        layout.max_top,
                        layout.body_height,
                        "compare",
                        False,
                    )
                    dirty = True
                continue
            if detail_key:
                if key == "\x1b":
                    detail_key = ""
                    dirty = True
                elif key in {"1", "2", "3"}:
                    detail_mode = {"1": "timeline", "2": "turns", "3": "evidence"}[key]
                    dirty = True
                elif key == "f":
                    if detail_mode == "timeline":
                        follow = not follow
                        if follow and layout:
                            detail_scrolls[detail_mode] = layout.max_top
                        dirty = True
                elif key == "x":
                    selected = find_session(snapshot, detail_key)
                    active_alerts = (
                        [alert for alert in selected.alerts if alert.active]
                        if selected else []
                    )
                    if selected and active_alerts:
                        machine_key = f"{selected.instance_id}:{selected.session_id}"
                        engine.machine.acknowledge_alert(
                            machine_key,
                            active_alerts[-1].id,
                        )
                        dirty = True
                elif layout and key in (
                    UP | DOWN | PAGE_UP | PAGE_DOWN | HOME | END | {"g"}
                ):
                    current = (
                        layout.top
                        if follow and detail_mode == "timeline"
                        else detail_scrolls[detail_mode]
                    )
                    detail_scrolls[detail_mode], follow = detail_scroll_action(
                        key,
                        current,
                        layout.max_top,
                        layout.body_height,
                        detail_mode,
                        follow,
                    )
                    dirty = True
                continue
            if home_key:
                keys = [ref.key for ref in layout.all_refs] if layout else []
                if key == "\x1b":
                    home_key = ""
                    search_query = ""
                    dirty = True
                elif key in DOWN | UP | PAGE_DOWN | PAGE_UP | HOME | END and keys:
                    index = (
                        keys.index(home_selected_key)
                        if home_selected_key in keys
                        else 0
                    )
                    if key in DOWN:
                        index += 1
                    elif key in UP:
                        index -= 1
                    elif key in PAGE_DOWN:
                        index += max(1, layout.body_height)
                    elif key in PAGE_UP:
                        index -= max(1, layout.body_height)
                    elif key in HOME:
                        index = 0
                    else:
                        index = len(keys) - 1
                    home_selected_key = keys[min(len(keys) - 1, max(0, index))]
                    dirty = True
                elif key == "/":
                    search_active = True
                    dirty = True
                elif key == "c":
                    compare_open = True
                    compare_top = 0
                    dirty = True
                elif key in {"\r", "\n"}:
                    selected = next(
                        (ref for ref in layout.all_refs if ref.key == home_selected_key),
                        None,
                    )
                    if selected:
                        detail_key = selected.session_key
                        detail_mode = "timeline"
                        follow = True
                        dirty = True
                continue
            keys = [ref.key for ref in layout.all_refs] if layout else []
            if key in DOWN and keys:
                index = keys.index(selected_key) if selected_key in keys else 0
                selected_key = keys[min(len(keys) - 1, index + 1)]
                dirty = True
            elif key in UP and keys:
                index = keys.index(selected_key) if selected_key in keys else 0
                selected_key = keys[max(0, index - 1)]
                dirty = True
            elif key in PAGE_DOWN | PAGE_UP | HOME | END and keys:
                index = keys.index(selected_key) if selected_key in keys else 0
                page = max(1, layout.body_height if layout else 1)
                if key in PAGE_DOWN:
                    index += page
                elif key in PAGE_UP:
                    index -= page
                elif key in HOME:
                    index = 0
                else:
                    index = len(keys) - 1
                selected_key = keys[min(len(keys) - 1, max(0, index))]
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
                    home_key = selected.instance_id
                    home_selected_key = ""
                    home_top = 0
                    dirty = True
                elif selected and selected.kind == "session":
                    detail_key = selected.session_key
                    detail_mode = "timeline"
                    follow = True
                    dirty = True
            elif key == " ":
                selected = next((ref for ref in refs if ref.key == selected_key), None)
                if selected and selected.kind == "group":
                    if selected.instance_id in collapsed:
                        collapsed.remove(selected.instance_id)
                    else:
                        collapsed.add(selected.instance_id)
                    dirty = True

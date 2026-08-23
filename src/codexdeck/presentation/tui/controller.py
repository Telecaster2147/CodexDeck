"""Public entry point for the Textual interactive monitor."""

from __future__ import annotations

from codexdeck.engine import MonitorEngine
from codexdeck.models import MonitorSnapshot
from codexdeck.presentation.tui.textual_app import run_textual_tui


def run_tui(
    engine: MonitorEngine,
    use_color: bool,
    flat: bool,
    show_all: bool = False,
) -> MonitorSnapshot:
    return run_textual_tui(engine, use_color, flat, show_all)

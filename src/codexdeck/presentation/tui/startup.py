"""Branded startup layer rendered while initial evidence becomes coherent."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from codexdeck.config import VERSION

STARTUP_FRAME_INTERVAL = 0.10
STARTUP_DURATION = 3.0
STARTUP_FRAMES_PER_STAGE = 6
STARTUP_SYSTEMS = ("CORE", "EVENTS", "TERMINALS", "NETWORK")
STARTUP_STAGES = (
    "DISCOVERING ACTIVE SESSIONS",
    "CORRELATING ROLLOUT EVENTS",
    "VERIFYING TERMINAL PROCESSES",
    "CONSOLE READY",
)

STARTUP_LOGO = (
    " ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗",
    "██╔════╝██╔═══██╗██╔══██╗██╔════╝╚██╗██╔╝",
    "██║     ██║   ██║██║  ██║█████╗   ╚███╔╝ ",
    "██║     ██║   ██║██║  ██║██╔══╝   ██╔██╗ ",
    "╚██████╗╚██████╔╝██████╔╝███████╗██╔╝ ██╗",
    " ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝",
)

STARTUP_DECK_LOGO = (
    "██████╗ ███████╗ ██████╗██╗  ██╗",
    "██╔══██╗██╔════╝██╔════╝██║ ██╔╝",
    "██║  ██║█████╗  ██║     █████╔╝ ",
    "██║  ██║██╔══╝  ██║     ██╔═██╗ ",
    "██████╔╝███████╗╚██████╗██║  ██╗",
    "╚═════╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝",
)


def startup_renderable(frame: int, *, compact: bool = False) -> Text:
    """Build one stable startup animation frame for wide or compact terminals."""

    stage = min(frame // STARTUP_FRAMES_PER_STAGE, len(STARTUP_STAGES) - 1)
    text = Text(justify="center")
    if compact:
        text.append("CODEXDECK\n", style="bold #67d8ff")
    else:
        for line in STARTUP_LOGO:
            text.append(f"{line}\n", style="bold #67d8ff")
        for line in STARTUP_DECK_LOGO:
            text.append(f"{line}\n", style="bold #f8fafc")
    text.append("\nREAD-ONLY PROCESS OBSERVATORY\n\n", style="bold #cbd5e1")

    for index, system in enumerate(STARTUP_SYSTEMS):
        active = index <= stage
        text.append("◆ " if active else "◇ ", style="#67d8ff" if active else "#334155")
        text.append(system, style="bold #e2e8f0" if active else "#64748b")
        if index < len(STARTUP_SYSTEMS) - 1:
            text.append("   " if compact else "    ")

    rail_width = 24 if compact else 40
    filled = max(1, round(rail_width * (stage + 1) / len(STARTUP_STAGES)))
    text.append("\n\n")
    text.append("━" * filled, style="#67d8ff")
    text.append("━" * (rail_width - filled), style="#1e293b")
    text.append(f"\n{STARTUP_STAGES[stage]}", style="bold #94a3b8")
    text.append(f"\n\nv{VERSION}", style="#475569")
    return text


class StartupOverlay(Static):
    """Short-lived brand layer shown while the initial snapshot is prepared."""

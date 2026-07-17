"""Persistent presentation preferences for the Textual interface."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


DISPLAY_MODES = {"operational", "diagnostic"}


@dataclass(frozen=True)
class TuiPreferences:
    """User-controlled TUI behavior that is safe to persist locally."""

    grouped: bool = True
    show_auxiliary: bool = False
    mode: str = "operational"


def preferences_path() -> Path:
    """Return the platform-appropriate CodexNet preferences path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "codexnet" / "settings.json"


def load_preferences(path: Path | None = None) -> TuiPreferences:
    """Load valid preference values and fall back field-by-field on bad input."""
    target = path or preferences_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TuiPreferences()
    if not isinstance(payload, dict):
        return TuiPreferences()

    defaults = TuiPreferences()
    values: dict[str, object] = {}
    legacy_mode = "diagnostic" if payload.get("trace_detail") == "full" else None
    for field in fields(defaults):
        value = payload.get(field.name, getattr(defaults, field.name))
        if field.name == "mode":
            candidate = legacy_mode or value
            values[field.name] = candidate if candidate in DISPLAY_MODES else defaults.mode
        else:
            values[field.name] = value if isinstance(value, bool) else getattr(defaults, field.name)
    return TuiPreferences(**values)


def save_preferences(preferences: TuiPreferences, path: Path | None = None) -> None:
    """Atomically save preferences without modifying any Codex-owned data."""
    target = path or preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(preferences), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)

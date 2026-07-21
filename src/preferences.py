"""CodexDeck-owned user preferences stored outside Codex data directories."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CodexDeckPreferences:
    startup_animation: bool = True
    group_sessions: bool = True
    show_hidden_sessions: bool = False
    follow_output: bool = True
    notifications: bool = True
    theme: str = "codexdeck-blue"
    default_tab: str = "activity"


THEMES = {"codexdeck-blue", "textual-dark", "textual-light"}
DEFAULT_TABS = {"activity", "diagnosis", "terminal"}


def preferences_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    config_root = values.get("XDG_CONFIG_HOME")
    root = Path(config_root).expanduser() if config_root else Path.home() / ".config"
    return root / "codexdeck" / "preferences.json"


def load_preferences(path: Path | None = None) -> CodexDeckPreferences:
    target = path or preferences_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return CodexDeckPreferences()
    if not isinstance(payload, dict):
        return CodexDeckPreferences()
    defaults = CodexDeckPreferences()

    def boolean(name: str, default: bool) -> bool:
        value = payload.get(name)
        return value if isinstance(value, bool) else default

    theme = payload.get("theme")
    default_tab = payload.get("default_tab")
    return CodexDeckPreferences(
        startup_animation=boolean("startup_animation", defaults.startup_animation),
        group_sessions=boolean("group_sessions", defaults.group_sessions),
        show_hidden_sessions=boolean(
            "show_hidden_sessions",
            defaults.show_hidden_sessions,
        ),
        follow_output=boolean("follow_output", defaults.follow_output),
        notifications=boolean("notifications", defaults.notifications),
        theme=theme if theme in THEMES else defaults.theme,
        default_tab=default_tab if default_tab in DEFAULT_TABS else defaults.default_tab,
    )


def save_preferences(
    preferences: CodexDeckPreferences,
    path: Path | None = None,
) -> Path:
    target = path or preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(asdict(preferences), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target

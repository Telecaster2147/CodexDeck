from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from preferences import (  # noqa: E402
    CodexDeckPreferences,
    load_preferences,
    preferences_path,
    save_preferences,
)


class PreferencesTests(unittest.TestCase):
    def test_path_uses_xdg_config_home(self) -> None:
        self.assertEqual(
            preferences_path({"XDG_CONFIG_HOME": "/tmp/config-root"}),
            Path("/tmp/config-root/codexdeck/preferences.json"),
        )

    def test_round_trip_and_invalid_file_default(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "preferences.json"
            preferences = CodexDeckPreferences(
                startup_animation=False,
                group_sessions=False,
                show_hidden_sessions=True,
                follow_output=False,
                notifications=False,
                theme="textual-light",
                default_tab="terminal",
            )
            saved = save_preferences(preferences, path)
            self.assertEqual(saved, path)
            self.assertEqual(load_preferences(path), preferences)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "startup_animation": False,
                    "group_sessions": False,
                    "show_hidden_sessions": True,
                    "follow_output": False,
                    "notifications": False,
                    "theme": "textual-light",
                    "default_tab": "terminal",
                },
            )

            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_preferences(path), CodexDeckPreferences())

            path.write_bytes(b'{"theme":"\xff"}')
            self.assertEqual(load_preferences(path), CodexDeckPreferences())

    def test_invalid_or_partial_values_fall_back_per_field(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "preferences.json"
            path.write_text(
                json.dumps(
                    {
                        "startup_animation": False,
                        "group_sessions": "no",
                        "theme": "unknown",
                        "default_tab": "metrics",
                    }
                ),
                encoding="utf-8",
            )

            preferences = load_preferences(path)

            self.assertFalse(preferences.startup_animation)
            self.assertTrue(preferences.group_sessions)
            self.assertEqual(preferences.theme, "codexdeck-blue")
            self.assertEqual(preferences.default_tab, "activity")

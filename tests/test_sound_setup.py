from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sound_setup import (
    SoundSetupError,
    configure_sound,
    enable_codexdeck_sounds,
    enable_vscode_terminal_bell,
    find_vscode_settings,
)


class SoundSetupTests(unittest.TestCase):
    def test_codexdeck_preferences_are_merged_without_losing_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text('{"theme":"textual-light","sound_enabled":false}\n', encoding="utf-8")

            enable_codexdeck_sounds(path)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["theme"], "textual-light")
            self.assertTrue(payload["sound_enabled"])
            self.assertTrue(payload["attention_sound"])
            self.assertTrue(payload["completion_sound"])

    def test_invalid_preferences_are_preserved_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text("{invalid", encoding="utf-8")

            with self.assertRaises(SoundSetupError):
                enable_codexdeck_sounds(path)

            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")

    def test_vscode_jsonc_is_updated_with_backup_and_comments_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = """{
    // Existing user preference.
    "editor.fontSize": 15,
}
"""
            path.write_text(original, encoding="utf-8")

            enable_vscode_terminal_bell(path)

            updated = path.read_text(encoding="utf-8")
            self.assertIn("// Existing user preference.", updated)
            self.assertIn('"accessibility.signals.terminalBell":', updated)
            self.assertIn('"sound":"on"', updated)
            self.assertIn('"terminal.integrated.enableVisualBell": true', updated)
            self.assertEqual(
                path.with_name("settings.json.codexdeck-backup").read_text(encoding="utf-8"),
                original,
            )

    def test_existing_vscode_sound_values_are_replaced_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                """{
    "accessibility.signals.terminalBell": {
        "sound": "off",
        "announcement": "auto"
    },
    "terminal.integrated.enableVisualBell": false
}
""",
                encoding="utf-8",
            )

            enable_vscode_terminal_bell(path)
            once = path.read_text(encoding="utf-8")
            enable_vscode_terminal_bell(path)

            self.assertEqual(path.read_text(encoding="utf-8"), once)
            self.assertIn('"sound":"on"', once)
            self.assertIn('"announcement":"off"', once)
            self.assertIn('"terminal.integrated.enableVisualBell": true', once)

    def test_explicit_vscode_path_is_detected_and_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferences = root / "preferences.json"
            settings = root / "settings.json"
            settings.write_text("{}\n", encoding="utf-8")
            environment = {
                "TERM_PROGRAM": "vscode",
                "CODEXDECK_VSCODE_SETTINGS": str(settings),
            }

            self.assertEqual(find_vscode_settings(environment), settings)
            result = configure_sound(
                environment=environment,
                preferences_file=preferences,
            )

            self.assertEqual(result.terminal, "vscode")
            self.assertTrue(result.vscode_configured)
            self.assertEqual(result.vscode_settings_path, settings)

    def test_vscode_remote_machine_settings_are_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            remote_settings = home / ".vscode-server/data/Machine/settings.json"
            remote_settings.parent.mkdir(parents=True)
            remote_settings.write_text("{}\n", encoding="utf-8")
            local_settings = home / ".config/Code/User/settings.json"
            local_settings.parent.mkdir(parents=True)
            local_settings.write_text("{}\n", encoding="utf-8")
            environment = {"HOME": str(home), "TERM_PROGRAM": "vscode"}

            self.assertEqual(find_vscode_settings(environment), remote_settings)

            result = configure_sound(
                environment=environment,
                preferences_file=root / "preferences.json",
            )

            self.assertEqual(result.vscode_settings_path, remote_settings)
            self.assertIn(
                '"accessibility.signals.terminalBell"',
                remote_settings.read_text(encoding="utf-8"),
            )
            self.assertEqual(local_settings.read_text(encoding="utf-8"), "{}\n")

    def test_missing_remote_settings_file_is_created_in_existing_machine_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            machine = home / ".vscode-server/data/Machine"
            machine.mkdir(parents=True)
            environment = {"HOME": str(home), "TERM_PROGRAM": "vscode"}

            settings = find_vscode_settings(environment)
            self.assertEqual(settings, machine / "settings.json")

            result = configure_sound(
                environment=environment,
                preferences_file=root / "preferences.json",
            )

            self.assertTrue(result.vscode_configured)
            assert settings is not None
            self.assertTrue(settings.is_file())
            self.assertFalse(settings.with_name("settings.json.codexdeck-backup").exists())

    def test_empty_vscode_settings_file_is_initialized_and_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("", encoding="utf-8")

            enable_vscode_terminal_bell(path)

            self.assertIn(
                '"accessibility.signals.terminalBell"',
                path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                path.with_name("settings.json.codexdeck-backup").read_text(encoding="utf-8"),
                "",
            )


if __name__ == "__main__":
    unittest.main()

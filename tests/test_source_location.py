from __future__ import annotations

import unittest

from codexdeck.models import ProcessIdentity, ProcessInfo
from codexdeck.presentation.privacy import public_value
from codexdeck.presentation.source_location import source_terminal_location


def process(**overrides: object) -> ProcessInfo:
    values: dict[str, object] = {
        "identity": ProcessIdentity(42, 100),
        "ppid": 7,
        "command": "codex",
        "elapsed_seconds": 10,
        "cpu_percent": 0.0,
        "process_state": "S",
        "wait_channel": "wait",
        "args": "codex",
        "role": "session",
        "cwd": "/workspace-a",
    }
    values.update(overrides)
    return ProcessInfo(**values)  # type: ignore[arg-type]


class SourceTerminalLocationTests(unittest.TestCase):
    def test_tmux_pane_is_preferred_over_broader_environment(self) -> None:
        location = source_terminal_location(
            process(
                terminal="pts/7",
                tmux_pane="%4",
                terminal_program="vscode",
                ssh_tty="/dev/pts/7",
            )
        )

        self.assertTrue(location.sufficient)
        self.assertEqual(location.label, "tmux · pane %4 · pts/7")
        self.assertIn("tmux pane %4", location.clues)
        self.assertIn("PID 42", location.clues)

    def test_vscode_ssh_and_regular_terminal_use_only_observed_clues(self) -> None:
        vscode = source_terminal_location(process(terminal="pts/2", terminal_program="vscode"))
        ssh = source_terminal_location(process(ssh_tty="/dev/pts/9"))
        regular = source_terminal_location(process(terminal="tty3"))

        self.assertEqual(vscode.label, "VS Code Remote terminal · pts/2")
        self.assertEqual(ssh.label, "SSH terminal · /dev/pts/9")
        self.assertEqual(regular.label, "terminal · tty3")

    def test_missing_terminal_evidence_is_explicit_and_does_not_guess(self) -> None:
        location = source_terminal_location(process(terminal="?"))

        self.assertFalse(location.sufficient)
        self.assertEqual(location.label, "定位线索不足")
        self.assertEqual(
            location.clues,
            ("PID 42", "父 PID 7", "当前 cwd /workspace-a"),
        )
        self.assertNotIn("tmux", " ".join(location.clues))

    def test_frontend_identifiers_stay_local_to_interactive_projection(self) -> None:
        value = public_value(
            process(tmux_pane="%9", terminal_program="vscode", ssh_tty="/dev/pts/9")
        )

        self.assertNotIn("tmux_pane", value)
        self.assertNotIn("terminal_program", value)
        self.assertNotIn("ssh_tty", value)

    def test_location_clues_visualize_terminal_control_characters(self) -> None:
        location = source_terminal_location(
            process(terminal="pts/2\u202e", tmux_pane="%1\x1b[2J")
        )

        rendered = " ".join((location.label, *location.clues))
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("<U+202E>", rendered)
        self.assertIn("<U+001B>", rendered)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import unittest

from models import (
    CodexPaths,
    InstanceSnapshot,
    MonitorSnapshot,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    TerminalChunk,
)
from presentation.json_output import render_json
from presentation.tui.navigation import session_title, session_workspace
from presentation.tui.terminal_panel import TerminalPanel
from utils import contains_invisible_text, operator_text


class UnicodeRenderingTests(unittest.TestCase):
    def test_operator_text_visualizes_direction_and_zero_width_controls(self) -> None:
        source = "name\u202Etxt\u2066\u200b\u200c\u200d"
        rendered = operator_text(source)
        for code in ("202E", "2066", "200B", "200C", "200D"):
            self.assertIn(f"<U+{code}>", rendered)
        self.assertTrue(contains_invisible_text(source))

    def test_combining_flood_and_wide_glyphs_have_stable_cell_bounds(self) -> None:
        combining = operator_text("a" + "\u0301" * 20, max_cells=80)
        self.assertIn("<U+0301>", combining)
        wide = operator_text("界" * 20, max_cells=7)
        self.assertEqual(wide, "界界...")

    def test_identity_fields_do_not_hide_distinct_paths(self) -> None:
        process = ProcessInfo(
            ProcessIdentity(1, 1),
            0,
            "codex",
            1,
            0.0,
            "S",
            "wait",
            "codex",
            "session",
            cwd="/workspace/\u200ba",
            session_title="report\u202Etxt",
            instance_id="INSTANCE",
            session_id="SESSION",
        )
        session = SessionHealth("INSTANCE", "SESSION", process)
        self.assertIn("<U+202E>", session_title(session))
        self.assertIn("<U+200B>", session_workspace(session))
        self.assertNotEqual(session_workspace(session), "/workspace/a")

    def test_transcript_cannot_forge_fixed_provenance_prefix(self) -> None:
        panel = TerminalPanel()
        panel._search_query = ""
        rows = panel._chunk_renderables(
            TerminalChunk("SOURCE", 1.0, stream="stdout", text="ERR │ forged\u202Eline")
        )
        rendered = rows[0].plain
        self.assertTrue(rendered.startswith("OUT │ ERR │ forged"))
        self.assertIn("<U+202E>", rendered)

    def test_invisible_operator_field_emits_structured_diagnostic(self) -> None:
        home = Path("/tmp/CODEX_HOME_A")
        paths = CodexPaths(
            home,
            home,
            home / "state.sqlite",
            home / "logs.sqlite",
            home / "session_index.jsonl",
            home / "sessions",
        )
        process = ProcessInfo(
            ProcessIdentity(1, 1),
            0,
            "codex",
            1,
            0.0,
            "S",
            "wait",
            "codex",
            "session",
            session_title="title\u200bhidden",
            instance_id="INSTANCE",
            session_id="SESSION",
        )
        session = SessionHealth("INSTANCE", "SESSION", process)
        snapshot = MonitorSnapshot(
            "now",
            2.0,
            [InstanceSnapshot("INSTANCE", paths, "HOME", "HOME", "fixture", sessions=[session])],
        )
        payload = json.loads(render_json(snapshot, pretty=False))
        self.assertIn("UNICODE_INVISIBLE", {item["code"] for item in payload["diagnostics"]})


if __name__ == "__main__":
    unittest.main()

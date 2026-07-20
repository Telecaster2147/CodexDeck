"""Optional compact evidence sources behind one read-only adapter."""

from __future__ import annotations

from pathlib import Path

from codex.hook_events import HookEventReader
from codex.tui_session_log import SessionLogReadResult, TuiSessionLogReader
from models import NormalizedEvent


class CompactEvidenceReader:
    """Coordinate optional compact side channels without making them authoritative."""

    def __init__(self, hook_events_path: Path | None = None) -> None:
        self.session_logs = TuiSessionLogReader()
        self.hooks = HookEventReader(hook_events_path)

    def read_hooks(self) -> list[tuple[str, NormalizedEvent]]:
        return self.hooks.read()

    def read_session_log(
        self,
        path: Path | None,
        *,
        default_session_id: str = "",
    ) -> SessionLogReadResult:
        return self.session_logs.read(path, default_session_id=default_session_id)

    def prune_session_logs(self, active_paths: set[str]) -> None:
        self.session_logs.prune(active_paths)

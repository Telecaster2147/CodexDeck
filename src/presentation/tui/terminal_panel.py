"""Read-only current background-process terminal panel."""

from __future__ import annotations

import re
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Input, RichLog, Static

from models import SessionHealth, TerminalCapability, TerminalSessionSummary
from utils import compact_path, format_duration


class TerminalLog(RichLog):
    can_focus = True


RUNNING_TERMINAL_STATUSES = frozenset({"running", "in_progress"})


class TerminalPanel(Vertical):
    """Read-only view of currently running background processes."""

    CAPABILITY_LABELS = {
        TerminalCapability.STREAMING: "RESERVED",
        TerminalCapability.FILE_TAIL: "FILE",
        TerminalCapability.POLL_TRANSCRIPT: "POLL",
        TerminalCapability.FINAL_TRANSCRIPT: "FINAL",
        TerminalCapability.METADATA_ONLY: "META",
    }

    def compose(self) -> ComposeResult:
        yield Static("BACKGROUND TERMINAL  ·  当前没有运行中的后台进程", id="terminal-header")
        yield Input(
            placeholder="搜索当前终端输出",
            id="terminal-search",
            classes="settled",
        )
        yield DataTable(id="terminal-list", cursor_type="row", show_header=False)
        yield TerminalLog(
            id="terminal-output",
            min_width=1,
            wrap=True,
            markup=False,
            auto_scroll=True,
            max_lines=5000,
        )

    def on_mount(self) -> None:
        table = self.query_one("#terminal-list", DataTable)
        table.add_column("后台进程", key="process")
        self._session_key = ""
        self._terminals: dict[str, TerminalSessionSummary] = {}
        self._row_keys: tuple[str, ...] = ()
        self._selected_terminal_id = ""
        self._output_signatures: tuple[tuple[object, ...], ...] = ()
        self._output_terminal_id = ""
        self._search_query = ""
        self._match_rows: list[int] = []
        self._match_index = -1

    @staticmethod
    def _command_label(terminal: TerminalSessionSummary) -> str:
        command = terminal.command or "后台进程"
        return command if len(command) <= 80 else command[:79] + "…"

    def _process_label(self, terminal: TerminalSessionSummary, now: float) -> Text:
        label = Text("● ", style="bold #4ade80")
        label.append(terminal.process_id, style="bold #e2e8f0")
        label.append(f"  {self._command_label(terminal)}", style="#cbd5e1")
        if terminal.last_output_at is not None:
            label.append(
                f"  ·  {format_duration(max(0, now - terminal.last_output_at))}前",
                style="#64748b",
            )
        return label

    def show_session(self, session: SessionHealth | None, *, follow: bool) -> None:
        if not self.is_mounted:
            return
        terminals = [
            terminal
            for terminal in (session.terminal_sessions if session else [])
            if terminal.status in RUNNING_TERMINAL_STATUSES
            and bool(terminal.process_id)
            and not terminal.stale
        ]
        self._session_key = session.key if session else ""
        self._terminals = {item.terminal_id: item for item in terminals}
        self.set_class(len(terminals) <= 1, "single-terminal")
        row_keys = tuple(item.terminal_id for item in terminals)
        table = self.query_one("#terminal-list", DataTable)
        if row_keys != self._row_keys:
            previous = self._selected_terminal_id
            table.clear(columns=False)
            now = time.time()
            for terminal in terminals:
                table.add_row(
                    self._process_label(terminal, now),
                    key=terminal.terminal_id,
                )
            self._row_keys = row_keys
            selected = previous if previous in row_keys else next(
                (item.terminal_id for item in reversed(terminals) if item.status == "running"),
                row_keys[-1] if row_keys else "",
            )
            self._selected_terminal_id = selected
            if selected:
                table.move_cursor(row=row_keys.index(selected))
        else:
            now = time.time()
            for terminal in terminals:
                table.update_cell(
                    terminal.terminal_id,
                    "process",
                    self._process_label(terminal, now),
                )
        if not terminals:
            self._selected_terminal_id = ""
        self._render_selected(follow)

    def _header(self, terminal: TerminalSessionSummary | None) -> Text:
        header = Text()
        if terminal is None:
            header.append("BACKGROUND TERMINAL", style="bold #38bdf8")
            header.append("  ·  当前没有运行中的后台进程", style="#64748b")
            return header
        capability = self.CAPABILITY_LABELS.get(terminal.capability, terminal.capability.value)
        header.append("● RUNNING", style="bold #4ade80")
        header.append(
            f"  ·  PID {terminal.process_id}  ·  {capability}",
            style="#e2e8f0",
        )
        header.append("  ·  READ ONLY", style="#64748b")
        if terminal.dropped_bytes:
            header.append(f"  ·  DROP {terminal.dropped_bytes} B", style="#fbbf24")
        if terminal.upstream_truncated:
            header.append("  ·  TRUNCATED", style="bold #fbbf24")
        if self._search_query:
            current = self._match_index + 1 if self._match_rows else 0
            header.append(
                f"  ·  MATCH {current}/{len(self._match_rows)}",
                style="bold #fbbf24" if self._match_rows else "bold #fca5a5",
            )
        return header

    def _prompt_renderable(self, terminal: TerminalSessionSummary) -> Text:
        prompt_path = compact_path(terminal.cwd) if terminal.cwd else "~"
        prompt = Text(prompt_path, style="bold #38bdf8")
        prompt.append(" $ ", style="bold #4ade80")
        prompt.append(terminal.command or "command unavailable", style="#f8fafc")
        if self._search_query:
            prompt.highlight_regex(
                re.escape(self._search_query),
                style="bold black on #fbbf24",
            )
        return prompt

    def _chunk_renderables(self, chunk: object) -> list[Text]:
        stream = str(getattr(chunk, "stream", "combined"))
        tag = {
            "stdout": "OUT",
            "stderr": "ERR",
            "system": "SYS",
            "combined": "TTY",
        }.get(stream, stream.upper()[:3])
        color = {
            "stdout": "#cbd5e1",
            "stderr": "#fca5a5",
            "system": "#fbbf24",
            "combined": "#e2e8f0",
        }.get(stream, "#e2e8f0")
        value = str(getattr(chunk, "text", ""))
        lines = value.splitlines() or ([""] if value else [])
        rows: list[Text] = []
        for line in lines:
            text = Text(f"{tag:<3} │ ", style=f"bold {color}")
            text.append(line, style=color)
            if self._search_query:
                text.highlight_regex(
                    re.escape(self._search_query),
                    style="bold black on #fbbf24",
                )
            rows.append(text)
        return rows

    def _terminal_rows(self, terminal: TerminalSessionSummary | None) -> list[Text]:
        if terminal is None:
            return []
        rows = [self._prompt_renderable(terminal)]
        rows.extend(
            row
            for chunk in terminal.chunks
            for row in self._chunk_renderables(chunk)
        )
        if not terminal.chunks:
            rows.append(Text("等待后台进程产生可读取输出", style="#64748b"))
        return rows

    def _render_selected(self, follow: bool) -> None:
        terminal = self._terminals.get(self._selected_terminal_id)
        rows = self._terminal_rows(terminal)
        self._match_rows = (
            [
                index
                for index, row in enumerate(rows)
                if self._search_query.lower() in row.plain.lower()
            ]
            if self._search_query
            else []
        )
        if not self._match_rows:
            self._match_index = -1
        elif self._match_index < 0 or self._match_index >= len(self._match_rows):
            self._match_index = 0
        self.query_one("#terminal-header", Static).update(self._header(terminal))
        log = self.query_one("#terminal-output", TerminalLog)
        if terminal is None:
            if self._output_terminal_id or self._output_signatures or not log.lines:
                log.clear()
                log.write(
                    Text("当前没有运行中的后台进程", style="#64748b"),
                    scroll_end=False,
                )
                log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
            self._output_terminal_id = ""
            self._output_signatures = ()
            return
        signatures = (
            ("prompt", terminal.cwd, terminal.command, self._search_query),
            *(
                (
                    chunk.source_id,
                    chunk.sequence,
                    chunk.stream,
                    chunk.text,
                    self._search_query,
                )
                for chunk in terminal.chunks
            ),
        )
        same_terminal = self._output_terminal_id == terminal.terminal_id
        previous = self._output_signatures
        was_at_end = log.is_vertical_scroll_end
        scroll_y = log.scroll_y
        should_follow = follow and (not same_terminal or was_at_end)
        log.auto_scroll = False
        if (
            same_terminal
            and len(previous) > 1
            and signatures[: len(previous)] == previous
        ):
            previous_chunk_count = len(previous) - 1
            for chunk in terminal.chunks[previous_chunk_count:]:
                for row in self._chunk_renderables(chunk):
                    log.write(row, scroll_end=False)
        elif signatures != previous or not same_terminal:
            log.clear()
            for row in rows:
                log.write(row, scroll_end=False)
        if should_follow:
            log.scroll_end(animate=False, immediate=True, x_axis=False)
            log.scroll_to(x=0, animate=False, immediate=True)
        elif same_terminal:
            log.scroll_to(x=0, y=scroll_y, animate=False, immediate=True, force=True)
        else:
            log.scroll_home(animate=False, immediate=True, x_axis=True, y_axis=True)
        log.auto_scroll = follow
        self._output_terminal_id = terminal.terminal_id
        self._output_signatures = signatures

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "terminal-list" or event.row_key is None:
            return
        self._selected_terminal_id = str(event.row_key.value)
        self._render_selected(self.app.follow)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "terminal-list":
            self.query_one("#terminal-output", TerminalLog).focus()

    def action_search(self) -> None:
        search = self.query_one("#terminal-search", Input)
        search.remove_class("settled")
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "terminal-search":
            return
        self._search_query = event.value
        self._match_index = -1
        self._output_signatures = ()
        self._render_selected(self.app.follow)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "terminal-search":
            event.input.add_class("settled")
            output = self.query_one("#terminal-output", TerminalLog)
            output.focus()
            self._scroll_to_match()

    def _scroll_to_match(self) -> None:
        if self._match_index < 0 or not self._match_rows:
            return
        self.query_one("#terminal-output", TerminalLog).scroll_to(
            y=self._match_rows[self._match_index],
            animate=False,
            immediate=True,
            force=True,
        )

    def next_match(self, direction: int) -> bool:
        if not self._match_rows:
            return False
        self._match_index = (self._match_index + direction) % len(self._match_rows)
        terminal = self._terminals.get(self._selected_terminal_id)
        self.query_one("#terminal-header", Static).update(self._header(terminal))
        self._scroll_to_match()
        return True

    def focus_transcript(self) -> None:
        target = (
            self.query_one("#terminal-output", TerminalLog)
            if len(self._terminals) <= 1
            else self.query_one("#terminal-list", DataTable)
        )
        target.focus()

    def back(self) -> bool:
        search = self.query_one("#terminal-search", Input)
        output = self.query_one("#terminal-output", TerminalLog)
        if search.has_focus:
            search.add_class("settled")
            output.focus()
            return True
        if output.has_focus:
            self.query_one("#terminal-list", DataTable).focus()
            return True
        return False

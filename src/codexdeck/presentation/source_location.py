"""Read-only clues for returning from CodexDeck to the source terminal."""

from __future__ import annotations

from dataclasses import dataclass

from codexdeck.models import ProcessInfo
from codexdeck.utils import operator_text


@dataclass(frozen=True)
class SourceTerminalLocation:
    """A conservative presentation projection over process collector evidence."""

    label: str
    clues: tuple[str, ...]
    guidance: str
    sufficient: bool


def _bounded(value: str, limit: int = 120) -> str:
    return operator_text(value, max_cells=limit, max_characters=limit * 2)


def source_terminal_location(process: ProcessInfo) -> SourceTerminalLocation:
    """Describe only stable evidence; never infer a terminal or pane identity."""

    tty = _bounded(process.terminal)
    tty_known = tty not in {"", "?", "-"}
    tmux_pane = _bounded(process.tmux_pane, 40)
    terminal_program = _bounded(process.terminal_program, 40)
    ssh_tty = _bounded(process.ssh_tty, 80)

    clues = [f"PID {process.pid}", f"父 PID {process.ppid}"]
    if tty_known:
        clues.append(f"TTY {tty}")
    if process.cwd:
        clues.append(f"当前 cwd {_bounded(process.cwd)}")

    if tmux_pane:
        clues.append(f"tmux pane {tmux_pane}")
        return SourceTerminalLocation(
            f"tmux · pane {tmux_pane}" + (f" · {tty}" if tty_known else ""),
            tuple(clues),
            f"在 tmux 中按 pane {tmux_pane} 定位；先核对 PID {process.pid}。",
            True,
        )

    if terminal_program.lower() == "vscode":
        clues.append("TERM_PROGRAM vscode")
        if tty_known:
            return SourceTerminalLocation(
                f"VS Code Remote terminal · {tty}",
                tuple(clues),
                f"在 VS Code 终端列表中核对 {tty} 与 PID {process.pid}。",
                True,
            )

    if ssh_tty:
        clues.append(f"SSH_TTY {ssh_tty}")
        return SourceTerminalLocation(
            f"SSH terminal · {ssh_tty}",
            tuple(clues),
            f"回到对应 SSH 连接并核对 {ssh_tty} 与 PID {process.pid}。",
            True,
        )

    if tty_known:
        return SourceTerminalLocation(
            f"terminal · {tty}",
            tuple(clues),
            f"在终端中按 {tty} 与 PID {process.pid} 核对源会话。",
            True,
        )

    return SourceTerminalLocation(
        "定位线索不足",
        tuple(clues),
        "根据 workspace、当前 cwd 和 PID 手工核对；当前证据不指认 terminal 或 pane。",
        False,
    )

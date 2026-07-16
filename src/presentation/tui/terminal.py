"""Raw terminal lifecycle and stable full-screen frame emission."""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty
import unicodedata

from config import (
    ALT_SCREEN_ENTER,
    ALT_SCREEN_LEAVE,
    CURSOR_HIDE,
    CURSOR_SHOW,
    ERASE_LINE,
    SCREEN_HOME_CLEAR,
)


class RawTerminal:
    def __enter__(self) -> "RawTerminal":
        self.fd = sys.stdin.fileno()
        self.settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(ALT_SCREEN_ENTER + CURSOR_HIDE)
        sys.stdout.flush()
        return self

    def __exit__(self, *_: object) -> None:
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.settings)
        sys.stdout.write(CURSOR_SHOW + ALT_SCREEN_LEAVE)
        sys.stdout.flush()

    def read_key(self) -> str:
        first_byte = os.read(self.fd, 1)
        if first_byte and first_byte[0] >= 0x80:
            leading = first_byte[0]
            length = 2 if leading < 0xE0 else 3 if leading < 0xF0 else 4
            remaining = os.read(self.fd, length - 1)
            return (first_byte + remaining).decode(errors="ignore")
        first = first_byte.decode(errors="ignore")
        if first != "\x1b":
            return first
        return first + _read_escape_tail(self.fd)


def _read_escape_tail(fd: int, timeout: float = 0.02) -> str:
    """Read one complete CSI/SS3 sequence without consuming the next key."""
    output = bytearray()
    while len(output) < 32:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break
        byte = os.read(fd, 1)
        if not byte:
            break
        output.extend(byte)
        value = byte[0]
        if len(output) == 1 and value not in {ord("["), ord("O")}:
            break
        if len(output) > 1 and 0x40 <= value <= 0x7E:
            break
    return output.decode(errors="ignore")


ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def cell_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def visible_width(text: str) -> int:
    plain = ANSI_SEQUENCE.sub("", text)
    return sum(cell_width(character) for character in plain)


def clip_ansi(text: str, width: int) -> str:
    output: list[str] = []
    cells = 0
    position = 0
    while position < len(text) and cells < width:
        match = ANSI_SEQUENCE.match(text, position)
        if match:
            output.append(match.group())
            position = match.end()
            continue
        character = text[position]
        character_width = cell_width(character)
        if cells + character_width > width:
            break
        output.append(character)
        cells += character_width
        position += 1
    if "\x1b[" in text:
        output.append("\x1b[0m")
    return "".join(output)


def emit_frame(lines: list[str], width: int, height: int, clear: bool = True) -> None:
    visible = lines[:height] + [""] * max(0, height - len(lines))
    output = [SCREEN_HOME_CLEAR] if clear else []
    # Address rows directly. Writing a newline on the terminal's bottom row can
    # scroll the whole alternate screen and make the fixed header disappear.
    for row, line in enumerate(visible, start=1):
        output.append(f"\033[{row};1H{ERASE_LINE}{clip_ansi(line, width)}")
    sys.stdout.write("".join(output))
    sys.stdout.flush()

#!/usr/bin/env python3
"""Diagnose whether the current WSL terminal presents Terminal BEL audibly."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import TextIO
import wave


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_wsl() -> bool:
    marker = (
        _read_text(Path("/proc/sys/kernel/osrelease"))
        + "\n"
        + _read_text(Path("/proc/version"))
    ).lower()
    return "microsoft" in marker or "wsl" in marker


def _terminal_name() -> str:
    if os.environ.get("WT_SESSION"):
        return "Windows Terminal"
    if os.environ.get("TERM_PROGRAM") == "vscode":
        return "VS Code integrated terminal"
    if os.environ.get("TMUX"):
        return "tmux"
    if os.environ.get("SSH_TTY"):
        return "SSH terminal"
    return os.environ.get("TERM_PROGRAM") or os.environ.get("TERM") or "unknown"


def _preferences_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "codexdeck" / "preferences.json"


def _report_preferences() -> None:
    path = _preferences_path()
    defaults = {
        "sound_enabled": False,
        "attention_sound": False,
        "completion_sound": True,
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  preferences  : {path} (missing; CodexDeck sound master defaults to off)")
        return
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        print(f"  preferences  : {path} (unreadable: {type(error).__name__})")
        return
    if not isinstance(payload, dict):
        payload = {}
    values = {
        name: payload.get(name) if isinstance(payload.get(name), bool) else default
        for name, default in defaults.items()
    }
    print(f"  preferences  : {path}")
    for name, value in values.items():
        print(f"    {name:<18}: {'on' if value else 'off'}")


def _report() -> None:
    print("CodexDeck Terminal BEL probe")
    print(f"  WSL detected : {'yes' if _is_wsl() else 'no'}")
    print(f"  terminal     : {_terminal_name()}")
    for name in ("WSL_DISTRO_NAME", "TERM", "TERM_PROGRAM", "WT_SESSION", "TMUX", "SSH_TTY"):
        print(f"  {name:<12}: {os.environ.get(name) or '-'}")
    _report_preferences()
    print()
    print("CodexDeck uses the same BEL byte (0x07) through Textual App.bell().")


def _open_tty() -> tuple[TextIO, bool]:
    try:
        return open("/dev/tty", "w", encoding="utf-8", buffering=1), True
    except OSError:
        return sys.stdout, False


def _emit_pattern(stream: TextIO, delays: tuple[float, ...]) -> None:
    stream.write("\a")
    stream.flush()
    for delay in delays:
        time.sleep(delay)
        stream.write("\a")
        stream.flush()


def _run_raw(*, interactive: bool) -> int:
    stream, should_close = _open_tty()
    try:
        patterns = (
            ("single BEL", ()),
            ("CodexDeck completion pattern: two BELs / 150 ms", (0.15,)),
            ("CodexDeck attention pattern: three BELs / 250 ms", (0.25, 0.25)),
        )
        for label, delays in patterns:
            print(f"\nTesting {label} ...")
            _emit_pattern(stream, delays)
            time.sleep(0.5)
    finally:
        if should_close:
            stream.close()

    if not interactive:
        return 0
    try:
        answer = input("\nDid you hear at least one audible bell? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 2
    if answer in {"y", "yes"}:
        print("PASS: this terminal currently presents BEL audibly.")
        return 0
    print("NO AUDIBLE BEL: the bytes were sent, but the terminal did not present a sound.")
    _print_terminal_hint()
    return 1


def _print_terminal_hint() -> None:
    terminal = _terminal_name()
    if terminal == "VS Code integrated terminal":
        remote_settings = Path.home() / ".vscode-server/data/Machine/settings.json"
        print(f"In VS Code Remote Machine Settings ({remote_settings}), enable:")
        print(
            '  "accessibility.signals.terminalBell": '
            '{"sound": "on", "announcement": "off"}'
        )
        print('  "terminal.integrated.enableVisualBell": true')
        print("The older terminal.integrated.enableBell setting is deprecated.")
    elif terminal == "Windows Terminal":
        print("Check the active Windows Terminal profile's bell/notification sound settings.")
    elif terminal == "tmux":
        print("Check tmux bell forwarding and the outer terminal's audible-bell setting.")
    else:
        print("Check the current terminal emulator's audible-bell or visual-bell setting.")


def _run_textual() -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Static
    except ImportError:
        print("Textual is not installed in this Python environment.", file=sys.stderr)
        print("Run with: uv run python tools/test_wsl_bell.py --textual", file=sys.stderr)
        return 2

    class BellProbeApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Static(
                "Testing Textual App.bell(): completion pattern (two BELs / 150 ms)"
            )

        def on_mount(self) -> None:
            self.set_timer(0.25, self._first_bell)

        def _first_bell(self) -> None:
            self.bell()
            self.set_timer(0.15, self._second_bell)

        def _second_bell(self) -> None:
            self.bell()
            self.set_timer(0.5, self.exit)

    BellProbeApp().run()
    print("Textual probe finished. If it was silent, the terminal is suppressing BEL.")
    _print_terminal_hint()
    return 0


def _write_tone(path: Path) -> None:
    sample_rate = 44_100
    amplitude = 0.22 * 32767
    tone_seconds = 0.10
    gap_seconds = 0.08
    samples: list[int] = []
    for index in range(2):
        samples.extend(
            int(amplitude * math.sin(2 * math.pi * 880 * frame / sample_rate))
            for frame in range(int(tone_seconds * sample_rate))
        )
        if index == 0:
            samples.extend(0 for _ in range(int(gap_seconds * sample_rate)))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def _run_pulse(*, interactive: bool) -> int:
    paplay = shutil.which("paplay")
    if paplay is None:
        print("paplay is missing; install the pulseaudio-utils package.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="codexdeck-bell-") as directory:
        tone = Path(directory) / "completion.wav"
        _write_tone(tone)
        print("Testing WSLg PulseAudio with a generated completion tone ...")
        result = subprocess.run([paplay, str(tone)], check=False)
    if result.returncode != 0:
        print(f"paplay exited with status {result.returncode}.")
        print("Run `pactl info` and confirm that Default Sink is available.")
        return 1
    if not interactive:
        return 0
    try:
        answer = input("Did you hear the generated PulseAudio tone? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 2
    if answer in {"y", "yes"}:
        print("PASS: WSLg audio works; only the terminal BEL presentation is muted.")
        return 0
    print("NO AUDIO: paplay reached the server, but the WSLg/Windows output path was silent.")
    print("Check the Windows volume mixer and the selected Windows output device.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send the same Terminal BEL patterns used by CodexDeck."
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="emit all raw BEL patterns without asking whether they were audible",
    )
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--textual",
        action="store_true",
        help="run a short Textual App.bell() probe instead of the raw probe",
    )
    backend.add_argument(
        "--pulse",
        action="store_true",
        help="play a generated WAV through WSLg PulseAudio instead of Terminal BEL",
    )
    args = parser.parse_args()
    _report()
    if args.textual:
        return _run_textual()
    if args.pulse:
        return _run_pulse(interactive=not args.non_interactive)
    return _run_raw(interactive=not args.non_interactive)


if __name__ == "__main__":
    raise SystemExit(main())

"""Installer-facing configuration for CodexDeck and terminal bell presentation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Mapping


MAX_SETTINGS_BYTES = 2 * 1024 * 1024
TERMINAL_BELL_VALUE = {"sound": "on", "announcement": "off"}


class SoundSetupError(RuntimeError):
    """Raised when an existing user configuration cannot be updated safely."""


@dataclass(frozen=True)
class SoundSetupResult:
    preferences_path: Path
    terminal: str
    vscode_settings_path: Path | None = None
    vscode_configured: bool = False


def _config_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    return Path(configured).expanduser() if configured else _home(environment) / ".config"


def _home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    return Path(configured).expanduser() if configured else Path.home()


def preferences_path(environment: Mapping[str, str] | None = None) -> Path:
    values = environment or os.environ
    return _config_home(values) / "codexdeck" / "preferences.json"


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SoundSetupError(f"configuration path is not a regular file: {path}")
    if path.stat().st_size > MAX_SETTINGS_BYTES:
        raise SoundSetupError(f"configuration file exceeds {MAX_SETTINGS_BYTES} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SoundSetupError(f"configuration is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise SoundSetupError(f"configuration root is not an object: {path}")
    return value


def _atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def enable_codexdeck_sounds(path: Path | None = None) -> Path:
    target = path or preferences_path()
    payload = _read_json_object(target)
    payload.update(
        {
            "sound_enabled": True,
            "attention_sound": True,
            "completion_sound": True,
        }
    )
    _atomic_write(target, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    return target


def _is_wsl() -> bool:
    for path in (Path("/proc/sys/kernel/osrelease"), Path("/proc/version")):
        try:
            if "microsoft" in path.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            continue
    return False


def terminal_kind(environment: Mapping[str, str] | None = None) -> str:
    values = environment or os.environ
    if values.get("TERM_PROGRAM") == "vscode" or values.get("VSCODE_IPC_HOOK_CLI"):
        return "vscode"
    if values.get("WT_SESSION"):
        return "windows-terminal"
    if values.get("TMUX"):
        return "tmux"
    if values.get("SSH_TTY"):
        return "ssh"
    return values.get("TERM_PROGRAM") or values.get("TERM") or "unknown"


def _windows_user_settings(environment: Mapping[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    user = environment.get("USERNAME") or environment.get("USER")
    if _is_wsl() and user:
        base = Path("/mnt/c/Users") / user / "AppData/Roaming"
        candidates.extend(
            (
                base / "Code/User/settings.json",
                base / "Code - Insiders/User/settings.json",
            )
        )
    return tuple(candidates)


def _remote_vscode_settings(environment: Mapping[str, str]) -> tuple[Path, ...]:
    home = _home(environment)
    return (
        home / ".vscode-server/data/Machine/settings.json",
        home / ".vscode-server-insiders/data/Machine/settings.json",
    )


def find_vscode_settings(environment: Mapping[str, str] | None = None) -> Path | None:
    values = environment or os.environ
    override = values.get("CODEXDECK_VSCODE_SETTINGS")
    if override:
        return Path(override).expanduser()
    candidates = list(_remote_vscode_settings(values))
    candidates.extend(_windows_user_settings(values))
    home = _home(values)
    candidates.extend(
        (
            home / ".config/Code/User/settings.json",
            home / ".config/Code - Insiders/User/settings.json",
        )
    )
    existing = next((path for path in candidates if path.is_file() and not path.is_symlink()), None)
    if existing is not None:
        return existing
    return next((path for path in candidates if path.parent.is_dir()), None)


def _jsonc_object_pattern(key: str) -> re.Pattern[str]:
    string = r'"(?:\\.|[^"\\])*"'
    comment = r"//[^\r\n]*|/\*.*?\*/"
    object_body = rf"(?:[^{{}}\"/]+|{string}|{comment})*"
    return re.compile(
        rf'("{re.escape(key)}"\s*:\s*)\{{{object_body}\}}',
        re.DOTALL,
    )


def _jsonc_scalar_pattern(key: str) -> re.Pattern[str]:
    return re.compile(
        rf'("{re.escape(key)}"\s*:\s*)(?:true|false|null|"(?:\\.|[^"\\])*")'
    )


def _replace_or_append_jsonc_setting(
    text: str,
    key: str,
    value: object,
    *,
    object_value: bool,
) -> str:
    rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    pattern = _jsonc_object_pattern(key) if object_value else _jsonc_scalar_pattern(key)
    if pattern.search(text):
        return pattern.sub(lambda match: match.group(1) + rendered, text, count=1)
    if f'"{key}"' in text:
        raise SoundSetupError(f"existing VS Code setting has an unsupported shape: {key}")
    closing = text.rfind("}")
    if closing < 0 or "{" not in text[:closing]:
        raise SoundSetupError("VS Code settings root object was not found")
    before = text[:closing].rstrip()
    separator = "" if before.endswith(("{", ",")) else ","
    insertion = f'{separator}\n    "{key}": {rendered}\n'
    return before + insertion + text[closing:]


def enable_vscode_terminal_bell(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SoundSetupError(f"VS Code settings path is not a regular file: {path}")
    if path.exists() and path.stat().st_size > MAX_SETTINGS_BYTES:
        raise SoundSetupError(f"VS Code settings exceed {MAX_SETTINGS_BYTES} bytes: {path}")
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else "{}\n"
    except (OSError, UnicodeError) as error:
        raise SoundSetupError(f"VS Code settings could not be read: {path}") from error
    if not original.strip():
        original = "{}\n"
    updated = _replace_or_append_jsonc_setting(
        original,
        "accessibility.signals.terminalBell",
        TERMINAL_BELL_VALUE,
        object_value=True,
    )
    updated = _replace_or_append_jsonc_setting(
        updated,
        "terminal.integrated.enableVisualBell",
        True,
        object_value=False,
    )
    if updated != original:
        backup = path.with_name(f"{path.name}.codexdeck-backup")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        _atomic_write(path, updated, mode=mode)
    return path


def configure_sound(
    *,
    environment: Mapping[str, str] | None = None,
    preferences_file: Path | None = None,
    vscode_settings_file: Path | None = None,
) -> SoundSetupResult:
    values = environment or os.environ
    configured_preferences = enable_codexdeck_sounds(preferences_file)
    terminal = terminal_kind(values)
    settings = vscode_settings_file
    if settings is None and terminal == "vscode":
        settings = find_vscode_settings(values)
    configured = False
    if settings is not None:
        enable_vscode_terminal_bell(settings)
        configured = True
    return SoundSetupResult(
        configured_preferences,
        terminal,
        vscode_settings_path=settings,
        vscode_configured=configured,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure CodexDeck terminal sounds.")
    parser.add_argument("--preferences", type=Path)
    parser.add_argument("--vscode-settings", type=Path)
    args = parser.parse_args()
    try:
        result = configure_sound(
            preferences_file=args.preferences,
            vscode_settings_file=args.vscode_settings,
        )
    except SoundSetupError as error:
        print(f"sound setup error: {error}")
        return 1
    print(f"preferences={result.preferences_path}")
    print(f"terminal={result.terminal}")
    print(f"vscode_configured={'yes' if result.vscode_configured else 'no'}")
    if result.vscode_settings_path is not None:
        print(f"vscode_settings={result.vscode_settings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

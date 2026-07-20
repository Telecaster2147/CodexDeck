"""Small cross-layer helpers and injectable operating-system boundaries."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from config import COMMAND_TIMEOUT


class CommandError(RuntimeError):
    pass


class CommandRunner:
    """Run bounded read-only system commands used by the monitor."""

    def run(self, command: Sequence[str], timeout: float = COMMAND_TIMEOUT) -> str:
        try:
            completed = subprocess.run(
                list(command),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"缺少命令：{command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"命令超时：{' '.join(command)}") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or f"exit={exc.returncode}"
            raise CommandError(f"命令执行失败：{' '.join(command)}：{detail}") from exc
        return completed.stdout


def format_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def message_text(item: dict[str, object]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def compact_path(path: str | Path) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + "/"):
        return "~" + value[len(home) :]
    return value


def redact_sensitive(text: str) -> str:
    if not text:
        return text
    redacted = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        text,
    )
    redacted = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password|passwd)"
        r"([\s\"']*[:=][\s\"']*)([^\s\"',;}]+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", redacted)
    return redacted


TRANSCRIPT_BODY_FIELDS = frozenset(
    {
        "aggregated_output",
        "chunks",
        "formatted_output",
        "interaction_input",
        "output",
        "stderr",
        "stdout",
        "transcript",
    }
)


def strip_transcript_bodies(value: Any) -> Any:
    """Return a public/history projection with terminal body fields removed."""

    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "chunks":
                continue
            if name in TRANSCRIPT_BODY_FIELDS:
                projected[name] = ""
            else:
                projected[name] = strip_transcript_bodies(item)
        return projected
    if isinstance(value, list):
        return [strip_transcript_bodies(item) for item in value]
    if isinstance(value, tuple):
        return [strip_transcript_bodies(item) for item in value]
    return value


def one_line(text: str) -> str:
    return " ".join(text.split())

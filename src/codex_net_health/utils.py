"""Small dependency-free helpers shared across application layers."""

from __future__ import annotations


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def message_text(item: dict[str, object]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {"input_text", "text"}:
            continue
        value = part.get("text")
        if isinstance(value, str):
            pieces.append(value)
    return " ".join(pieces).strip()

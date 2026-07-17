"""Read the small subset of Codex configuration used as monitoring evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only.
    import tomli as tomllib


@dataclass(frozen=True)
class CodexConfigSnapshot:
    auto_compact_token_limit: int | None = None
    auto_compact_token_limit_scope: str = ""
    compact_prompt_overridden: bool = False
    source: str = ""
    error: str = ""


class CodexConfigReader:
    """Read presentation-safe values from a Codex home."""

    def read(self, codex_home: Path) -> CodexConfigSnapshot:
        path = codex_home / "config.toml"
        try:
            path.stat()
        except FileNotFoundError:
            return CodexConfigSnapshot()
        except OSError as error:
            return CodexConfigSnapshot(source="config.toml", error=str(error))

        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            result = CodexConfigSnapshot(source="config.toml", error=str(error))
        else:
            raw_limit = payload.get("model_auto_compact_token_limit")
            raw_scope = payload.get("model_auto_compact_token_limit_scope")
            limit = (
                raw_limit
                if isinstance(raw_limit, int) and not isinstance(raw_limit, bool)
                else None
            )
            if limit is not None and limit <= 0:
                limit = None
            result = CodexConfigSnapshot(
                auto_compact_token_limit=limit,
                auto_compact_token_limit_scope=(
                    str(raw_scope) if isinstance(raw_scope, (str, int, float)) else ""
                ),
                compact_prompt_overridden=any(
                    key in payload
                    for key in (
                        "compact_prompt",
                        "model_compact_prompt",
                        "experimental_compact_prompt",
                    )
                ),
                source="config.toml"
                if limit is not None or raw_scope is not None
                else "",
            )
        return result

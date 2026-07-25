"""Tracked upstream compatibility handlers and maintenance signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


LONG_UNOBSERVED_DAYS = 180


@dataclass(frozen=True)
class CompatibilityHandler:
    handler_id: str
    source: str
    fixture: str
    last_observed_version: str
    semantics: str
    diagnostic_only: bool
    deletion_condition: str


COMPATIBILITY_HANDLERS = (
    CompatibilityHandler(
        "event_msg.lifecycle",
        "rollout:event_msg",
        "replay_lifecycle_terminal.jsonl",
        "unversioned-fixture-2026-07-22",
        "lifecycle",
        False,
        "Remove after two supported Codex minor lines emit no covered event_msg lifecycle shape.",
    ),
    CompatibilityHandler(
        "response_item.progress",
        "rollout:response_item",
        "replay_lifecycle_terminal.jsonl",
        "unversioned-fixture-2026-07-22",
        "lifecycle and model progress",
        False,
        "Remove after replacement progress records cover the same lifecycle transitions.",
    ),
    CompatibilityHandler(
        "attention.structured",
        "rollout:request_user_input/approval/elicitation",
        "ground_truth_attention.jsonl",
        "unversioned-fixture-2026-07-24",
        "attention",
        False,
        "Remove only after anonymous fixtures prove an authoritative replacement family.",
    ),
    CompatibilityHandler(
        "compaction.structured",
        "rollout:compaction records",
        "auto_compact_rollout.jsonl",
        "unversioned-fixture-2026-07-24",
        "compaction lifecycle",
        False,
        "Remove after current compaction fixtures pass through the replacement handler.",
    ),
    CompatibilityHandler(
        "tool.structured",
        "rollout:function and custom tool calls",
        "replay_lifecycle_terminal.jsonl",
        "unversioned-fixture-2026-07-22",
        "tool lifecycle and terminal ownership",
        False,
        "Remove after call, output, nested identity, and terminal association fixtures migrate.",
    ),
    CompatibilityHandler(
        "log.compaction",
        "sqlite:structured logs",
        "compact_structured_logs.jsonl",
        "unversioned-fixture-2026-07-24",
        "supporting compaction diagnostics",
        True,
        "Remove when structured rollout evidence fully replaces this supporting signal.",
    ),
    CompatibilityHandler(
        "unknown.conservative",
        "rollout:unknown record family",
        "replay_unknown_phase.jsonl",
        "unversioned-fixture-2026-07-22",
        "diagnostic and confidence degradation",
        True,
        "Keep while unknown families remain possible; never promote without a fixture.",
    ),
)


def compatibility_stats(today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    long_unobserved = 0
    for handler in COMPATIBILITY_HANDLERS:
        observed = date.fromisoformat(handler.last_observed_version[-10:])
        if (today - observed).days > LONG_UNOBSERVED_DAYS:
            long_unobserved += 1
    return {
        "handler_count": len(COMPATIBILITY_HANDLERS),
        "diagnostic_only_count": sum(handler.diagnostic_only for handler in COMPATIBILITY_HANDLERS),
        "long_unobserved_count": long_unobserved,
    }

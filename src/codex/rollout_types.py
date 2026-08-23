"""Mutable reader lifecycle state and immutable rollout read results."""

from __future__ import annotations

from dataclasses import dataclass, field

from models import NormalizedEvent

from .terminal_protocol import TerminalUpdate


@dataclass
class RolloutCursor:
    device: int
    inode: int
    offset: int
    generation: int = 0
    partial: bytes = b""
    anchor: bytes = b""
    saw_turn_boundary: bool = False
    saw_user_input: bool = False
    context_tokens: int | None = None
    context_window: int | None = None
    context_observed_at: float | None = None
    context_source_id: str = ""
    context_turn_id: str = ""
    manual_compact_in_flight: bool = False
    pending_empty_task_at: float | None = None
    pending_empty_task_source_id: str = ""
    pending_empty_task_turn_id: str = ""
    pending_context_tokens: int | None = None
    pending_context_window: int | None = None
    stat_size: int = 0
    mtime_ns: int = 0
    last_growth_at: float | None = None
    last_compact_completion_at: float | None = None
    last_compact_completion_type: str = ""
    skipping_oversize: bool = False
    skipped_bytes: int = 0
    oversize_records: int = 0
    gap_count: int = 0
    gap_reason: str = ""
    gap_hash: str = ""
    backlog_since: float | None = None
    stream_uncertain: bool = False
    stream_uncertainty_count: int = 0
    stream_uncertainty_reason: str = ""


@dataclass
class TerminalMetadataBackfillCursor:
    inode: int
    next_end: int
    floor: int
    process_ids: set[str]
    generation: int = 0
    call_ids: set[str] = field(default_factory=set)
    process_call_ids: dict[str, set[str]] = field(default_factory=dict)
    resolved_process_ids: set[str] = field(default_factory=set)
    pending_updates: dict[str, list[TerminalUpdate]] = field(default_factory=dict)


@dataclass(frozen=True)
class RolloutActivity:
    path: str
    observed_at: float
    available: bool = False
    stat_size: int = 0
    mtime_ns: int = 0
    bytes_read: int = 0
    complete_record_count: int = 0
    record_count: int = 0
    ignored_record_count: int = 0
    normalized_count: int = 0
    partial_bytes: int = 0
    last_growth_at: float | None = None
    replaced: bool = False
    truncated: bool = False
    copy_truncated: bool = False
    consumed_bytes: int = 0
    backlog_bytes: int = 0
    backlog_records_lower_bound: int = 0
    backlog_age_seconds: float | None = None
    budget_exceeded: bool = False
    oversize_record_count: int = 0
    skipped_bytes: int = 0
    gap_count: int = 0
    gap_reason: str = ""
    gap_hash: str = ""
    parse_duration_seconds: float = 0.0
    metadata_backfill_dropped: int = 0
    metadata_backfill_reason: str = ""
    terminal_parser_evictions: int = 0
    terminal_parser_eviction_reason: str = ""
    device: int = 0
    inode: int = 0
    generation: int = 0
    anchor_hash: str = ""
    stream_uncertain: bool = False
    stream_uncertainty_count: int = 0
    stream_uncertainty_reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.bytes_read or self.replaced or self.truncated or self.copy_truncated)


@dataclass(frozen=True)
class RolloutReadResult:
    events: tuple[NormalizedEvent, ...]
    activity: RolloutActivity
    terminal_updates: tuple[TerminalUpdate, ...] = ()

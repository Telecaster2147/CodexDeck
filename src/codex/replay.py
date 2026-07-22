"""Deterministic incremental replay for anonymized Codex protocol fixtures."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from codex.rollout import RolloutReader
from codex.terminal import TerminalStore
from models import (
    InstanceIdentity,
    NetworkEvidence,
    ProcessIdentity,
    ProcessInfo,
    ProtocolCapabilities,
    SessionIdentity,
    TerminalCapability,
)
from state_machine import SessionStateMachine


@dataclass(frozen=True)
class ReplayOperation:
    mode: str
    payload: bytes


@dataclass(frozen=True)
class ReplayTerminalSummary:
    terminal_id: str
    process_id: str
    status: str
    capability: TerminalCapability
    retained_bytes: int
    dropped_bytes: int
    upstream_truncated: bool


@dataclass(frozen=True)
class ProtocolReplaySummary:
    normalized_kinds: tuple[str, ...]
    lifecycle: str
    recovery: str
    attention: str
    terminal_sessions: tuple[ReplayTerminalSummary, ...]
    unknown_events: tuple[tuple[str, int], ...]
    shape_families: tuple[tuple[str, int], ...]
    protocol_capabilities: ProtocolCapabilities
    context_truncated: bool
    copy_truncated: bool
    ignored_records: int


class ProtocolReplayRunner:
    """Replay file operations through the same incremental readers as the engine."""

    def __init__(self, event_lookback: int = 3_600) -> None:
        self.event_lookback = event_lookback

    @staticmethod
    def append_chunks(payload: bytes, chunk_sizes: Iterable[int]) -> tuple[ReplayOperation, ...]:
        operations: list[ReplayOperation] = []
        offset = 0
        for size in chunk_sizes:
            if size <= 0 or offset >= len(payload):
                continue
            operations.append(ReplayOperation("append", payload[offset : offset + size]))
            offset += size
        if offset < len(payload):
            operations.append(ReplayOperation("append", payload[offset:]))
        return tuple(operations)

    def replay_file(
        self,
        fixture: Path,
        *,
        chunk_sizes: Iterable[int] = (),
    ) -> ProtocolReplaySummary:
        payload = fixture.read_bytes()
        operations = self.append_chunks(payload, chunk_sizes)
        if not operations:
            operations = (ReplayOperation("append", payload),)
        return self.replay(operations)

    def replay(self, operations: Iterable[ReplayOperation]) -> ProtocolReplaySummary:
        identity = SessionIdentity(
            InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A")),
            "SESSION_ID",
        )
        process = ProcessInfo(
            ProcessIdentity(42, 100),
            1,
            "codex",
            1,
            0.0,
            "S",
            "futex",
            "codex",
            "session",
            cwd="/workspace-a",
            instance_id=identity.instance.storage_key,
            session_id=identity.session_id,
            instance_identity=identity.instance,
        )
        reader = RolloutReader()
        terminals = TerminalStore()
        machine = SessionStateMachine(self.event_lookback)
        kinds: list[str] = []
        ignored_records = 0
        copy_truncated = False
        latest_timestamp = 0.0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.touch()
            for operation in operations:
                if operation.mode == "append":
                    with path.open("ab") as handle:
                        handle.write(operation.payload)
                elif operation.mode == "replace":
                    path.write_bytes(operation.payload)
                else:
                    raise ValueError(f"unsupported replay operation: {operation.mode}")
                result = reader.read_with_activity(path)
                copy_truncated |= result.activity.copy_truncated
                ignored_records += result.activity.ignored_record_count
                kinds.extend(event.kind for event in result.events)
                latest_timestamp = max(
                    (event.timestamp for event in result.events),
                    default=latest_timestamp,
                )
                terminals.apply(identity, result.terminal_updates)
                machine.ingest(identity, list(result.events))

            state = machine.derive(
                identity,
                process,
                NetworkEvidence(),
                now=latest_timestamp + 1.0 if latest_timestamp else 1.0,
            )
            terminal_summaries = tuple(
                ReplayTerminalSummary(
                    terminal_id=item.terminal_id,
                    process_id=item.process_id,
                    status=item.status,
                    capability=item.capability,
                    retained_bytes=item.retained_bytes,
                    dropped_bytes=item.dropped_bytes,
                    upstream_truncated=item.upstream_truncated,
                )
                for item in terminals.summaries(identity)
            )
            path_key = str(path)
            return ProtocolReplaySummary(
                normalized_kinds=tuple(kinds),
                lifecycle=state.lifecycle.value,
                recovery=state.recovery.value,
                attention=state.attention.value,
                terminal_sessions=terminal_summaries,
                unknown_events=tuple(reader.unknown_counts({path_key}).items()),
                shape_families=tuple(reader.shape_counts({path_key}).items()),
                protocol_capabilities=state.protocol_capabilities,
                context_truncated=reader.has_truncated_context({path_key}),
                copy_truncated=copy_truncated,
                ignored_records=ignored_records,
            )

"""Build bounded, read-only terminal transcripts from Codex evidence."""

from __future__ import annotations

import os
import re
import shlex
from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from models import (
    RUNNING_TERMINAL_STATUSES,
    RolloutIdentity,
    SessionIdentity,
    TerminalAssociationSummary,
    TerminalCapability,
    TerminalChunk,
    TerminalIdentity,
    TerminalSessionSummary,
)

from .terminal_protocol import TerminalProtocolParser as TerminalProtocolParser
from .terminal_protocol import TerminalUpdate as TerminalUpdate
from .terminal_protocol import extract_terminal_updates as extract_terminal_updates
from .terminal_protocol import sanitize_terminal_text as sanitize_terminal_text

MAX_TERMINAL_BYTES = 2 * 1024 * 1024
MAX_TERMINAL_CHUNKS = 4_000
MAX_TERMINALS_PER_SESSION = 16
MAX_GLOBAL_TERMINAL_BYTES = 16 * 1024 * 1024
MAX_TERMINAL_DEDUPE_SCOPES_PER_SESSION = 32
MAX_TERMINAL_SOURCE_IDS_PER_SCOPE = 8_192
MAX_TERMINAL_ALIASES_PER_TERMINAL = 64
TERMINAL_OS_MISS_WINDOWS = 2
TERMINAL_OS_FALLBACK_MIN_AGE_SECONDS = 10.0


@dataclass
class _TerminalSession:
    terminal_id: str
    root_call_id: str = ""
    process_id: str = ""
    turn_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = "unknown"
    exit_code: int | None = None
    capability: TerminalCapability = TerminalCapability.METADATA_ONLY
    started_at: float | None = None
    completed_at: float | None = None
    last_output_at: float | None = None
    dropped_bytes: int = 0
    upstream_truncated: bool = False
    stale: bool = False
    source: str = "rollout"
    last_state_at: float = 0.0
    process_active: bool = False
    os_confirmed: bool = False
    os_match_windows: int = 0
    pending_os_process_id: str = ""
    os_miss_windows: int = 0
    last_os_seen_at: float | None = None
    chunks: deque[TerminalChunk] = field(default_factory=deque)
    retained_bytes: int = 0
    identity: TerminalIdentity | None = None
    association_status: str = "unresolved"
    correlation_source: str = ""
    association_reason: str = "missing_correlation_identity"

    def append(self, update: TerminalUpdate, sequence: int) -> None:
        text = update.output
        if update.cumulative and text and self.chunks:
            current = "".join(chunk.text for chunk in self.chunks if chunk.stream != "system")
            if text == current or current.endswith(text):
                text = ""
            elif text.startswith(current):
                text = text[len(current) :]
            elif current and current in text:
                text = text.split(current, 1)[1]
            elif current:
                text = "\n[final aggregate]\n" + text
        if not text:
            return
        encoded = text.encode("utf-8", errors="replace")
        self.chunks.append(
            TerminalChunk(
                source_id=update.source_id,
                observed_at=update.observed_at,
                stream=update.stream,
                text=text,
                sequence=sequence,
            )
        )
        self.retained_bytes += len(encoded)
        self.last_output_at = update.observed_at
        while self.chunks and (
            len(self.chunks) > MAX_TERMINAL_CHUNKS or self.retained_bytes > MAX_TERMINAL_BYTES
        ):
            removed = self.chunks.popleft()
            size = len(removed.text.encode("utf-8", errors="replace"))
            self.retained_bytes = max(0, self.retained_bytes - size)
            self.dropped_bytes += size

    def summary(self) -> TerminalSessionSummary:
        chunks = tuple(self.chunks)
        if self.dropped_bytes:
            marker = TerminalChunk(
                source_id=f"trim:{self.terminal_id}",
                observed_at=self.last_output_at or self.started_at or 0.0,
                stream="system",
                text=f"[CodexDeck dropped {self.dropped_bytes} earlier bytes]\n",
                sequence=-1,
            )
            chunks = (marker, *chunks)
        return TerminalSessionSummary(
            terminal_id=self.terminal_id,
            root_call_id=self.root_call_id,
            process_id=self.process_id,
            turn_id=self.turn_id,
            command=self.command,
            cwd=self.cwd,
            status=self.status,
            exit_code=self.exit_code,
            capability=self.capability,
            started_at=self.started_at,
            completed_at=self.completed_at,
            last_output_at=self.last_output_at,
            retained_bytes=self.retained_bytes,
            dropped_bytes=self.dropped_bytes,
            upstream_truncated=self.upstream_truncated,
            stale=self.stale,
            process_active=self.process_active,
            source=self.source,
            association_status=self.association_status,
            correlation_source=self.correlation_source,
            association_reason=self.association_reason,
            chunks=chunks,
            identity=self.identity,
        )


class TerminalStore:
    """Correlate terminal updates while keeping output memory bounded."""

    def __init__(self) -> None:
        self.sessions: dict[str | SessionIdentity, dict[str, _TerminalSession]] = defaultdict(dict)
        self.association_conflicts: dict[str | SessionIdentity, int] = defaultdict(int)
        self.association_dropped: dict[str | SessionIdentity, int] = defaultdict(int)
        self.call_ids: dict[str | SessionIdentity, dict[tuple[str | RolloutIdentity, str], str]] = (
            defaultdict(dict)
        )
        self.process_ids: dict[
            str | SessionIdentity, dict[tuple[str | RolloutIdentity, str], str]
        ] = defaultdict(dict)
        self.continuation_call_ids: dict[
            str | SessionIdentity, set[tuple[str | RolloutIdentity, str]]
        ] = defaultdict(set)
        self.wait_call_ids: dict[str | SessionIdentity, set[tuple[str | RolloutIdentity, str]]] = (
            defaultdict(set)
        )
        self.seen_sources: dict[
            str | SessionIdentity,
            OrderedDict[str | RolloutIdentity, OrderedDict[str, None]],
        ] = defaultdict(OrderedDict)
        self.saturated_source_scopes: dict[str | SessionIdentity, set[str | RolloutIdentity]] = (
            defaultdict(set)
        )
        self.private_state_evictions: dict[str | SessionIdentity, int] = defaultdict(int)
        self.private_state_dropped: dict[str | SessionIdentity, int] = defaultdict(int)
        self.private_state_recoveries: dict[str | SessionIdentity, int] = defaultdict(int)
        self.private_state_reasons: dict[str | SessionIdentity, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self.invocations: dict[str | SessionIdentity, int] = defaultdict(int)
        self.sequence = 0

    @staticmethod
    def _correlation_key(
        scope: str | RolloutIdentity, value: str
    ) -> tuple[str | RolloutIdentity, str]:
        return scope, value

    @staticmethod
    def _rank(capability: TerminalCapability) -> int:
        return {
            TerminalCapability.METADATA_ONLY: 0,
            TerminalCapability.FINAL_TRANSCRIPT: 1,
            TerminalCapability.POLL_TRANSCRIPT: 2,
            TerminalCapability.FILE_TAIL: 3,
        }[capability]

    def _record_private_degradation(
        self,
        session_key: str | SessionIdentity,
        reason: str,
        *,
        dropped: bool = False,
        eviction: bool = False,
    ) -> None:
        self.private_state_reasons[session_key][reason] += 1
        if dropped:
            self.private_state_dropped[session_key] += 1
            self.association_dropped[session_key] += 1
        if eviction:
            self.private_state_evictions[session_key] += 1

    def _accept_source(
        self,
        session_key: str | SessionIdentity,
        scope: str | RolloutIdentity,
        source_id: str,
    ) -> bool:
        scopes = self.seen_sources[session_key]
        sources = scopes.get(scope)
        if sources is None:
            if len(scopes) >= MAX_TERMINAL_DEDUPE_SCOPES_PER_SESSION:
                self._record_private_degradation(
                    session_key,
                    "dedupe_scope_limit",
                    dropped=True,
                    eviction=True,
                )
                return False
            sources = OrderedDict()
            scopes[scope] = sources
        scopes.move_to_end(scope)
        if source_id in sources:
            return False
        if scope in self.saturated_source_scopes[session_key]:
            self._record_private_degradation(
                session_key,
                "dedupe_scope_saturated",
                dropped=True,
            )
            return False
        if len(sources) >= MAX_TERMINAL_SOURCE_IDS_PER_SCOPE:
            self.saturated_source_scopes[session_key].add(scope)
            self._record_private_degradation(
                session_key,
                "dedupe_source_limit",
                dropped=True,
                eviction=True,
            )
            return False
        return True

    def _remember_source(
        self,
        session_key: str | SessionIdentity,
        scope: str | RolloutIdentity,
        source_id: str,
    ) -> None:
        sources = self.seen_sources[session_key].get(scope)
        if sources is not None:
            sources[source_id] = None

    def _set_correlation(
        self,
        session_key: str | SessionIdentity,
        mapping: dict[tuple[str | RolloutIdentity, str], str],
        key: tuple[str | RolloutIdentity, str],
        terminal_id: str,
        reason: str,
    ) -> bool:
        if key in mapping:
            mapping[key] = terminal_id
            return True
        aliases = sum(value == terminal_id for value in mapping.values())
        if aliases >= MAX_TERMINAL_ALIASES_PER_TERMINAL:
            self._record_private_degradation(
                session_key,
                reason,
                dropped=True,
                eviction=True,
            )
            return False
        mapping[key] = terminal_id
        return True

    def apply(
        self,
        session_key: str | SessionIdentity,
        updates: tuple[TerminalUpdate, ...],
    ) -> bool:
        changed = False
        for update in updates:
            dropped_before = self.private_state_dropped.get(session_key, 0)
            if not self._accept_source(session_key, update.scope, update.source_id):
                changed |= self.private_state_dropped.get(session_key, 0) > dropped_before
                continue
            terminal_id = ""
            process_terminal = ""
            call_terminal = ""
            if update.process_id:
                process_terminal = self.process_ids[session_key].get(
                    self._correlation_key(update.scope, update.process_id), ""
                )
            if update.call_id:
                call_terminal = self.call_ids[session_key].get(
                    self._correlation_key(update.scope, update.call_id), ""
                )
            if process_terminal and call_terminal and process_terminal != call_terminal:
                self.association_conflicts[session_key] += 1
                self.association_dropped[session_key] += 1
                self._remember_source(session_key, update.scope, update.source_id)
                changed = True
                continue
            terminal_id = process_terminal or call_terminal
            if not terminal_id and not update.terminal_candidate:
                continue
            if not terminal_id:
                base_terminal_id = update.process_id or update.call_id or update.source_id
                self.invocations[session_key] += 1
                terminal_id = base_terminal_id
                if terminal_id in self.sessions[session_key]:
                    terminal_id = f"{base_terminal_id}:{self.invocations[session_key]}"
                association_status, correlation_source, association_reason = self._association_for(
                    update
                )
                self.sessions[session_key][terminal_id] = _TerminalSession(
                    terminal_id=terminal_id,
                    root_call_id=update.call_id,
                    process_id=update.process_id,
                    turn_id=update.turn_id,
                    command=update.command,
                    cwd=update.cwd,
                    status=update.status,
                    capability=update.capability,
                    started_at=update.observed_at,
                    source=update.source,
                    last_state_at=update.observed_at,
                    identity=(
                        TerminalIdentity(
                            session_key,
                            update.process_id,
                            update.call_id,
                            self.invocations[session_key],
                        )
                        if isinstance(session_key, SessionIdentity)
                        else None
                    ),
                    association_status=association_status,
                    correlation_source=correlation_source,
                    association_reason=association_reason,
                )
            terminal = self.sessions[session_key][terminal_id]
            association_status, correlation_source, association_reason = self._association_for(
                update
            )
            association_rank = {
                "unresolved": 0,
                "ambiguous": 1,
                "confirmed": 2,
                "conflicting": 3,
            }
            if association_rank[association_status] > association_rank[terminal.association_status]:
                terminal.association_status = association_status
                terminal.correlation_source = correlation_source
                terminal.association_reason = association_reason
            if update.call_id:
                call_key = self._correlation_key(update.scope, update.call_id)
                call_remembered = self._set_correlation(
                    session_key,
                    self.call_ids[session_key],
                    call_key,
                    terminal_id,
                    "call_alias_limit",
                )
                if call_remembered and update.continuation:
                    self.continuation_call_ids[session_key].add(call_key)
                if call_remembered and update.wait_for_completion:
                    self.wait_call_ids[session_key].add(call_key)
            if update.process_id:
                self._set_correlation(
                    session_key,
                    self.process_ids[session_key],
                    self._correlation_key(update.scope, update.process_id),
                    terminal_id,
                    "process_alias_limit",
                )
                terminal.process_id = terminal.process_id or update.process_id
            terminal.root_call_id = terminal.root_call_id or update.call_id
            terminal.turn_id = terminal.turn_id or update.turn_id
            terminal.command = terminal.command or update.command
            terminal.cwd = terminal.cwd or update.cwd
            terminal.upstream_truncated |= update.upstream_truncated
            if self._rank(update.capability) > self._rank(terminal.capability):
                terminal.capability = update.capability
            if update.observed_at >= terminal.last_state_at:
                terminal.last_state_at = update.observed_at
                status = update.status
                if (
                    self._correlation_key(update.scope, update.call_id)
                    in self.continuation_call_ids[session_key]
                    and update.exit_code is None
                    and status in {"completed", "complete", "success"}
                    and (
                        update.continuation
                        or self._correlation_key(update.scope, update.call_id)
                        not in self.wait_call_ids[session_key]
                    )
                ):
                    status = "running"
                if status and status != "unknown":
                    terminal.status = status
                if update.exit_code is not None:
                    terminal.exit_code = update.exit_code
                if terminal.status in {"completed", "failed", "declined", "error", "errored"}:
                    terminal.completed_at = update.observed_at
                    terminal.process_active = False
                elif terminal.status in RUNNING_TERMINAL_STATUSES and update.source == "file-tail":
                    terminal.process_active = True
            self.sequence += 1
            terminal.append(update, self.sequence)
            self._remember_source(session_key, update.scope, update.source_id)
            changed = True
        self._trim_sessions(session_key)
        self._prune_indices(session_key)
        self._trim_global()
        return changed

    @staticmethod
    def _association_for(update: TerminalUpdate) -> tuple[str, str, str]:
        if update.source == "file-tail" and update.process_id and update.scope:
            return "confirmed", "file_identity", "pid_start_device_inode"
        if update.source == "process" and update.process_id:
            return "confirmed", "os_metadata", "pid_and_kernel_start_time"
        if update.process_id:
            return "confirmed", "process_id", "protocol_process_id"
        if update.call_id and update.scope:
            return "confirmed", "rollout_scoped_call_id", "call_id_with_rollout_generation"
        if update.call_id:
            return "ambiguous", "call_id", "call_id_without_rollout_generation"
        return "unresolved", "invocation", "missing_process_and_call_id"

    def association_summary(
        self,
        session_key: str | SessionIdentity,
        *,
        labeled_correct: int = 0,
        labeled_incorrect: int = 0,
    ) -> TerminalAssociationSummary:
        terminals = tuple(self.sessions.get(session_key, {}).values())
        counts = {
            status: sum(item.association_status == status for item in terminals)
            for status in ("confirmed", "ambiguous", "conflicting", "unresolved")
        }
        conflict_count = self.association_conflicts.get(session_key, 0)
        dropped = self.association_dropped.get(session_key, 0)
        counts["conflicting"] += conflict_count
        reasons: dict[str, int] = defaultdict(int)
        for item in terminals:
            reasons[item.association_reason] += 1
        if conflict_count:
            reasons["process_call_identity_conflict"] += conflict_count
        eligible = len(terminals) + conflict_count
        associated = counts["confirmed"] + counts["ambiguous"]
        labeled = labeled_correct + labeled_incorrect
        private_state = self.private_state_summary(session_key)
        return TerminalAssociationSummary(
            eligible_operations=eligible,
            associated_operations=associated,
            confirmed=counts["confirmed"],
            ambiguous=counts["ambiguous"],
            conflicting=counts["conflicting"],
            unresolved=counts["unresolved"],
            dropped=dropped,
            reasons=tuple(sorted(reasons.items())),
            labeled_correct=labeled_correct,
            labeled_incorrect=labeled_incorrect,
            association_coverage=associated / eligible if eligible else None,
            unresolved_rate=(counts["unresolved"] + counts["conflicting"]) / eligible
            if eligible
            else None,
            precision=labeled_correct / labeled if labeled else None,
            private_state_entries=private_state["entries"],
            private_state_estimated_bytes=private_state["estimated_bytes"],
            private_state_evictions=self.private_state_evictions.get(session_key, 0),
            private_state_dropped=self.private_state_dropped.get(session_key, 0),
            private_state_recoveries=self.private_state_recoveries.get(session_key, 0),
            private_state_reasons=tuple(
                sorted(self.private_state_reasons.get(session_key, {}).items())
            ),
        )

    def private_state_summary(self, session_key: str | SessionIdentity) -> dict[str, int]:
        scopes: Mapping[str | RolloutIdentity, OrderedDict[str, None]] = self.seen_sources.get(
            session_key, {}
        )
        source_entries = sum(len(values) for values in scopes.values())
        call_entries = len(self.call_ids.get(session_key, {}))
        process_entries = len(self.process_ids.get(session_key, {}))
        continuation_entries = len(self.continuation_call_ids.get(session_key, set()))
        wait_entries = len(self.wait_call_ids.get(session_key, set()))
        entries = (
            len(scopes)
            + source_entries
            + call_entries
            + process_entries
            + continuation_entries
            + wait_entries
        )
        string_bytes = sum(
            len(source_id.encode("utf-8", errors="replace"))
            for values in scopes.values()
            for source_id in values
        )
        string_bytes += sum(
            len(str(value).encode("utf-8", errors="replace"))
            for mapping in (
                self.call_ids.get(session_key, {}),
                self.process_ids.get(session_key, {}),
            )
            for key in mapping
            for value in key
        )
        return {
            "entries": entries,
            "estimated_bytes": string_bytes + entries * 72,
            "source_entries": source_entries,
            "call_entries": call_entries,
            "process_entries": process_entries,
            "continuation_entries": continuation_entries,
            "wait_entries": wait_entries,
            "scope_entries": len(scopes),
            "saturated_scopes": len(self.saturated_source_scopes.get(session_key, set())),
        }

    def _merge_terminal(
        self,
        session_key: str | SessionIdentity,
        target_id: str,
        source_id: str,
    ) -> bool:
        if not target_id or target_id == source_id:
            return False
        terminals = self.sessions.get(session_key, {})
        target = terminals.get(target_id)
        source = terminals.get(source_id)
        if target is None or source is None:
            return False
        target.capability = max(
            (target.capability, source.capability),
            key=self._rank,
        )
        target.upstream_truncated |= source.upstream_truncated
        association_rank = {"unresolved": 0, "ambiguous": 1, "confirmed": 2, "conflicting": 3}
        if (
            association_rank[source.association_status]
            > association_rank[target.association_status]
        ):
            target.association_status = source.association_status
            target.correlation_source = source.correlation_source
            target.association_reason = source.association_reason
        target.dropped_bytes += source.dropped_bytes
        target.last_output_at = max(
            (
                value
                for value in (target.last_output_at, source.last_output_at)
                if value is not None
            ),
            default=None,
        )
        target.chunks = deque(
            sorted(
                (*target.chunks, *source.chunks),
                key=lambda chunk: (chunk.observed_at, chunk.sequence),
            )
        )
        target.retained_bytes = sum(
            len(chunk.text.encode("utf-8", errors="replace")) for chunk in target.chunks
        )
        while target.chunks and (
            len(target.chunks) > MAX_TERMINAL_CHUNKS or target.retained_bytes > MAX_TERMINAL_BYTES
        ):
            removed = target.chunks.popleft()
            size = len(removed.text.encode("utf-8", errors="replace"))
            target.retained_bytes = max(0, target.retained_bytes - size)
            target.dropped_bytes += size
        terminals.pop(source_id, None)
        for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
            for key, value in list(mapping.items()):
                if value == source_id:
                    mapping[key] = target_id
        return True

    def _prune_indices(self, session_key: str | SessionIdentity) -> None:
        retained = set(self.sessions.get(session_key, {}))
        for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
            for key, terminal_id in list(mapping.items()):
                if terminal_id not in retained:
                    mapping.pop(key, None)
        call_keys = set(self.call_ids[session_key])
        self.continuation_call_ids[session_key].intersection_update(call_keys)
        self.wait_call_ids[session_key].intersection_update(call_keys)

    def _trim_sessions(self, session_key: str | SessionIdentity) -> None:
        values = self.sessions.get(session_key, {})
        if len(values) <= MAX_TERMINALS_PER_SESSION:
            return

        def retention_priority(item: _TerminalSession) -> int:
            running = item.status in RUNNING_TERMINAL_STATUSES
            unconfirmed_metadata = (
                running
                and item.capability == TerminalCapability.METADATA_ONLY
                and not item.process_id
                and not item.os_confirmed
                and not item.process_active
                and item.last_output_at is None
            )
            if unconfirmed_metadata:
                return 0
            return 2 if running else 1

        ordered = sorted(
            values.values(),
            key=lambda item: (
                retention_priority(item),
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        for terminal in ordered[: len(values) - MAX_TERMINALS_PER_SESSION]:
            values.pop(terminal.terminal_id, None)
            self.association_dropped[session_key] += 1
            for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
                for key, value in list(mapping.items()):
                    if value == terminal.terminal_id:
                        mapping.pop(key, None)

    def summaries(self, session_key: str | SessionIdentity) -> list[TerminalSessionSummary]:
        return [
            terminal.summary()
            for terminal in sorted(
                self.sessions.get(session_key, {}).values(),
                key=lambda item: item.started_at or 0.0,
            )
        ]

    def current_summaries(self, session_key: str | SessionIdentity) -> list[TerminalSessionSummary]:
        """Publish background tasks known active from protocol or current OS evidence."""

        return [
            summary
            for summary in self.summaries(session_key)
            if summary.status in RUNNING_TERMINAL_STATUSES
            and bool(summary.process_id)
            and summary.process_active
            and not summary.stale
            and (
                summary.source == "file-tail"
                or self.sessions[session_key][summary.terminal_id].os_confirmed
            )
        ]

    @staticmethod
    def _command_matches_child(
        command: str,
        child_command: str,
        *,
        exact_only: bool = False,
    ) -> bool:
        if not command or not child_command:
            return False

        normalized_command = " ".join(command.split())
        normalized_child = " ".join(child_command.split())
        if normalized_command in normalized_child:
            return True
        if exact_only:
            return False

        def tokens(value: str) -> list[str]:
            try:
                values = shlex.split(value)
            except ValueError:
                values = value.split()
            return [Path(token).name.lower() for token in values if token]

        def executable(value: str) -> str:
            return re.sub(r"[\d.]+$", "", Path(value).name.lower())

        def shell_script_tokens(value: str) -> list[str]:
            try:
                values = shlex.split(value)
            except ValueError:
                values = value.split()
            for index, token in enumerate(values[:-1]):
                if Path(token).name.lower() not in {"sh", "bash", "dash", "zsh"}:
                    continue
                if values[index + 1] != "-c" or index + 2 >= len(values):
                    continue
                script = " ".join(values[index + 2 :]).casefold()
                return re.findall(r"[a-z0-9_./:=+-]+", script)
            return []

        command_script = shell_script_tokens(command)
        child_script = shell_script_tokens(child_command)
        if len(command_script) >= 2 and child_script:
            child_token_set = set(child_script)
            if all(token in child_token_set for token in command_script):
                return True

        command_tokens = tokens(command)
        child_tokens = tokens(child_command)
        if not command_tokens or not child_tokens:
            return False
        if executable(command_tokens[0]) != executable(child_tokens[0]):
            return False
        if len(child_tokens) == 1:
            return True
        required = [
            token for token in command_tokens[1:] if not token.startswith("-") and "=" not in token
        ]
        return bool(required) and all(token in child_tokens for token in required)

    @staticmethod
    def _os_process_id(child: object) -> str:
        identity = getattr(child, "identity", None)
        pid = getattr(identity, "pid", None)
        start_time = getattr(identity, "start_time", None)
        if not isinstance(pid, int) or not isinstance(start_time, int):
            return ""
        return f"os:{pid}:{start_time}"

    @staticmethod
    def _os_pid(process_id: str) -> int | None:
        if not process_id.startswith("os:"):
            return None
        try:
            return int(process_id.split(":", 2)[1])
        except (IndexError, ValueError):
            return None

    @staticmethod
    def _descendant_pids(children: tuple[object, ...], root_pid: int) -> set[int]:
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for child in children:
                identity = getattr(child, "identity", None)
                pid = getattr(identity, "pid", None)
                parent_pid = getattr(child, "parent_pid", None)
                if (
                    isinstance(pid, int)
                    and isinstance(parent_pid, int)
                    and parent_pid in descendants
                    and pid not in descendants
                ):
                    descendants.add(pid)
                    changed = True
        return descendants

    @classmethod
    def _observer_process_ids(cls, children: tuple[object, ...]) -> set[int]:
        child_by_pid = {
            pid: child
            for child in children
            if isinstance(
                pid := getattr(getattr(child, "identity", None), "pid", None),
                int,
            )
        }
        current_pid = os.getpid()
        if current_pid not in child_by_pid:
            return set()
        root_pid = current_pid
        seen: set[int] = set()
        while root_pid not in seen:
            seen.add(root_pid)
            parent_pid = getattr(child_by_pid[root_pid], "parent_pid", None)
            if not isinstance(parent_pid, int) or parent_pid not in child_by_pid:
                break
            root_pid = parent_pid
        return cls._descendant_pids(children, root_pid)

    @staticmethod
    def _top_level_child_pid(child_by_pid: dict[int, object], pid: int) -> int:
        current = pid
        seen: set[int] = set()
        while current not in seen:
            seen.add(current)
            parent = getattr(child_by_pid.get(current), "parent_pid", None)
            if not isinstance(parent, int) or parent not in child_by_pid:
                break
            current = parent
        return current

    @staticmethod
    def _command_executable(command: str) -> str:
        try:
            values = shlex.split(command)
        except ValueError:
            values = command.split()
        return Path(values[0]).name.casefold() if values else ""

    @classmethod
    def _is_internal_job_root(cls, child: object) -> bool:
        executable = cls._command_executable(str(getattr(child, "command", "") or ""))
        return executable in {"codex", "codex-code-mode-host"}

    @classmethod
    def _is_observable_job_root(cls, child: object) -> bool:
        executable = cls._command_executable(str(getattr(child, "command", "") or ""))
        return executable in {
            "bash",
            "bwrap",
            "codex-linux-sandbox",
            "dash",
            "sh",
            "time",
            "timeout",
            "zsh",
        }

    @classmethod
    def _representative_job_command(
        cls,
        children: tuple[object, ...],
        root_pid: int,
    ) -> str:
        child_by_pid = {
            pid: child
            for child in children
            if isinstance(
                pid := getattr(getattr(child, "identity", None), "pid", None),
                int,
            )
        }
        descendants = cls._descendant_pids(children, root_pid)
        wrappers = {
            "bash",
            "bwrap",
            "codex-linux-sandbox",
            "dash",
            "env",
            "sh",
            "time",
            "timeout",
            "zsh",
        }

        def depth(pid: int) -> int:
            value = 0
            current = pid
            seen: set[int] = set()
            while current not in seen:
                seen.add(current)
                parent = getattr(child_by_pid.get(current), "parent_pid", None)
                if not isinstance(parent, int) or parent not in descendants:
                    break
                value += 1
                current = parent
            return value

        ranked: list[tuple[int, int, str]] = []
        for pid in descendants:
            child = child_by_pid.get(pid)
            if child is None:
                continue
            command = str(getattr(child, "command", "") or "")
            if command:
                ranked.append(
                    (
                        int(cls._command_executable(command) not in wrappers),
                        depth(pid),
                        command,
                    )
                )
        return max(ranked, key=lambda item: (item[0], item[1]))[2] if ranked else ""

    def _reconcile_os_jobs(
        self,
        session_key: str | SessionIdentity,
        children: tuple[object, ...],
        live_children: list[object],
        claimed_job_roots: set[int],
        observed_at: float,
        workspace: str,
    ) -> bool:
        child_by_pid = {
            pid: child
            for child in live_children
            if isinstance(
                pid := getattr(getattr(child, "identity", None), "pid", None),
                int,
            )
        }
        roots = [
            child
            for pid, child in child_by_pid.items()
            if getattr(child, "parent_pid", None) not in child_by_pid
            and pid not in claimed_job_roots
            and not self._is_internal_job_root(child)
            and self._is_observable_job_root(child)
            and float(getattr(child, "elapsed_seconds", 0.0) or 0.0)
            >= TERMINAL_OS_FALLBACK_MIN_AGE_SECONDS
        ]
        live_process_ids = {self._os_process_id(child) for child in roots}
        changed = False

        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.source != "process" or not terminal.process_id.startswith("os:"):
                continue
            active = terminal.process_id in live_process_ids
            if terminal.process_active != active:
                terminal.process_active = active
                changed = True
            if not active and terminal.status in RUNNING_TERMINAL_STATUSES:
                terminal.status = "completed"
                terminal.completed_at = observed_at
                terminal.last_state_at = observed_at
                changed = True

        for root in roots:
            root_pid = getattr(getattr(root, "identity", None), "pid", None)
            if not isinstance(root_pid, int):
                continue
            process_id = self._os_process_id(root)
            if not process_id:
                continue
            existing_id = self.process_ids[session_key].get(
                self._correlation_key("", process_id), ""
            )
            command = self._representative_job_command(children, root_pid)
            if not existing_id:
                changed |= self.apply(
                    session_key,
                    (
                        TerminalUpdate(
                            source_id=f"os-child:{process_id}",
                            observed_at=observed_at,
                            process_id=process_id,
                            command=command,
                            cwd=workspace,
                            status="running",
                            capability=TerminalCapability.METADATA_ONLY,
                            terminal_candidate=True,
                            source="process",
                        ),
                    ),
                )
                existing_id = self.process_ids[session_key].get(
                    self._correlation_key("", process_id), ""
                )
            reconciled_terminal = self.sessions.get(session_key, {}).get(existing_id)
            if reconciled_terminal is None:
                continue
            reconciled_terminal.command = command or reconciled_terminal.command
            reconciled_terminal.cwd = reconciled_terminal.cwd or workspace
            reconciled_terminal.status = "running"
            reconciled_terminal.completed_at = None
            reconciled_terminal.process_active = True
            reconciled_terminal.os_confirmed = True
            reconciled_terminal.last_os_seen_at = observed_at
            reconciled_terminal.last_state_at = observed_at
        return changed

    def reconcile_children(
        self,
        session_key: str | SessionIdentity,
        children: tuple[object, ...],
        observed_at: float,
        *,
        evidence_cutoff: float | None = None,
        workspace: str = "",
    ) -> bool:
        """Close previously confirmed rollout terminals after stable OS absence."""

        observer_process_ids = self._observer_process_ids(children)
        all_live_children = [
            child
            for child in children
            if str(getattr(child, "state", "")).upper() != "Z"
            and getattr(getattr(child, "identity", None), "pid", None) not in observer_process_ids
        ]
        child_by_pid = {
            pid: child
            for child in all_live_children
            if isinstance(
                pid := getattr(getattr(child, "identity", None), "pid", None),
                int,
            )
        }
        candidates = [
            terminal
            for terminal in self.sessions.get(session_key, {}).values()
            if terminal.source == "rollout"
            and terminal.command
            and not terminal.stale
            and (
                (bool(terminal.process_id) and terminal.status in RUNNING_TERMINAL_STATUSES)
                or (not terminal.process_id and terminal.completed_at is not None)
            )
            and (evidence_cutoff is None or terminal.last_state_at <= evidence_cutoff)
        ]
        for terminal in candidates:
            if terminal.os_confirmed:
                terminal.process_active = False
        matched_terminal_ids: set[str] = set()
        claimed_job_roots: set[int] = set()
        changed = False
        for terminal in reversed(candidates):
            exact_only = not terminal.process_id or terminal.process_id.startswith("os:")
            matching_children = [
                child
                for child in all_live_children
                if self._command_matches_child(
                    terminal.command,
                    str(getattr(child, "command", "") or ""),
                    exact_only=exact_only,
                )
            ]
            matching_pids = {
                getattr(getattr(child, "identity", None), "pid", None)
                for child in matching_children
            }
            job_roots = [
                child
                for child in matching_children
                if getattr(child, "parent_pid", 0) not in matching_pids
            ]
            for child in job_roots:
                child_pid = getattr(getattr(child, "identity", None), "pid", None)
                if not isinstance(child_pid, int) or child_pid in claimed_job_roots:
                    continue
                if self._command_matches_child(
                    terminal.command,
                    str(getattr(child, "command", "") or ""),
                    exact_only=exact_only,
                ):
                    os_process_id = self._os_process_id(child)
                    existing = self.process_ids[session_key].get(
                        self._correlation_key("", os_process_id), ""
                    )
                    if existing and existing != terminal.terminal_id:
                        existing_terminal = self.sessions[session_key].get(existing)
                        if existing_terminal is None:
                            continue
                        if existing_terminal.source == "file-tail":
                            changed |= self._merge_terminal(
                                session_key,
                                terminal.terminal_id,
                                existing,
                            )
                        elif existing_terminal.last_state_at >= terminal.last_state_at:
                            continue
                        else:
                            existing_terminal.process_active = False
                            existing_terminal.os_confirmed = False
                    matched_terminal_ids.add(terminal.terminal_id)
                    claimed_job_roots.add(self._top_level_child_pid(child_by_pid, child_pid))
                    protocol_confirmed = bool(
                        terminal.process_id and not terminal.process_id.startswith("os:")
                    )
                    if protocol_confirmed:
                        terminal.os_match_windows = TERMINAL_OS_MISS_WINDOWS
                    elif terminal.pending_os_process_id == os_process_id:
                        terminal.os_match_windows += 1
                    else:
                        terminal.pending_os_process_id = os_process_id
                        terminal.os_match_windows = 1
                    if terminal.os_match_windows >= TERMINAL_OS_MISS_WINDOWS:
                        if os_process_id:
                            self._set_correlation(
                                session_key,
                                self.process_ids[session_key],
                                self._correlation_key("", os_process_id),
                                terminal.terminal_id,
                                "process_alias_limit",
                            )
                        if not terminal.process_id and os_process_id:
                            terminal.process_id = os_process_id
                            changed = True
                        if terminal.status not in RUNNING_TERMINAL_STATUSES:
                            terminal.status = "running"
                            terminal.completed_at = None
                            terminal.exit_code = None
                            changed = True
                        terminal.os_confirmed = True
                        terminal.process_active = True
                    terminal.os_miss_windows = 0
                    terminal.last_os_seen_at = observed_at
                    descendant_pids = self._descendant_pids(
                        tuple(all_live_children),
                        child_pid,
                    )
                    for file_terminal in list(self.sessions[session_key].values()):
                        if file_terminal.source != "file-tail":
                            continue
                        file_pid = self._os_pid(file_terminal.process_id)
                        if file_pid not in descendant_pids:
                            continue
                        changed |= self._merge_terminal(
                            session_key,
                            terminal.terminal_id,
                            file_terminal.terminal_id,
                        )
                    break
        unmatched = [
            terminal for terminal in candidates if terminal.terminal_id not in matched_terminal_ids
        ]
        for terminal in unmatched:
            if not terminal.os_confirmed:
                terminal.pending_os_process_id = ""
                terminal.os_match_windows = 0
                continue
            terminal.os_miss_windows += 1
            if terminal.os_miss_windows < TERMINAL_OS_MISS_WINDOWS:
                continue
            terminal.status = "completed"
            terminal.completed_at = observed_at
            terminal.last_state_at = max(terminal.last_state_at, observed_at)
            changed = True
        changed |= self._reconcile_os_jobs(
            session_key,
            children,
            all_live_children,
            claimed_job_roots,
            observed_at,
            workspace,
        )
        self._trim_sessions(session_key)
        self._prune_indices(session_key)
        self._trim_global()
        return changed

    def prune_scopes(
        self,
        active_scopes: set[str | RolloutIdentity],
    ) -> None:
        active = set(active_scopes)
        active.add("")
        for session_key, scopes in self.seen_sources.items():
            removed = {scope for scope in scopes if scope not in active}
            if not removed:
                continue
            for scope in removed:
                scopes.pop(scope, None)
            saturated = self.saturated_source_scopes[session_key]
            recovered = len(saturated & removed)
            saturated.difference_update(removed)
            if recovered:
                self.private_state_recoveries[session_key] += recovered
                self.private_state_reasons[session_key]["dedupe_scope_recovered"] += recovered
            for mapping in (self.call_ids[session_key], self.process_ids[session_key]):
                for key in [key for key in mapping if key[0] in removed]:
                    mapping.pop(key, None)
            self._prune_indices(session_key)

    def _trim_global(self) -> None:
        terminals = [terminal for values in self.sessions.values() for terminal in values.values()]
        total = sum(terminal.retained_bytes for terminal in terminals)
        if total <= MAX_GLOBAL_TERMINAL_BYTES:
            return
        ordered = sorted(
            terminals,
            key=lambda item: (
                item.status in RUNNING_TERMINAL_STATUSES,
                item.last_output_at or item.completed_at or item.started_at or 0.0,
            ),
        )
        while total > MAX_GLOBAL_TERMINAL_BYTES and ordered:
            progressed = False
            for terminal in ordered:
                if not terminal.chunks:
                    continue
                removed = terminal.chunks.popleft()
                size = len(removed.text.encode("utf-8", errors="replace"))
                terminal.retained_bytes = max(0, terminal.retained_bytes - size)
                terminal.dropped_bytes += size
                total -= size
                progressed = True
                if total <= MAX_GLOBAL_TERMINAL_BYTES:
                    break
            if not progressed:
                break

    def mark_stale(self, session_key: str | SessionIdentity) -> None:
        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.status in RUNNING_TERMINAL_STATUSES:
                terminal.stale = True
                terminal.process_active = False

    def mark_process_unavailable(self, session_key: str | SessionIdentity) -> None:
        """Hide live terminals when the current process tree cannot be verified."""

        for terminal in self.sessions.get(session_key, {}).values():
            if terminal.status in RUNNING_TERMINAL_STATUSES:
                terminal.process_active = False

    def prune(self, retained_session_keys: set[str | SessionIdentity]) -> None:
        for session_key in set(self.sessions) - retained_session_keys:
            self.sessions.pop(session_key, None)
            self.call_ids.pop(session_key, None)
            self.process_ids.pop(session_key, None)
            self.continuation_call_ids.pop(session_key, None)
            self.wait_call_ids.pop(session_key, None)
            self.seen_sources.pop(session_key, None)
            self.saturated_source_scopes.pop(session_key, None)
            self.invocations.pop(session_key, None)
            self.association_conflicts.pop(session_key, None)
            self.association_dropped.pop(session_key, None)
            self.private_state_evictions.pop(session_key, None)
            self.private_state_dropped.pop(session_key, None)
            self.private_state_recoveries.pop(session_key, None)
            self.private_state_reasons.pop(session_key, None)

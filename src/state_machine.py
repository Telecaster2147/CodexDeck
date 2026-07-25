"""Derive recovery-aware session health from normalized Codex events."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from typing import Any

from config import (
    ALERT_HTTP_RESPONSE,
    ALERT_KEEPALIVE_ONLY,
    ALERT_POST_TOOL,
    ALERT_PRE_REQUEST,
    ALERT_THRESHOLDS,
    EVENT_LABELS,
    LIFECYCLE_LABELS,
    MAX_EVENTS_PER_SESSION,
)
from models import (
    AgentNode,
    AxisCompleteness,
    AttentionRequest,
    AttentionState,
    AlertOccurrence,
    AlertStatus,
    AlertTransition,
    CapabilityMode,
    CapabilityStatus,
    ClockAssessment,
    Confidence,
    CompactionEvidence,
    CompactionSummary,
    CurrentOperationSummary,
    DiagnosisFinding,
    EvidenceCoverage,
    EventTelemetrySummary,
    FailureInfo,
    LifecycleState,
    NetworkEvidence,
    NormalizedEvent,
    ObservationPulse,
    ProcessInfo,
    ProtocolCapabilities,
    Provenance,
    RateLimitSummary,
    RateLimitWindow,
    RecoveryState,
    SessionIdentity,
    SessionHealth,
    SessionCompleteness,
    SilenceAssessment,
    SilenceState,
    TokenUsageSummary,
    ToolExecutionSummary,
    TurnSummary,
)


PROGRESS_KINDS = {
    "RESPONSE_STARTED",
    "MODEL_PROGRESS",
    "REASONING_SUMMARY",
    "PLAN_UPDATED",
    "TOOL_RUNNING",
    "TOOL_COMPLETED",
    "FILE_CHANGE_APPLIED",
    "FILE_CHANGE_FAILED",
    "COMPACTING",
    "COMPACT_COMPLETED",
    "TURN_COMPLETED",
    "TURN_FAILED",
    "TURN_ABORTED",
}
TERMINAL_KINDS = {"TURN_COMPLETED", "TURN_FAILED", "TURN_ABORTED"}
ACTIVE_TURN_KINDS = {
    "COMPACT_REQUESTED",
    "REQUEST_SENT",
    "RESPONSE_STARTED",
    "MODEL_PROGRESS",
    "REASONING_SUMMARY",
    "PLAN_UPDATED",
    "TOOL_RUNNING",
    "TOOL_COMPLETED",
    "FILE_CHANGE_APPLIED",
    "FILE_CHANGE_FAILED",
    "COMPACTING",
    "COMPACT_COMPLETED",
}
CURRENT_TURN_KINDS = ACTIVE_TURN_KINDS | {"ACTION_REQUIRED"}
LIFECYCLE_PHASE_KINDS = ACTIVE_TURN_KINDS | TERMINAL_KINDS | {"TURN_STARTED"}
DISPLAY_PHASE_KINDS = LIFECYCLE_PHASE_KINDS | {
    "RECONNECTING",
    "TRANSPORT_FALLBACK",
    "RECOVERED",
    "PROCESS_RESUMED",
}
ATTENTION_CLEAR_KINDS = PROGRESS_KINDS | {
    "ACTION_RESOLVED",
    "TURN_STARTED",
    "ITEM_COMPLETED",
    "PROCESS_EXITED",
    "SESSION_CLOSED",
}
AXIS_BASELINE_KINDS = {
    "lifecycle": set(LIFECYCLE_PHASE_KINDS)
    | {"PROCESS_RESUMED", "PROCESS_EXITED", "SESSION_CLOSED"},
    "attention": {"ACTION_REQUIRED"} | set(ATTENTION_CLEAR_KINDS),
    "failure_recovery": {
        "TURN_STARTED",
        "TURN_FAILED",
        "COMPACT_FAILED",
        "RECONNECTING",
        "TRANSPORT_FALLBACK",
        "RECOVERED",
        "PROCESS_RESUMED",
        "PROCESS_EXITED",
        "SESSION_CLOSED",
    }
    | set(PROGRESS_KINDS),
    "terminal_ownership": {
        "TOOL_COMPLETED",
        *TERMINAL_KINDS,
        "PROCESS_EXITED",
        "SESSION_CLOSED",
    },
}
NON_SEMANTIC_KINDS = {
    "KEEPALIVE",
    "TOKEN_USAGE",
    "RATE_LIMIT",
    "MODEL_CONFIG",
    "UNPARSED_PAYLOAD",
}

CLOCK_CLEAR_KINDS = (ATTENTION_CLEAR_KINDS - {"TURN_FAILED", "FILE_CHANGE_FAILED"}) | {
    "COMPACT_COMPLETED",
}
CLOCK_POLICIES = {
    "rollout": (5.0, 2.0, "codex_process_wall_clock"),
    "tui_session_log": (5.0, 2.0, "codex_tui_wall_clock"),
    "compact_hook": (30.0, 5.0, "hook_producer_wall_clock"),
    "log": (120.0, 120.0, "sqlite_log_wall_clock"),
    "sse": (120.0, 120.0, "sqlite_sse_wall_clock"),
    "process": (2.0, 1.0, "observer_process_wall_clock"),
    "detector": (2.0, 1.0, "observer_wall_clock"),
}
DEDUPE_FILTER_BITS = 1 << 18
DEDUPE_FILTER_HASHES = 4
DEDUPE_FILTER_DEGRADED_RATIO = 0.90
MAX_MUTABLE_STREAM_IDENTITIES_PER_SESSION = 32


class BoundedDedupeFilter:
    """Fixed-size duplicate memory with no false negatives for inserted keys."""

    def __init__(
        self,
        bit_count: int = DEDUPE_FILTER_BITS,
        hash_count: int = DEDUPE_FILTER_HASHES,
    ) -> None:
        if bit_count <= 0 or bit_count & (bit_count - 1):
            raise ValueError("bit_count must be a positive power of two")
        self.bit_count = bit_count
        self.hash_count = hash_count
        self.bits = 0

    def _positions(self, value: str) -> tuple[int, ...]:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=32).digest()
        mask = self.bit_count - 1
        return tuple(
            int.from_bytes(digest[index * 4 : index * 4 + 4], "little") & mask
            for index in range(self.hash_count)
        )

    def __contains__(self, value: str) -> bool:
        return all(self.bits & (1 << position) for position in self._positions(value))

    def add(self, value: str) -> None:
        for position in self._positions(value):
            self.bits |= 1 << position

    def __len__(self) -> int:
        return self.bits.bit_count()

    @property
    def fill_ratio(self) -> float:
        return len(self) / self.bit_count

    @property
    def degraded(self) -> bool:
        return self.fill_ratio >= DEDUPE_FILTER_DEGRADED_RATIO


class SessionStateMachine:
    def __init__(
        self,
        lookback_seconds: int,
        *,
        dedupe_filter_bits: int = DEDUPE_FILTER_BITS,
    ) -> None:
        self.lookback_seconds = lookback_seconds
        self.dedupe_filter_bits = dedupe_filter_bits
        self.events: dict[str | SessionIdentity, list[NormalizedEvent]] = defaultdict(list)
        self.seen: dict[str | SessionIdentity, BoundedDedupeFilter] = defaultdict(
            lambda: BoundedDedupeFilter(self.dedupe_filter_bits)
        )
        self.dedupe_matches: dict[str | SessionIdentity, int] = defaultdict(int)
        self.dedupe_degraded_drops: dict[str | SessionIdentity, int] = defaultdict(int)
        self.stream_generations: dict[str | SessionIdentity, dict[str, int]] = defaultdict(dict)
        self.stale_stream_generation_dropped: dict[str | SessionIdentity, int] = defaultdict(int)
        self.stream_identity_limit_dropped: dict[str | SessionIdentity, int] = defaultdict(int)
        self.stream_generation_advances: dict[str | SessionIdentity, int] = defaultdict(int)
        self.pending_recovery: dict[str | SessionIdentity, RecoveryState] = {}
        self.alerts: dict[str | SessionIdentity, list[AlertOccurrence]] = defaultdict(list)
        self.compactions: dict[str | SessionIdentity, list[CompactionSummary]] = defaultdict(list)
        self.clock_state: dict[
            str | SessionIdentity,
            dict[str, tuple[float, float, float]],
        ] = defaultdict(dict)
        self.clock_observed_at: dict[str | SessionIdentity, float] = {}
        self.clock_decision_at: dict[str | SessionIdentity, float] = {}
        self.coverage_sources: dict[str | SessionIdentity, dict[str, tuple[int, int, bool]]] = (
            defaultdict(dict)
        )
        self.coverage_gap_at: dict[str | SessionIdentity, float] = {}
        self.coverage_gap_reasons: dict[str | SessionIdentity, tuple[str, ...]] = {}
        self.coverage_backlog: dict[str | SessionIdentity, bool] = {}
        self.terminal_probe_complete: dict[str | SessionIdentity, bool] = {}
        self.terminal_probe_complete_at: dict[str | SessionIdentity, float] = {}
        self.network_probe_complete: dict[str | SessionIdentity, bool] = {}
        self.silence_probe_complete: dict[str | SessionIdentity, bool] = {}
        self.event_retention_dropped: dict[str | SessionIdentity, int] = defaultdict(int)
        self.axis_baselines: dict[
            str | SessionIdentity, dict[str, NormalizedEvent]
        ] = defaultdict(dict)
        self.clock_sequence = 0

    def _remember_axis_baselines(
        self,
        key: str | SessionIdentity,
        event: NormalizedEvent,
    ) -> None:
        baselines = self.axis_baselines[key]
        for axis, kinds in AXIS_BASELINE_KINDS.items():
            if event.kind not in kinds:
                continue
            previous = baselines.get(axis)
            if previous is None or (event.timestamp, event.source_id) >= (
                previous.timestamp,
                previous.source_id,
            ):
                baselines[axis] = event

    @staticmethod
    def _storage_key(key: str | SessionIdentity) -> str:
        return key.storage_key if isinstance(key, SessionIdentity) else key

    def _accept_stream_generation(
        self,
        key: str | SessionIdentity,
        event: NormalizedEvent,
    ) -> bool:
        if event.source not in {"rollout", "tui_session_log", "compact_hook"}:
            return True
        path_hash = event.metadata.get("stream_path_sha256")
        generation = event.metadata.get("stream_generation")
        if not isinstance(path_hash, str) or len(path_hash) != 64:
            return True
        if not isinstance(generation, int) or generation < 0:
            return True
        stream_key = f"{event.source}:{path_hash}"
        generations = self.stream_generations[key]
        previous = generations.get(stream_key)
        if previous is None:
            if len(generations) >= MAX_MUTABLE_STREAM_IDENTITIES_PER_SESSION:
                self.stream_identity_limit_dropped[key] += 1
                return False
            generations[stream_key] = generation
            return True
        if generation < previous:
            self.stale_stream_generation_dropped[key] += 1
            return False
        if generation > previous:
            generations[stream_key] = generation
            self.stream_generation_advances[key] += 1
        return True

    def update_coverage(
        self,
        key: str | SessionIdentity,
        coverage: EvidenceCoverage,
    ) -> None:
        sources = self.coverage_sources[key]
        previous = sources.get(coverage.source_epoch) if coverage.source_epoch else None
        reasons: list[str] = []
        if coverage.bootstrap_truncated and (previous is None or not previous[2]):
            reasons.append("bootstrap_tail_truncated")
        if previous is not None:
            if coverage.gap_count > previous[0]:
                reasons.append("explicit_ingress_gap")
            if coverage.stream_uncertainty_count > previous[1]:
                reasons.append("mutable_stream_uncertain")
        elif coverage.gap_count:
            reasons.append("explicit_ingress_gap")
        elif coverage.stream_uncertainty_count:
            reasons.append("mutable_stream_uncertain")
        if coverage.generation_changed:
            reasons.append("stream_generation_changed")
        if coverage.copy_truncated:
            reasons.append("copy_truncated")
        if reasons:
            self.coverage_gap_at[key] = max(
                self.coverage_gap_at.get(key, 0.0),
                coverage.observed_at - 0.000001,
            )
            existing = self.coverage_gap_reasons.get(key, ())
            self.coverage_gap_reasons[key] = tuple(dict.fromkeys((*existing, *reasons)))[-8:]
        if coverage.source_epoch:
            if (
                coverage.source_epoch not in sources
                and len(sources) >= MAX_MUTABLE_STREAM_IDENTITIES_PER_SESSION
            ):
                oldest = next(iter(sources))
                sources.pop(oldest, None)
                self.coverage_gap_at[key] = max(
                    self.coverage_gap_at.get(key, 0.0),
                    coverage.observed_at - 0.000001,
                )
                self.coverage_gap_reasons[key] = tuple(
                    dict.fromkeys(
                        (*self.coverage_gap_reasons.get(key, ()), "coverage_source_limit")
                    )
                )[-8:]
            sources[coverage.source_epoch] = (
                coverage.gap_count,
                coverage.stream_uncertainty_count,
                coverage.bootstrap_truncated,
            )
        self.coverage_backlog[key] = coverage.backlog_pending
        if coverage.terminal_probe_complete is True:
            self.terminal_probe_complete[key] = True
            self.terminal_probe_complete_at[key] = coverage.observed_at
        elif coverage.terminal_probe_complete is False:
            self.terminal_probe_complete[key] = False
            self.terminal_probe_complete_at.pop(key, None)
        if coverage.network_probe_complete is not None:
            self.network_probe_complete[key] = coverage.network_probe_complete
        if coverage.silence_probe_complete is not None:
            self.silence_probe_complete[key] = coverage.silence_probe_complete

    @staticmethod
    def _alert_id(key: str | SessionIdentity, kind: str, opened_at: float) -> str:
        identity = f"{SessionStateMachine._storage_key(key)}\0{kind}\0{opened_at:.6f}".encode()
        return "alert_" + hashlib.sha256(identity).hexdigest()[:20]

    def acknowledge_alert(
        self,
        key: str | SessionIdentity,
        alert_id: str,
        now: float | None = None,
    ) -> bool:
        """Acknowledge an active occurrence without suppressing its resolution."""

        timestamp = time.time() if now is None else now
        for occurrence in self.alerts.get(key, []):
            if occurrence.id != alert_id or not occurrence.active:
                continue
            if occurrence.status != AlertStatus.ACKNOWLEDGED:
                occurrence.status = AlertStatus.ACKNOWLEDGED
                occurrence.acknowledged_at = timestamp
                occurrence.updated_at = timestamp
                occurrence.transitions.append(AlertTransition(AlertStatus.ACKNOWLEDGED, timestamp))
            return True
        return False

    def retained_events(self, key: str | SessionIdentity) -> tuple[NormalizedEvent, ...]:
        """Return the complete bounded event history, independent of UI lookback."""

        events = self.events.get(key)
        if events is None and isinstance(key, SessionIdentity):
            events = self.events.get(key.storage_key)
        return tuple(events or ())

    @staticmethod
    def _clock_policy(source: str) -> tuple[float, float, str]:
        for prefix, policy in CLOCK_POLICIES.items():
            if source.startswith(prefix):
                return policy
        return 30.0, 30.0, "external_wall_clock"

    def _adjudicate_clock(
        self,
        key: str | SessionIdentity,
        event: NormalizedEvent,
    ) -> NormalizedEvent:
        source_timestamp = event.source_timestamp or event.timestamp
        self.clock_sequence += 1
        observed_at = event.observed_at
        future_tolerance, rollback_tolerance, clock_domain = self._clock_policy(event.source)
        if observed_at is None:
            return replace(
                event,
                source_timestamp=source_timestamp,
                adjudicated_at=event.timestamp,
                clock_domain=clock_domain,
                clock_trust=Confidence.MEDIUM,
                clock_sequence=self.clock_sequence,
            )

        previous_observed = self.clock_observed_at.get(key)
        logical_observed = observed_at
        reasons: list[str] = []
        if previous_observed is not None and observed_at < previous_observed - 1.0:
            logical_observed = previous_observed + 0.000001
            reasons.append("observer_wall_clock_rollback")
        self.clock_observed_at[key] = max(previous_observed or observed_at, logical_observed)

        last_source, _, last_decision = self.clock_state[key].get(
            clock_domain,
            (float("-inf"), float("-inf"), float("-inf")),
        )
        global_decision = self.clock_decision_at.get(key, float("-inf"))
        invalid = not math.isfinite(source_timestamp) or source_timestamp <= 0
        future = not invalid and source_timestamp > observed_at + future_tolerance
        rollback = (
            not invalid
            and math.isfinite(last_source)
            and source_timestamp < last_source - rollback_tolerance
        )
        if invalid:
            reasons.append("invalid_source_timestamp")
        if future:
            reasons.append(f"future_source_timestamp_gt_{future_tolerance:g}s")
        if rollback:
            reasons.append(f"source_clock_rollback_gt_{rollback_tolerance:g}s")

        if reasons and event.kind in CLOCK_CLEAR_KINDS:
            decision = max(logical_observed, global_decision + 0.000001)
        elif rollback or "observer_wall_clock_rollback" in reasons:
            decision = (
                min(source_timestamp, global_decision - 0.000001)
                if math.isfinite(global_decision)
                else source_timestamp
            )
        elif invalid or future:
            decision = logical_observed
        else:
            decision = min(source_timestamp, logical_observed)
            if math.isfinite(last_source) and source_timestamp >= last_source:
                decision = max(decision, last_decision + 0.000001)

        if not invalid and not future and (not rollback or event.kind in CLOCK_CLEAR_KINDS):
            next_source = source_timestamp
        else:
            next_source = last_source
        self.clock_state[key][clock_domain] = (
            next_source,
            logical_observed,
            max(last_decision, decision),
        )
        self.clock_decision_at[key] = max(global_decision, decision)
        return replace(
            event,
            timestamp=decision,
            source_timestamp=source_timestamp,
            adjudicated_at=decision,
            clock_domain=clock_domain,
            clock_trust=Confidence.LOW if reasons else Confidence.HIGH,
            clock_uncertain=bool(reasons),
            clock_reason=",".join(reasons),
            clock_sequence=self.clock_sequence,
        )

    def _record_compaction(self, key: str | SessionIdentity, event: NormalizedEvent) -> None:
        metadata = event.metadata
        context_tokens = self._int(metadata.get("context_tokens"))
        context_window = self._int(metadata.get("context_window"))
        auto_compact_token_limit = self._int(metadata.get("auto_compact_token_limit"))
        trigger = str(metadata.get("trigger") or "")
        history = self.compactions[key]
        edge = {
            "COMPACT_REQUESTED": "requested",
            "COMPACT_CANDIDATE": "candidate",
            "COMPACTING": "started",
            "COMPACT_PROGRESS": "progress",
            "COMPACT_COMPLETED": "completed",
            "COMPACT_FAILED": "failed",
            "TURN_FAILED": "failed",
            "COMPACT_ABORTED": "aborted",
            "TURN_ABORTED": "aborted",
        }[event.kind]
        if history and edge in {"requested", "candidate", "started", "progress"}:
            latest_terminal_at = history[-1].terminal_at
            source_time = event.source_timestamp or event.timestamp
            if latest_terminal_at is not None and source_time <= latest_terminal_at:
                return
        evidence = CompactionEvidence(
            edge=edge,
            timestamp=event.timestamp,
            source=event.source,
            observed_at=event.observed_at,
            confidence=event.confidence,
            direct=not event.derived,
            detail=event.detail or event.summary,
            parse_validity=event.parse_validity,
            source_authenticity=event.source_authenticity,
            identity_binding=event.identity_binding,
            semantic_confidence=event.semantic_confidence,
            binding_evidence=event.binding_evidence,
        )
        current = next(
            (
                item
                for item in reversed(history)
                if item.status not in {"completed", "failed", "aborted", "dismissed"}
                and (not event.turn_id or not item.turn_id or item.turn_id == event.turn_id)
            ),
            None,
        )
        if (
            event.source == "compact_hook"
            and event.source_authenticity != Confidence.HIGH
            and current is not None
            and any(item.source == "rollout" for item in current.evidence)
        ):
            history[history.index(current)] = replace(
                current,
                evidence=(*current.evidence, evidence),
                confidence=min(current.confidence, event.confidence),
            )
            return
        if current is None and history and edge in {"completed", "failed", "aborted"}:
            latest = history[-1]
            terminal_at = latest.terminal_at
            if (
                latest.status == edge
                and terminal_at is not None
                and abs(terminal_at - event.timestamp) <= 1.0
            ):
                current = latest
        if current is None:
            operation_id = hashlib.sha256(
                f"{key}:{event.turn_id}:{trigger}:{edge}:{event.timestamp}".encode()
            ).hexdigest()[:16]
            current = CompactionSummary(
                operation_id=operation_id,
                trigger=trigger or "unknown",
                turn_id=event.turn_id,
            )
            history.append(current)
        evidence_items = list(current.evidence)
        if not any(
            item.edge == evidence.edge
            and item.source == evidence.source
            and abs(item.timestamp - evidence.timestamp) <= 0.001
            for item in evidence_items
        ):
            evidence_items.append(evidence)
        confidence_rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
        confidence = max(
            (item.confidence for item in evidence_items),
            key=confidence_rank.__getitem__,
            default=event.confidence,
        )
        updates: dict[str, Any] = {
            "trigger": trigger or current.trigger or "unknown",
            "turn_id": event.turn_id or current.turn_id,
            "source": current.source or event.source,
            "confidence": confidence,
            "context_tokens": context_tokens or current.context_tokens,
            "context_window": context_window or current.context_window,
            "auto_compact_token_limit": (
                auto_compact_token_limit or current.auto_compact_token_limit
            ),
            "reconstructed": current.reconstructed or bool(metadata.get("reconstructed")),
            "evidence": tuple(sorted(evidence_items, key=lambda item: item.timestamp)),
        }
        if edge == "requested":
            updates.update(
                status="requested",
                requested_at=min(
                    value for value in (current.requested_at, event.timestamp) if value is not None
                ),
            )
        elif edge == "candidate":
            updates.update(status="candidate", started_at=current.started_at or event.timestamp)
        elif edge == "started":
            started_at = min(
                value for value in (current.started_at, event.timestamp) if value is not None
            )
            updates.update(status="running", started_at=started_at)
        elif edge == "progress":
            updates.update(
                status=current.status,
                retry_count=current.retry_count + int(bool(metadata.get("retry"))),
            )
        elif edge == "completed":
            updates.update(
                status="completed",
                completed_at=event.timestamp,
                context_tokens_after=self._int(metadata.get("context_tokens_after")),
            )
        elif edge == "failed":
            updates.update(status="failed", failed_at=event.timestamp, failure=event.failure)
        else:
            updates.update(status="aborted", aborted_at=event.timestamp)
        history[history.index(current)] = replace(current, **updates)
        if len(history) > 20:
            del history[:-20]

    def observe_compaction(
        self,
        key: str | SessionIdentity,
        *,
        timestamp: float,
        source: str,
        detail: str,
        turn_id: str = "",
    ) -> None:
        """Attach low-level progress evidence without appending a timeline event."""

        history = self.compactions.get(key, [])
        if not history or history[-1].status not in {"requested", "candidate", "running"}:
            return
        self._record_compaction(
            key,
            NormalizedEvent(
                timestamp=timestamp,
                kind="COMPACT_PROGRESS",
                summary="compact 活动证据",
                detail=detail,
                source=source,
                confidence=Confidence.MEDIUM,
                turn_id=turn_id or history[-1].turn_id,
                source_id=f"compact-progress:{source}:{timestamp}",
                derived=True,
                observed_at=timestamp,
            ),
        )

    def _reconcile_alert(
        self, key: str | SessionIdentity, state: SessionHealth, now: float
    ) -> None:
        history = self.alerts[key]
        active = next((item for item in reversed(history) if item.active), None)
        if not state.alert:
            if active:
                active.status = AlertStatus.RESOLVED
                active.resolved_at = now
                active.updated_at = now
                active.transitions.append(AlertTransition(AlertStatus.RESOLVED, now))
            state.alerts = deepcopy(history)
            return

        if active and active.kind != state.alert:
            active.status = AlertStatus.RESOLVED
            active.resolved_at = now
            active.updated_at = now
            active.transitions.append(
                AlertTransition(AlertStatus.RESOLVED, now, "replaced by another alert")
            )
            active = None

        if active is None:
            active = AlertOccurrence(
                id=self._alert_id(key, state.alert, now),
                kind=state.alert,
                severity=state.alert_level,
                status=AlertStatus.OPENED,
                reason=state.alert_reason,
                opened_at=now,
                updated_at=now,
                transitions=[AlertTransition(AlertStatus.OPENED, now, state.alert_reason)],
            )
            history.append(active)
            if len(history) > MAX_EVENTS_PER_SESSION:
                del history[:-MAX_EVENTS_PER_SESSION]
        else:
            active.reason = state.alert_reason
            active.updated_at = now
            if state.alert_level == "严重" and active.severity != "严重":
                active.severity = state.alert_level
                active.status = AlertStatus.ESCALATED
                active.escalated_at = now
                active.transitions.append(
                    AlertTransition(AlertStatus.ESCALATED, now, state.alert_reason)
                )
        state.alerts = deepcopy(history)

    def ingest(self, key: str | SessionIdentity, incoming: list[NormalizedEvent]) -> None:
        bucket = self.events[key]
        for raw_event in sorted(incoming, key=lambda item: (item.timestamp, item.source_id)):
            if not self._accept_stream_generation(key, raw_event):
                continue
            event = self._adjudicate_clock(key, raw_event)
            dedupe_key = event.source_id or (
                f"{event.source}:{event.timestamp}:{event.kind}:{event.turn_id}:{event.detail}"
            )
            if dedupe_key in self.seen[key]:
                self.dedupe_matches[key] += 1
                if self.seen[key].degraded:
                    self.dedupe_degraded_drops[key] += 1
                continue
            self.seen[key].add(dedupe_key)
            compact_kinds = {
                "COMPACT_REQUESTED",
                "COMPACT_CANDIDATE",
                "COMPACTING",
                "COMPACT_PROGRESS",
                "COMPACT_COMPLETED",
                "COMPACT_FAILED",
                "COMPACT_ABORTED",
            }
            open_compact = bool(
                self.compactions[key]
                and self.compactions[key][-1].status
                not in {"completed", "failed", "aborted", "dismissed"}
            )
            if event.kind in compact_kinds or (
                open_compact and event.kind in {"TURN_FAILED", "TURN_ABORTED"}
            ):
                self._record_compaction(key, event)
            elif open_compact and event.kind in {"RECONNECTING", "TRANSPORT_FALLBACK"}:
                self._record_compaction(
                    key,
                    replace(
                        event,
                        kind="COMPACT_PROGRESS",
                        metadata={
                            **event.metadata,
                            "retry": event.kind == "RECONNECTING",
                        },
                        derived=True,
                    ),
                )
            if event.kind == "TOKEN_USAGE" and self.compactions[key]:
                compact = self.compactions[key][-1]
                context_after = self._int(event.metadata.get("context_tokens"))
                if (
                    compact.status == "completed"
                    and compact.completed_at is not None
                    and compact.context_tokens_after is None
                    and context_after is not None
                    and 0 <= event.timestamp - compact.completed_at <= 120
                ):
                    self.compactions[key][-1] = replace(
                        compact,
                        context_tokens_after=context_after,
                    )
            if event.kind in compact_kinds and any(
                existing.kind == event.kind and abs(existing.timestamp - event.timestamp) <= 1.0
                for existing in reversed(bucket[-8:])
            ):
                continue
            if event.kind == "TURN_FAILED" and event.turn_id:
                duplicate_index = next(
                    (
                        index
                        for index, existing in enumerate(bucket)
                        if existing.kind == "TURN_FAILED" and existing.turn_id == event.turn_id
                    ),
                    None,
                )
                if duplicate_index is not None:
                    old = bucket[duplicate_index]
                    old_details = old.failure.additional_details if old.failure else ""
                    new_details = event.failure.additional_details if event.failure else ""
                    old_size = len(old.detail) + len(old_details)
                    new_size = len(event.detail) + len(new_details)
                    if new_size > old_size:
                        bucket[duplicate_index] = event
                    continue
            if (
                self.compactions[key]
                and self.compactions[key][-1].status == "candidate"
                and event.kind not in compact_kinds
                and event.kind not in {"TOKEN_USAGE", "MODEL_CONFIG", "KEEPALIVE", "TURN_STARTED"}
            ):
                self.compactions[key][-1] = replace(self.compactions[key][-1], status="dismissed")
            recovery = self.pending_recovery.get(key)
            if (
                recovery
                and event.kind in PROGRESS_KINDS
                and event.kind not in {"TURN_FAILED", "TURN_ABORTED"}
            ):
                recovered = NormalizedEvent(
                    timestamp=event.timestamp,
                    kind="RECOVERED",
                    summary=EVENT_LABELS["RECOVERED"],
                    detail=f"{recovery.value} 后重新观察到进展",
                    source="detector",
                    confidence=Confidence.HIGH,
                    turn_id=event.turn_id,
                    source_id=f"recovered:{dedupe_key}",
                    observed_at=event.observed_at,
                )
                bucket.append(recovered)
                self.seen[key].add(recovered.source_id)
                self.pending_recovery.pop(key, None)
            if event.kind == "RECONNECTING":
                self.pending_recovery[key] = RecoveryState.RECONNECTING
            elif event.kind == "TRANSPORT_FALLBACK":
                self.pending_recovery[key] = RecoveryState.TRANSPORT_FALLBACK
            elif event.kind == "RECOVERED":
                self.pending_recovery.pop(key, None)
            elif event.kind in TERMINAL_KINDS:
                self.pending_recovery.pop(key, None)
            self._remember_axis_baselines(key, event)
            bucket.append(event)
        bucket.sort(key=lambda item: (item.timestamp, item.source_id))
        if len(bucket) > MAX_EVENTS_PER_SESSION:
            dropped = len(bucket) - MAX_EVENTS_PER_SESSION
            retained = bucket[-MAX_EVENTS_PER_SESSION:]
            self.events[key] = retained
            self.event_retention_dropped[key] += dropped

    @staticmethod
    def _latest(events: list[NormalizedEvent], *kinds: str) -> NormalizedEvent | None:
        wanted = set(kinds)
        return next((event for event in reversed(events) if event.kind in wanted), None)

    @staticmethod
    def _clock_assessments(events: list[NormalizedEvent]) -> tuple[ClockAssessment, ...]:
        latest_by_source: dict[str, NormalizedEvent] = {}
        for event in events:
            previous = latest_by_source.get(event.source)
            if previous is None or event.clock_sequence >= previous.clock_sequence:
                latest_by_source[event.source] = event
        return tuple(
            ClockAssessment(
                source=event.source,
                clock_domain=event.clock_domain,
                source_timestamp=event.source_timestamp or event.timestamp,
                observed_at=event.observed_at,
                adjudicated_at=event.decision_timestamp,
                reason=event.clock_reason,
            )
            for event in latest_by_source.values()
            if event.clock_uncertain
        )

    @staticmethod
    def _tokens(event: NormalizedEvent | None) -> tuple[int | None, int | None]:
        if not event or not event.detail:
            return None, None
        if "/" in event.detail and event.detail.replace("/", "").isdigit():
            used, limit = event.detail.split("/", 1)
            return int(used), int(limit)
        try:
            payload = json.loads(event.detail)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        usage = payload.get("total_token_usage") or payload.get("total_usage") or payload
        if not isinstance(usage, dict):
            return None, None
        used_value = usage.get("total_tokens") or usage.get("input_tokens")
        limit_value = payload.get("model_context_window") or payload.get("context_window")
        return (
            int(used_value) if isinstance(used_value, (int, float)) else None,
            int(limit_value) if isinstance(limit_value, (int, float)) else None,
        )

    @staticmethod
    def _int(value: object) -> int | None:
        return (
            int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        )

    @classmethod
    def _token_summary(
        cls, event: NormalizedEvent | None, *, cumulative: bool = False
    ) -> TokenUsageSummary | None:
        if not event:
            return None
        metadata = event.metadata
        total = metadata.get("total_usage")
        last = metadata.get("last_usage")
        total = total if isinstance(total, dict) else {}
        last = last if isinstance(last, dict) else {}
        usage = total if cumulative else last or total
        if not metadata and event.detail:
            try:
                raw = json.loads(event.detail)
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, dict):
                total = raw.get("total_token_usage") or raw.get("total_usage") or raw
                last = raw.get("last_token_usage") or raw.get("last_usage") or {}
                total = total if isinstance(total, dict) else {}
                last = last if isinstance(last, dict) else {}
                usage = total if cumulative else last or total
                metadata = {
                    "context_window": raw.get("model_context_window") or raw.get("context_window"),
                    "context_tokens": total.get("input_tokens"),
                }
        if not usage and not metadata.get("context_window"):
            return None
        return TokenUsageSummary(
            input_tokens=cls._int(usage.get("input_tokens")),
            cached_input_tokens=cls._int(
                usage.get("cached_input_tokens") or usage.get("cached_tokens")
            ),
            output_tokens=cls._int(usage.get("output_tokens")),
            reasoning_output_tokens=cls._int(
                usage.get("reasoning_output_tokens") or usage.get("reasoning_tokens")
            ),
            total_tokens=cls._int(usage.get("total_tokens")),
            context_tokens=cls._int(metadata.get("context_tokens")),
            context_window=cls._int(metadata.get("context_window")),
            provenance=event.provenance,
        )

    @staticmethod
    def _rate_window(value: object) -> RateLimitWindow | None:
        if not isinstance(value, dict):
            return None
        used = value.get("used_percent")
        if used is None:
            used = value.get("used_percentage")
        reset = value.get("reset_at") or value.get("resets_at")
        minutes = value.get("window_minutes")
        return RateLimitWindow(
            float(used) if isinstance(used, (int, float)) else None,
            float(reset) if isinstance(reset, (int, float)) else None,
            int(minutes) if isinstance(minutes, (int, float)) else None,
        )

    @classmethod
    def _rate_limits(cls, event: NormalizedEvent | None) -> RateLimitSummary | None:
        if not event:
            return None
        raw = event.metadata.get("rate_limits")
        if not isinstance(raw, dict):
            return None
        credits = raw.get("credits")
        credits_dict = credits if isinstance(credits, dict) else {}
        credit_value = credits_dict.get("balance") if credits_dict else credits
        reached = raw.get("reached")
        return RateLimitSummary(
            primary=cls._rate_window(raw.get("primary")),
            secondary=cls._rate_window(raw.get("secondary")),
            credits=float(credit_value) if isinstance(credit_value, (int, float)) else None,
            has_credits=(
                bool(credits_dict.get("has_credits")) if "has_credits" in credits_dict else None
            ),
            reached=bool(reached) if isinstance(reached, bool) else None,
            reason=str(raw.get("reason") or raw.get("limit_reached_reason") or ""),
            provenance=event.provenance,
        )

    @staticmethod
    def _duration(start: NormalizedEvent, end: NormalizedEvent | None) -> tuple[float | None, bool]:
        metadata = end.metadata if end else start.metadata
        direct = metadata.get("duration_seconds")
        if isinstance(direct, (int, float)):
            return float(direct), False
        if end:
            started_at = start.metadata.get("started_at")
            completed_at = end.metadata.get("completed_at")
            started_at = (
                float(started_at) if isinstance(started_at, (int, float)) else start.timestamp
            )
            completed_at = (
                float(completed_at) if isinstance(completed_at, (int, float)) else end.timestamp
            )
            return max(0.0, completed_at - started_at), True
        return None, True

    @classmethod
    def _tool_summaries(cls, events: list[NormalizedEvent]) -> list[ToolExecutionSummary]:
        starts: dict[str, NormalizedEvent] = {}
        unnamed: dict[str, list[NormalizedEvent]] = defaultdict(list)
        completed: set[str] = set()
        summaries: list[ToolExecutionSummary] = []
        for event in events:
            if event.kind not in {"TOOL_RUNNING", "TOOL_COMPLETED"}:
                continue
            call_id = str(event.metadata.get("call_id") or "")
            if event.kind == "TOOL_RUNNING":
                if call_id:
                    starts[call_id] = event
                else:
                    unnamed[event.turn_id].append(event)
                continue
            start = starts.get(call_id) if call_id else None
            if not start and unnamed[event.turn_id]:
                start = unnamed[event.turn_id].pop(0)
            identity = call_id or (start.source_id if start else event.source_id)
            duration, derived = cls._duration(start or event, event)
            exit_code = cls._int(event.metadata.get("exit_code"))
            completion = str(event.metadata.get("completion_status") or "")
            failed = exit_code not in (None, 0) or completion.lower() in {
                "failed",
                "error",
                "errored",
            }
            raw_output_name = str(event.metadata.get("display_name") or "")
            output_name_is_fallback = bool(
                event.metadata.get("display_name_is_fallback")
                or raw_output_name.endswith("_output")
                or "tool_call_output" in raw_output_name
            )
            event_display_name = raw_output_name
            event_tool_name = str(event.metadata.get("tool_name") or "")
            display_name = (
                str(start.metadata.get("display_name") or "")
                if start and (output_name_is_fallback or not event_display_name)
                else event_display_name
            )
            tool_name = (
                str(start.metadata.get("tool_name") or "")
                if start and (output_name_is_fallback or not event_tool_name)
                else event_tool_name
            )
            summaries.append(
                ToolExecutionSummary(
                    call_id=identity,
                    turn_id=event.turn_id or (start.turn_id if start else ""),
                    category=str(
                        (
                            start.metadata.get("category")
                            if start and output_name_is_fallback
                            else event.metadata.get("category")
                        )
                        or (start.metadata.get("category") if start else "tool")
                    ),
                    display_name=str(display_name or event.detail or "tool"),
                    tool_name=tool_name,
                    started_at=(
                        float(start.metadata["started_at"])
                        if start and isinstance(start.metadata.get("started_at"), (int, float))
                        else start.timestamp
                        if start
                        else None
                    ),
                    completed_at=(
                        float(event.metadata["completed_at"])
                        if isinstance(event.metadata.get("completed_at"), (int, float))
                        else event.timestamp
                    ),
                    duration_seconds=duration,
                    status="failed" if failed else "completed",
                    exit_code=exit_code,
                    completion_status=completion,
                    command=str(
                        event.metadata.get("command")
                        or (start.metadata.get("command") if start else "")
                    ),
                    cwd=str(
                        event.metadata.get("cwd") or (start.metadata.get("cwd") if start else "")
                    ),
                    arguments=str(
                        event.metadata.get("arguments")
                        or (start.metadata.get("arguments") if start else "")
                    ),
                    output=str(event.metadata.get("output") or ""),
                    files=tuple(
                        dict.fromkeys(
                            [
                                str(path)
                                for path in ((start.metadata.get("files") if start else []) or [])
                            ]
                            + [str(path) for path in (event.metadata.get("files") or [])]
                        )
                    ),
                    provenance=Provenance(
                        event.source,
                        min(event.confidence, start.confidence) if start else event.confidence,
                        derived=derived,
                        complete=start is not None,
                    ),
                )
            )
            completed.add(identity)
        for call_id, start in starts.items():
            if call_id in completed:
                continue
            summaries.append(
                ToolExecutionSummary(
                    call_id=call_id,
                    turn_id=start.turn_id,
                    category=str(start.metadata.get("category") or "tool"),
                    display_name=str(start.metadata.get("display_name") or start.detail or "tool"),
                    tool_name=str(start.metadata.get("tool_name") or ""),
                    started_at=(
                        float(start.metadata["started_at"])
                        if isinstance(start.metadata.get("started_at"), (int, float))
                        else start.timestamp
                    ),
                    status="running",
                    command=str(start.metadata.get("command") or ""),
                    cwd=str(start.metadata.get("cwd") or ""),
                    arguments=str(start.metadata.get("arguments") or ""),
                    output=str(start.metadata.get("output") or ""),
                    files=tuple(str(path) for path in (start.metadata.get("files") or [])),
                    provenance=Provenance(
                        start.source, start.confidence, start.derived, complete=False
                    ),
                )
            )
        for queue in unnamed.values():
            for start in queue:
                summaries.append(
                    ToolExecutionSummary(
                        call_id=start.source_id,
                        turn_id=start.turn_id,
                        category=str(start.metadata.get("category") or "tool"),
                        display_name=str(
                            start.metadata.get("display_name") or start.detail or "tool"
                        ),
                        tool_name=str(start.metadata.get("tool_name") or ""),
                        started_at=start.timestamp,
                        status="running",
                        command=str(start.metadata.get("command") or ""),
                        cwd=str(start.metadata.get("cwd") or ""),
                        arguments=str(start.metadata.get("arguments") or ""),
                        output=str(start.metadata.get("output") or ""),
                        files=tuple(str(path) for path in (start.metadata.get("files") or [])),
                        provenance=Provenance(
                            start.source, start.confidence, derived=True, complete=False
                        ),
                    )
                )
        return sorted(summaries, key=lambda item: item.started_at or item.completed_at or 0.0)

    @classmethod
    def _turn_summaries(
        cls,
        events: list[NormalizedEvent],
        tools: list[ToolExecutionSummary],
        process: ProcessInfo,
    ) -> list[TurnSummary]:
        groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        active_turn = ""
        for event in events:
            if event.kind == "TURN_STARTED":
                active_turn = event.turn_id or f"turn@{event.source_id or event.timestamp}"
            turn_id = event.turn_id or active_turn
            if turn_id:
                groups[turn_id].append(event)
            if event.kind in TERMINAL_KINDS and turn_id == active_turn:
                active_turn = ""
        results: list[TurnSummary] = []
        for turn_id, turn_events in groups.items():
            start = cls._latest(turn_events, "TURN_STARTED")
            terminal = cls._latest(turn_events, *TERMINAL_KINDS)
            if not start and not terminal:
                continue
            first_token = next(
                (
                    item
                    for item in turn_events
                    if item.kind in {"RESPONSE_STARTED", "MODEL_PROGRESS"}
                    and (not start or item.timestamp >= start.timestamp)
                ),
                None,
            )
            turn_tools = [item for item in tools if item.turn_id == turn_id]
            known_tool_durations = [
                item.duration_seconds for item in turn_tools if item.duration_seconds is not None
            ]
            longest = max(
                (item for item in turn_tools if item.duration_seconds is not None),
                key=lambda item: item.duration_seconds or 0.0,
                default=None,
            )
            duration = None
            duration_derived = True
            if start:
                duration, duration_derived = cls._duration(start, terminal)
            token_event = cls._latest(turn_events, "TOKEN_USAGE")
            reconnects = [item for item in turn_events if item.kind == "RECONNECTING"]
            fallbacks = [item for item in turn_events if item.kind == "TRANSPORT_FALLBACK"]
            recovered = cls._latest(turn_events, "RECOVERED")
            recovery_start = min(
                (item.timestamp for item in reconnects + fallbacks),
                default=None,
            )
            recovery_duration = (
                max(0.0, recovered.timestamp - recovery_start)
                if recovered and recovery_start is not None
                else None
            )
            start_metadata = start.metadata if start else {}
            terminal_metadata = terminal.metadata if terminal else {}
            failure_event = cls._latest(turn_events, "TURN_FAILED")
            status = "running"
            result = ""
            if terminal:
                status = {
                    "TURN_COMPLETED": "completed",
                    "TURN_FAILED": "failed",
                    "TURN_ABORTED": "aborted",
                }[terminal.kind]
                result = status
            confidence = min((item.confidence for item in turn_events), default=Confidence.LOW)
            source = start.source if start else terminal.source if terminal else ""
            results.append(
                TurnSummary(
                    turn_id=turn_id,
                    started_at=(
                        float(start_metadata["started_at"])
                        if isinstance(start_metadata.get("started_at"), (int, float))
                        else start.timestamp
                        if start
                        else None
                    ),
                    completed_at=(
                        float(terminal_metadata["completed_at"])
                        if isinstance(terminal_metadata.get("completed_at"), (int, float))
                        else terminal.timestamp
                        if terminal
                        else None
                    ),
                    duration_seconds=duration,
                    time_to_first_token_seconds=(
                        float(start_metadata["time_to_first_token_seconds"])
                        if isinstance(
                            start_metadata.get("time_to_first_token_seconds"), (int, float)
                        )
                        else max(0.0, first_token.timestamp - start.timestamp)
                        if start and first_token
                        else None
                    ),
                    status=status,
                    result=result,
                    model=str(start_metadata.get("model") or process.model),
                    reasoning_effort=str(
                        start_metadata.get("reasoning_effort") or process.reasoning_effort
                    ),
                    collaboration_mode=str(start_metadata.get("collaboration_mode") or ""),
                    trace_id=str(start_metadata.get("trace_id") or ""),
                    token_usage=cls._token_summary(token_event),
                    tool_count=len(turn_tools),
                    tool_duration_seconds=(
                        sum(known_tool_durations) if known_tool_durations else None
                    ),
                    longest_tool=longest,
                    reconnect_count=len(reconnects),
                    fallback_count=len(fallbacks),
                    recovery_duration_seconds=recovery_duration,
                    compact_count=sum(item.kind == "COMPACT_COMPLETED" for item in turn_events),
                    failure=failure_event.failure if failure_event else None,
                    tools=tuple(turn_tools),
                    provenance=Provenance(
                        source,
                        confidence,
                        derived=duration_derived
                        or any(item.provenance.derived for item in turn_tools),
                        complete=start is not None and terminal is not None,
                    ),
                )
            )
        return sorted(results, key=lambda item: item.started_at or item.completed_at or 0.0)

    @staticmethod
    def _agent_tree(events: list[NormalizedEvent]) -> list[AgentNode]:
        nodes: dict[str, AgentNode] = {}
        parents: dict[str, str] = {}
        action_starts: dict[tuple[str, str], float] = {}
        action_status = {
            "AGENT_SPAWN_STARTED": "pending",
            "AGENT_SPAWNED": "running",
            "AGENT_RESUMED": "running",
            "AGENT_CLOSED": "shutdown",
        }
        for event in events:
            if not event.kind.startswith("AGENT_"):
                continue
            metadata = event.metadata
            sender = str(metadata.get("sender_thread_id") or "")
            receivers = metadata.get("receiver_thread_ids")
            receivers = receivers if isinstance(receivers, list) else []
            if not receivers and sender:
                receivers = [sender]
                sender = ""
            for thread_id in (str(item) for item in receivers if item):
                node = nodes.get(thread_id)
                if node is None:
                    node = AgentNode(
                        thread_id=thread_id,
                        parent_thread_id=sender,
                        spawned_at=event.timestamp,
                        provenance=event.provenance,
                    )
                    nodes[thread_id] = node
                if sender and sender != thread_id:
                    node.parent_thread_id = sender
                    parents[thread_id] = sender
                node.agent_path = str(metadata.get("agent_path") or node.agent_path)
                node.nickname = str(metadata.get("nickname") or node.nickname)
                node.role = str(metadata.get("role") or node.role)
                node.model = str(metadata.get("model") or node.model)
                node.reasoning_effort = str(
                    metadata.get("reasoning_effort") or node.reasoning_effort
                )
                node.updated_at = event.timestamp
                status = str(metadata.get("status") or action_status.get(event.kind, ""))
                if status:
                    node.status = status.lower()
                if event.kind == "AGENT_INTERACTION_COMPLETED":
                    node.interaction_count += 1
                elif event.kind == "AGENT_WAIT_COMPLETED":
                    node.wait_count += 1
                elif event.kind == "AGENT_RESUMED":
                    node.resume_count += 1
                elif event.kind == "AGENT_CLOSED":
                    node.close_count += 1
                action = ""
                for candidate in ("interaction", "wait", "resume", "close"):
                    if candidate.upper() in event.kind:
                        action = candidate
                        break
                if action and event.kind.endswith("STARTED"):
                    action_starts[(thread_id, action)] = event.timestamp
                elif action and event.kind in {
                    "AGENT_INTERACTION_COMPLETED",
                    "AGENT_WAIT_COMPLETED",
                    "AGENT_RESUMED",
                    "AGENT_CLOSED",
                }:
                    direct_duration = metadata.get("duration_seconds")
                    if isinstance(direct_duration, (int, float)):
                        action_duration = float(direct_duration)
                    else:
                        action_start = action_starts.pop((thread_id, action), None)
                        action_duration = (
                            max(0.0, event.timestamp - action_start)
                            if action_start is not None
                            else 0.0
                        )
                    current = getattr(node, f"{action}_seconds")
                    setattr(node, f"{action}_seconds", current + action_duration)
                error = str(metadata.get("error") or "")
                if error or node.status in {"error", "errored", "failed"}:
                    node.error = FailureInfo(
                        "subagent_error",
                        error or "subagent errored",
                        turn_id=event.turn_id,
                        timestamp=event.timestamp,
                        source=event.source,
                    )
        for node in nodes.values():
            node.children.clear()
        roots: list[AgentNode] = []
        for thread_id, node in nodes.items():
            parent = nodes.get(parents.get(thread_id, ""))
            if parent and parent is not node:
                parent.children.append(node)
            else:
                roots.append(node)
        for node in nodes.values():
            node.children.sort(key=lambda item: (item.spawned_at or 0.0, item.thread_id))
        return sorted(roots, key=lambda item: (item.spawned_at or 0.0, item.thread_id))

    @staticmethod
    def _capabilities(events: list[NormalizedEvent]) -> ProtocolCapabilities:
        def direct(source: str = "rollout") -> CapabilityStatus:
            return CapabilityStatus(CapabilityMode.DIRECT, source, Confidence.HIGH)

        def derived(source: str = "rollout") -> CapabilityStatus:
            return CapabilityStatus(CapabilityMode.DERIVED, source, Confidence.HIGH)

        unavailable = CapabilityStatus()
        turn_events = [item for item in events if item.kind in {"TURN_STARTED", *TERMINAL_KINDS}]
        tool_events = [item for item in events if item.kind in {"TOOL_RUNNING", "TOOL_COMPLETED"}]
        agent_events = [item for item in events if item.kind.startswith("AGENT_")]
        return ProtocolCapabilities(
            turn_timing=(
                direct()
                if any("duration_seconds" in item.metadata for item in turn_events)
                else derived()
                if turn_events
                else unavailable
            ),
            item_timing=direct()
            if any(item.kind in {"ITEM_STARTED", "ITEM_COMPLETED"} for item in events)
            else unavailable,
            tool_timing=(
                direct()
                if any("duration_seconds" in item.metadata for item in tool_events)
                else derived()
                if tool_events
                else unavailable
            ),
            token_usage=direct()
            if any(item.kind == "TOKEN_USAGE" for item in events)
            else unavailable,
            rate_limits=direct()
            if any(item.kind == "RATE_LIMIT" or item.metadata.get("rate_limits") for item in events)
            else unavailable,
            collab_status=direct() if agent_events else unavailable,
            subagent_path=direct()
            if any(item.metadata.get("agent_path") for item in agent_events)
            else unavailable,
            action_required=direct()
            if any(item.kind == "ACTION_REQUIRED" for item in events)
            else unavailable,
        )

    @staticmethod
    def _operation_summary(
        state: SessionHealth,
        events: list[NormalizedEvent],
    ) -> CurrentOperationSummary:
        if state.attention_request:
            request = state.attention_request
            return CurrentOperationSummary(
                "attention",
                request.summary,
                request.detail,
                request.started_at,
                provenance=request.provenance,
            )
        if state.current_failure:
            failure = state.current_failure
            return CurrentOperationSummary(
                "failure",
                "失败",
                failure.message,
                failure.timestamp,
                provenance=Provenance(failure.source, Confidence.HIGH),
            )
        active_compaction = next(
            (
                item
                for item in reversed(state.compactions)
                if item.status in {"requested", "candidate", "running"}
            ),
            None,
        )
        if active_compaction:
            status = {
                "requested": "compact requested",
                "candidate": "compact candidate",
                "running": "compact running",
            }[active_compaction.status]
            return CurrentOperationSummary(
                "compact",
                status,
                active_compaction.trigger,
                active_compaction.requested_at or active_compaction.started_at,
                provenance=Provenance(
                    active_compaction.source or "state-machine",
                    active_compaction.confidence,
                    derived=active_compaction.reconstructed,
                ),
            )
        if state.recovery != RecoveryState.NONE or state.network.state.value in {
            "SUSPECT",
            "STALLED",
        }:
            recovery = SessionStateMachine._latest(
                events, "RECONNECTING", "TRANSPORT_FALLBACK", "RECOVERED"
            )
            detail = state.network.reason or (recovery.detail if recovery else "")
            return CurrentOperationSummary(
                "recovery",
                state.phase,
                detail,
                recovery.timestamp if recovery else state.phase_since,
                provenance=(
                    recovery.provenance
                    if recovery
                    else Provenance("socket-classifier", Confidence.MEDIUM, derived=True)
                ),
            )
        running = next(
            (tool for tool in reversed(state.tool_executions) if tool.status == "running"),
            None,
        )
        if running:
            detail = running.command or running.display_name
            if detail:
                detail = " ".join(detail.split())
                if len(detail) > 96:
                    detail = detail[:95] + "…"
            return CurrentOperationSummary(
                running.category or "tool",
                running.display_name,
                detail,
                running.started_at,
                tool_count=sum(tool.status == "running" for tool in state.tool_executions),
                file_count=len(running.files),
                provenance=running.provenance,
            )
        file_event = SessionStateMachine._latest(
            events, "FILE_CHANGE_APPLIED", "FILE_CHANGE_FAILED"
        )
        if (
            file_event
            and state.lifecycle in {LifecycleState.GENERATING, LifecycleState.RUNNING_TOOL}
            and state.phase_since == file_event.timestamp
        ):
            files = file_event.metadata.get("files")
            files = files if isinstance(files, list) else []
            return CurrentOperationSummary(
                "write",
                file_event.summary,
                file_event.detail,
                file_event.timestamp,
                file_count=len(files),
                provenance=file_event.provenance,
            )
        all_agents: list[AgentNode] = []
        pending_agents = list(state.agents)
        while pending_agents:
            agent = pending_agents.pop()
            all_agents.append(agent)
            pending_agents.extend(agent.children)
        active_agent = next(
            (
                agent
                for agent in reversed(all_agents)
                if agent.status not in {"completed", "closed", "failed", "error"}
            ),
            None,
        )
        if active_agent:
            return CurrentOperationSummary(
                "agent",
                "Subagent",
                active_agent.role or active_agent.nickname or active_agent.agent_path,
                active_agent.spawned_at,
                agent=active_agent.nickname or active_agent.agent_path,
                provenance=active_agent.provenance,
            )
        return CurrentOperationSummary(
            state.lifecycle.value.lower(),
            state.phase,
            "",
            state.phase_since,
            provenance=Provenance("state-machine", Confidence.MEDIUM, derived=True),
        )

    @staticmethod
    def _diagnosis_findings(
        state: SessionHealth,
        events: list[NormalizedEvent],
        now: float,
    ) -> list[DiagnosisFinding]:
        operation = state.current_operation
        evidence_event = SessionStateMachine._latest(
            events,
            "ACTION_REQUIRED",
            "TURN_FAILED",
            "RECONNECTING",
            "TRANSPORT_FALLBACK",
            "RECOVERED",
            "TOOL_RUNNING",
            "MODEL_PROGRESS",
            "REQUEST_SENT",
        )
        freshness = None
        if evidence_event:
            freshness = max(0.0, now - (evidence_event.observed_at or evidence_event.timestamp))
        severity = "info"
        action = ""
        if state.attention_request:
            severity = "warning"
            action = "切换到对应 Codex 会话完成交互"
        elif state.current_failure or state.network.state.value == "STALLED":
            severity = "error"
        elif state.recovery not in {RecoveryState.NONE, RecoveryState.RECOVERED}:
            severity = "warning"
        reason = operation.detail or state.network.reason or state.phase
        evidence = []
        if evidence_event:
            evidence.append(f"{evidence_event.kind} · {evidence_event.source}")
        if state.network.reason:
            evidence.append(f"TCP · {state.network.reason}")
        findings = [
            DiagnosisFinding(
                severity,
                operation.label,
                reason,
                tuple(evidence),
                operation.provenance,
                freshness,
                action,
            )
        ]
        if state.clock_uncertain:
            assessment = state.clock_assessments[-1]
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    "事件时钟存在偏差",
                    (
                        f"source={assessment.source}; domain={assessment.clock_domain}; "
                        f"reason={assessment.reason}; "
                        f"adjudicated_at={assessment.adjudicated_at:.6f}"
                    ),
                    (
                        f"source_timestamp={assessment.source_timestamp:.6f}",
                        f"observed_at={assessment.observed_at}",
                    ),
                    Provenance(
                        assessment.source,
                        Confidence.LOW,
                        derived=True,
                        complete=False,
                    ),
                ),
            )
        if state.protocol_uncertain:
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    state.phase,
                    state.protocol_uncertainty_reason,
                    (f"UNPARSED_PAYLOAD · {state.protocol_uncertainty_scope}",),
                    state.lifecycle_provenance,
                    freshness,
                    "检查协议兼容性与原始来源形状",
                ),
            )
        if state.silence.state != SilenceState.NORMAL:
            findings.append(
                DiagnosisFinding(
                    state.silence.severity,
                    state.silence.state.value,
                    state.silence.reason,
                    tuple(
                        item
                        for item in (
                            f"last semantic · {state.observation.last_semantic_kind}"
                            if state.observation.last_semantic_kind
                            else "",
                            f"last evidence · {state.observation.last_evidence_source}"
                            if state.observation.last_evidence_source
                            else "",
                        )
                        if item
                    ),
                    state.silence.provenance,
                    (
                        max(0.0, now - state.observation.last_evidence_at)
                        if state.observation.last_evidence_at is not None
                        else None
                    ),
                )
            )
        if state.observation.auto_compact_expected:
            findings.append(
                DiagnosisFinding(
                    "info",
                    "AUTO_COMPACT_EXPECTED",
                    state.observation.auto_compact_reason,
                    provenance=Provenance(
                        "config.toml+token_usage",
                        Confidence.MEDIUM,
                        derived=True,
                    ),
                )
            )
        if state.event_telemetry.dedupe_filter_degraded:
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    "事件去重记忆接近饱和",
                    (
                        f"fill={state.event_telemetry.dedupe_filter_fill_ratio:.1%}; "
                        f"matches={state.event_telemetry.dedupe_filter_matches}; "
                        "新证据可能因固定容量过滤器命中而被丢弃"
                    ),
                    (
                        f"bits_set={state.event_telemetry.dedupe_filter_bits_set}",
                        f"capacity={state.event_telemetry.dedupe_filter_capacity_bits}",
                        "reason=dedupe_filter_saturated_fail_closed",
                    ),
                    Provenance("state-machine", Confidence.LOW, derived=True, complete=False),
                ),
            )
        generation_dropped = (
            state.event_telemetry.stale_stream_generation_dropped
            + state.event_telemetry.stream_identity_limit_dropped
        )
        if generation_dropped:
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    "可变证据流 generation 已降级",
                    (
                        f"stale_generation_dropped="
                        f"{state.event_telemetry.stale_stream_generation_dropped}; "
                        f"identity_limit_dropped="
                        f"{state.event_telemetry.stream_identity_limit_dropped}"
                    ),
                    (
                        f"generation_advances={state.event_telemetry.stream_generation_advances}",
                        "reason=mutable_stream_generation_guard",
                    ),
                    Provenance("state-machine", Confidence.LOW, derived=True, complete=False),
                ),
            )
        hook_event = next(
            (
                event
                for event in reversed(events)
                if event.source == "compact_hook"
                and event.kind
                in {
                    "COMPACTING",
                    "COMPACT_COMPLETED",
                    "COMPACT_FAILED",
                    "COMPACT_ABORTED",
                }
            ),
            None,
        )
        if hook_event and hook_event.source_authenticity != Confidence.HIGH:
            rollout_event = next(
                (
                    event
                    for event in reversed(events)
                    if event.source == "rollout"
                    and event.kind
                    in {"COMPACTING", "COMPACT_COMPLETED", "COMPACT_FAILED", "COMPACT_ABORTED"}
                ),
                None,
            )
            terminal_kinds = {"COMPACT_COMPLETED", "COMPACT_FAILED", "COMPACT_ABORTED"}
            conflict = bool(
                rollout_event
                and (
                    (rollout_event.kind in terminal_kinds) != (hook_event.kind in terminal_kinds)
                    or (
                        rollout_event.kind in terminal_kinds
                        and hook_event.kind in terminal_kinds
                        and rollout_event.kind != hook_event.kind
                    )
                )
            )
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    ("Hook 证据与 rollout 冲突" if conflict else "Hook 来源未认证"),
                    (
                        f"parse={hook_event.parse_validity.value}; "
                        f"authenticity={hook_event.source_authenticity.value}; "
                        f"binding={hook_event.identity_binding.value}; "
                        f"semantic={hook_event.semantic_confidence.value}"
                    ),
                    hook_event.binding_evidence,
                    Provenance(
                        "compact_hook",
                        Confidence.LOW,
                        derived=True,
                        complete=False,
                    ),
                ),
            )
        incomplete = [
            item
            for item in (
                state.completeness.lifecycle,
                state.completeness.attention,
                state.completeness.failure_recovery,
                state.completeness.terminal_ownership,
                state.completeness.network,
                state.completeness.silence,
            )
            if not item.complete
        ]
        if incomplete:
            findings.insert(
                0,
                DiagnosisFinding(
                    "warning",
                    "状态轴证据不完整",
                    "; ".join(f"{item.axis}={item.reason}" for item in incomplete),
                    tuple(
                        f"{item.axis}:{evidence}"
                        for item in incomplete
                        for evidence in item.evidence
                    )[:24],
                    Provenance(
                        "evidence-completeness",
                        Confidence.LOW,
                        derived=True,
                        complete=False,
                    ),
                    action="等待对应轴的可信正证据或清除基线",
                ),
            )
        return findings

    def _event_telemetry(
        self,
        key: str | SessionIdentity,
        events: list[NormalizedEvent],
    ) -> EventTelemetrySummary:
        delays = sorted(
            event.freshness_seconds for event in events if event.freshness_seconds is not None
        )

        def percentile(fraction: float) -> float | None:
            if not delays:
                return None
            position = (len(delays) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(delays) - 1)
            weight = position - lower
            return delays[lower] * (1.0 - weight) + delays[upper] * weight

        unparsed = sum(event.kind == "UNPARSED_PAYLOAD" for event in events)
        dedupe = self.seen.get(key)
        return EventTelemetrySummary(
            total_events=len(events),
            observed_events=len(delays),
            unparsed_events=unparsed,
            unknown_rate=unparsed / len(events) if events else 0.0,
            observation_p50_seconds=percentile(0.50),
            observation_p95_seconds=percentile(0.95),
            dedupe_filter_bits_set=len(dedupe) if dedupe is not None else 0,
            dedupe_filter_capacity_bits=dedupe.bit_count if dedupe is not None else 0,
            dedupe_filter_fill_ratio=dedupe.fill_ratio if dedupe is not None else 0.0,
            dedupe_filter_matches=self.dedupe_matches.get(key, 0),
            dedupe_filter_degraded_drops=self.dedupe_degraded_drops.get(key, 0),
            dedupe_filter_degraded=dedupe.degraded if dedupe is not None else False,
            stale_stream_generation_dropped=self.stale_stream_generation_dropped.get(key, 0),
            stream_identity_limit_dropped=self.stream_identity_limit_dropped.get(key, 0),
            stream_generation_advances=self.stream_generation_advances.get(key, 0),
        )

    @staticmethod
    def _event_evidence_time(event: NormalizedEvent) -> float:
        return event.observed_at or event.adjudicated_at or event.timestamp

    def _axis_after_gap(
        self,
        key: str | SessionIdentity,
        axis: str,
        events: list[NormalizedEvent],
        baseline_kinds: set[str],
    ) -> AxisCompleteness:
        if self.coverage_backlog.get(key, False):
            return AxisCompleteness(
                axis,
                complete=False,
                confidence=Confidence.LOW,
                reason="仍有未处理的证据积压，否定结论暂不成立",
                baseline_kind="backlog_pending",
                evidence=("ingress_backlog",),
            )
        gap_at = self.coverage_gap_at.get(key)
        if gap_at is None:
            return AxisCompleteness(axis)
        baseline = next(
            (
                event
                for event in reversed(events)
                if event.kind in baseline_kinds and self._event_evidence_time(event) >= gap_at
            ),
            None,
        )
        if baseline is not None:
            return AxisCompleteness(
                axis,
                complete=True,
                confidence=baseline.confidence,
                reason=f"缺口后观察到 {baseline.kind}，已建立该轴基线",
                baseline_kind=baseline.kind,
                baseline_at=self._event_evidence_time(baseline),
                evidence=(f"{baseline.kind}:{baseline.source}",),
            )
        reasons = self.coverage_gap_reasons.get(key, ("evidence_gap",))
        return AxisCompleteness(
            axis,
            complete=False,
            confidence=Confidence.LOW,
            reason="历史证据存在缺口，且缺口后没有该轴的可信正证据或清除基线",
            baseline_kind="missing_after_gap",
            baseline_at=gap_at,
            evidence=reasons,
        )

    def _session_completeness(
        self,
        key: str | SessionIdentity,
        events: list[NormalizedEvent],
    ) -> SessionCompleteness:
        lifecycle = self._axis_after_gap(
            key, "lifecycle", events, AXIS_BASELINE_KINDS["lifecycle"]
        )
        attention = self._axis_after_gap(
            key, "attention", events, AXIS_BASELINE_KINDS["attention"]
        )
        failure_recovery = self._axis_after_gap(
            key, "failure_recovery", events, AXIS_BASELINE_KINDS["failure_recovery"]
        )
        terminal_probe_at = self.terminal_probe_complete_at.get(key)
        terminal_probe_complete = self.terminal_probe_complete.get(key)
        gap_at = self.coverage_gap_at.get(key)
        terminal_probe_is_baseline = terminal_probe_at is not None and (
            gap_at is None or terminal_probe_at >= gap_at
        )
        if terminal_probe_complete is False:
            terminal_ownership = AxisCompleteness(
                "terminal_ownership",
                complete=False,
                confidence=Confidence.LOW,
                reason="进程树或 terminal 关联探针不完整，ownership 缺失不能视为 absent",
                baseline_kind="terminal_probe_incomplete",
                evidence=("process_tree", "terminal_association"),
            )
        elif terminal_probe_is_baseline and not self.coverage_backlog.get(key, False):
            terminal_ownership = AxisCompleteness(
                "terminal_ownership",
                reason="完整进程树与 terminal 关联探针已覆盖当前活动",
                baseline_kind="process_tree_probe",
                baseline_at=terminal_probe_at,
                evidence=("process_tree", "terminal_association"),
            )
        else:
            terminal_ownership = self._axis_after_gap(
                key, "terminal_ownership", events, AXIS_BASELINE_KINDS["terminal_ownership"]
            )
        network_complete = self.network_probe_complete.get(key, True)
        network = AxisCompleteness(
            "network",
            complete=network_complete,
            confidence=Confidence.HIGH if network_complete else Confidence.LOW,
            reason=(
                "当前 socket 探针完整"
                if network_complete
                else "socket 探针陈旧或不完整，closed/healthy 结论暂不成立"
            ),
            baseline_kind="socket_probe" if network_complete else "socket_probe_incomplete",
            evidence=("socket_snapshot",),
        )
        silence_probe_complete = self.silence_probe_complete.get(key, True)
        if not silence_probe_complete:
            silence = AxisCompleteness(
                "silence",
                complete=False,
                confidence=Confidence.LOW,
                reason="process/rollout/network 探针不完整，静默分类暂不成立",
                baseline_kind="observation_probe_incomplete",
                evidence=("observation_probe",),
            )
        elif not lifecycle.complete:
            silence = AxisCompleteness(
                "silence",
                complete=False,
                confidence=Confidence.LOW,
                reason="lifecycle 轴尚未建立缺口后的基线，静默分类缺少阶段上下文",
                baseline_kind="lifecycle_incomplete",
                evidence=("lifecycle",),
            )
        else:
            silence = AxisCompleteness(
                "silence",
                confidence=lifecycle.confidence,
                reason="当前观察探针与 lifecycle 基线均完整",
                baseline_kind="observation_probes",
                baseline_at=lifecycle.baseline_at,
                evidence=("process", "rollout", "network", "lifecycle"),
            )
        return SessionCompleteness(
            lifecycle=lifecycle,
            attention=attention,
            failure_recovery=failure_recovery,
            terminal_ownership=terminal_ownership,
            network=network,
            silence=silence,
        )

    def _apply_completeness(
        self,
        key: str | SessionIdentity,
        state: SessionHealth,
        events: list[NormalizedEvent],
    ) -> None:
        completeness = self._session_completeness(key, events)
        if state.observation.collector_stale:
            completeness = replace(
                completeness,
                silence=AxisCompleteness(
                    "silence",
                    confidence=Confidence.HIGH,
                    reason="collector 陈旧已明确建立 observer-blind 结论",
                    baseline_kind="collector_stale",
                    baseline_at=state.observation.sampled_at,
                    evidence=("collector_health",),
                ),
            )
        state.completeness = completeness
        if not completeness.lifecycle.complete:
            state.lifecycle_confidence = Confidence.LOW
            state.lifecycle_provenance = Provenance(
                "evidence-completeness",
                Confidence.LOW,
                derived=True,
                complete=False,
            )
        if not completeness.attention.complete:
            state.attention_confidence = Confidence.LOW
            state.attention_provenance = Provenance(
                "evidence-completeness",
                Confidence.LOW,
                derived=True,
                complete=False,
            )

    def derive(
        self,
        key: str | SessionIdentity,
        process: ProcessInfo,
        network: NetworkEvidence,
        now: float | None = None,
        observation: ObservationPulse | None = None,
    ) -> SessionHealth:
        now = time.time() if now is None else now
        all_events = self.events.get(key, [])
        decision_events = {
            event.source_id: event
            for event in (*all_events, *self.axis_baselines.get(key, {}).values())
        }
        decision_context = sorted(
            decision_events.values(), key=lambda event: (event.timestamp, event.source_id)
        )
        authoritative_events = [
            event
            for event in decision_context
            if not (
                event.source == "compact_hook"
                and (
                    event.source_authenticity != Confidence.HIGH
                    or event.identity_binding != Confidence.HIGH
                )
            )
        ]
        visible_cutoff = now - self.lookback_seconds
        visible = [event for event in all_events if event.timestamp >= visible_cutoff]
        model_config = self._latest(all_events, "MODEL_CONFIG")
        if model_config:
            model = str(model_config.metadata.get("model") or "")
            reasoning_effort = str(model_config.metadata.get("reasoning_effort") or "")
            if model or reasoning_effort:
                process = replace(
                    process,
                    model=model or process.model,
                    reasoning_effort=reasoning_effort or process.reasoning_effort,
                )
        state = SessionHealth(
            process.instance_id,
            process.session_id,
            process,
            network=network,
            events=visible,
            identity=key if isinstance(key, SessionIdentity) else None,
        )
        state.clock_assessments = self._clock_assessments(all_events)
        state.clock_uncertain = bool(state.clock_assessments)
        if not all_events:
            state.observation = self._finalize_observation(
                state, all_events, observation or ObservationPulse(), now
            )
            self._apply_completeness(key, state, authoritative_events)
            state.silence = self._silence_assessment(state, now)
            state.current_operation = self._operation_summary(state, all_events)
            state.diagnosis = self._diagnosis_findings(state, all_events, now)
            self._reconcile_alert(key, state, now)
            return state

        latest_failure = self._latest(authoritative_events, "TURN_FAILED", "COMPACT_FAILED")
        state.latest_failure = latest_failure.failure if latest_failure else None

        process_resume = self._latest(authoritative_events, "PROCESS_RESUMED")
        state_events = [
            event
            for event in authoritative_events
            if not process_resume or event.timestamp >= process_resume.timestamp
        ]
        task_start = self._latest(state_events, "TURN_STARTED")
        task_terminal = self._latest(state_events, *TERMINAL_KINDS)
        latest_active = self._latest(state_events, *CURRENT_TURN_KINDS)
        if task_start:
            current_turn = not task_terminal or task_start.timestamp > task_terminal.timestamp
        else:
            current_turn = bool(
                latest_active
                and (not task_terminal or latest_active.timestamp > task_terminal.timestamp)
            )
        relevant = [
            event
            for event in state_events
            if not task_start or event.timestamp >= task_start.timestamp
        ]
        lifecycle_event = self._latest(relevant, *LIFECYCLE_PHASE_KINDS)
        compact_start = self._latest(relevant, "COMPACTING")
        compact_end = self._latest(
            relevant,
            "COMPACT_COMPLETED",
            "COMPACT_FAILED",
            "COMPACT_ABORTED",
            *TERMINAL_KINDS,
        )
        compacting = bool(
            compact_start
            and (compact_end is None or compact_start.timestamp > compact_end.timestamp)
        )
        failure_event = self._latest(relevant, "TURN_FAILED", "COMPACT_FAILED")
        compact_abort = self._latest(relevant, "COMPACT_ABORTED")
        attention_event = self._latest(relevant, "ACTION_REQUIRED")
        attention_clear = self._latest(relevant, *ATTENTION_CLEAR_KINDS)
        if attention_event and (
            attention_clear is None or attention_event.timestamp > attention_clear.timestamp
        ):
            attention_name = str(attention_event.metadata.get("attention_state") or "USER_INPUT")
            try:
                state.attention = AttentionState(attention_name)
            except ValueError:
                state.attention = AttentionState.USER_INPUT
            state.attention_request = AttentionRequest(
                state=state.attention,
                request_id=str(attention_event.metadata.get("request_id") or ""),
                call_id=str(attention_event.metadata.get("call_id") or ""),
                turn_id=attention_event.turn_id,
                summary=attention_event.summary,
                detail=attention_event.detail,
                started_at=attention_event.timestamp,
                observed_at=attention_event.observed_at,
                provenance=attention_event.provenance,
            )
            state.attention_confidence = attention_event.confidence
            state.attention_provenance = attention_event.provenance
        process_exit = self._latest(authoritative_events, "PROCESS_EXITED", "SESSION_CLOSED")
        if process_exit and process_exit is authoritative_events[-1]:
            # Process termination is historical lifecycle evidence, not a turn failure.
            current_turn = False
            state.process_exited = True
            state.process_exited_at = process_exit.timestamp

        if not current_turn and task_terminal:
            if task_terminal.kind == "TURN_FAILED":
                state.lifecycle = LifecycleState.FAILED
                state.current_failure = task_terminal.failure
            elif task_terminal.kind == "TURN_ABORTED":
                state.lifecycle = LifecycleState.ABORTED
            else:
                state.lifecycle = LifecycleState.COMPLETED
        elif failure_event and (not task_start or failure_event.timestamp >= task_start.timestamp):
            state.lifecycle = LifecycleState.FAILED
            state.current_failure = failure_event.failure
        elif compact_abort and (not task_start or compact_abort.timestamp >= task_start.timestamp):
            state.lifecycle = LifecycleState.ABORTED
        elif current_turn and lifecycle_event:
            if compacting:
                state.lifecycle = LifecycleState.COMPACTING
            elif lifecycle_event.kind == "TOOL_RUNNING":
                state.lifecycle = LifecycleState.RUNNING_TOOL
            elif lifecycle_event.kind in {
                "MODEL_PROGRESS",
                "REASONING_SUMMARY",
                "PLAN_UPDATED",
                "RESPONSE_STARTED",
                "TOOL_COMPLETED",
                "FILE_CHANGE_APPLIED",
                "FILE_CHANGE_FAILED",
                "COMPACT_COMPLETED",
            }:
                state.lifecycle = LifecycleState.GENERATING
            elif lifecycle_event.kind == "REQUEST_SENT":
                state.lifecycle = LifecycleState.WAITING_RESPONSE
            else:
                state.lifecycle = LifecycleState.STARTING

        if state.process_exited:
            state.lifecycle = LifecycleState.IDLE
            state.current_failure = None

        recovery_events = relevant
        if task_terminal and not current_turn:
            recovery_events = [
                event for event in relevant if event.timestamp > task_terminal.timestamp
            ]
        reconnect = self._latest(recovery_events, "RECONNECTING")
        fallback = self._latest(recovery_events, "TRANSPORT_FALLBACK")
        recovered = self._latest(recovery_events, "RECOVERED")
        recovery_candidates = [event for event in (reconnect, fallback, recovered) if event]
        if recovery_candidates:
            latest_recovery = max(recovery_candidates, key=lambda event: event.timestamp)
            state.recovery = RecoveryState(latest_recovery.kind)
        if network.state.value == "SUSPECT" and state.recovery == RecoveryState.NONE:
            state.recovery = RecoveryState.SUSPECT

        phase_event: NormalizedEvent | None
        if state.process_exited and process_exit:
            phase_event = process_exit
        elif task_terminal and not current_turn:
            phase_event = task_terminal
        elif compacting:
            phase_event = compact_start
        else:
            phase_event = self._latest(relevant, *DISPLAY_PHASE_KINDS)
        if phase_event:
            state.phase = EVENT_LABELS.get(
                phase_event.kind, LIFECYCLE_LABELS[state.lifecycle.value]
            )
            state.phase_since = phase_event.timestamp
            state.lifecycle_confidence = phase_event.confidence
            state.lifecycle_provenance = phase_event.provenance
        else:
            state.phase = LIFECYCLE_LABELS[state.lifecycle.value]
        semantic_evidence = [
            event
            for event in relevant
            if (event.kind != "UNPARSED_PAYLOAD" and event.kind not in NON_SEMANTIC_KINDS)
            or (
                event.kind == "UNPARSED_PAYLOAD"
                and event.metadata.get("semantic_scope") != "auxiliary"
            )
        ]
        latest_semantic = semantic_evidence[-1] if semantic_evidence else None
        if latest_semantic and latest_semantic.kind == "UNPARSED_PAYLOAD":
            scope = str(latest_semantic.metadata.get("semantic_scope") or "lifecycle")
            previous_phase = state.phase
            source_type = (
                latest_semantic.unparsed.source_type
                if latest_semantic.unparsed is not None
                else "unknown"
            )
            state.protocol_uncertain = True
            state.protocol_uncertainty_scope = scope
            state.protocol_uncertainty_reason = (
                f"最新协议记录 {source_type} 可能改变 {scope}；上一结论为 {previous_phase}"
            )
            state.lifecycle_confidence = Confidence.LOW
            state.lifecycle_provenance = latest_semantic.provenance
            if scope == "attention":
                state.attention_confidence = Confidence.LOW
                state.attention_provenance = latest_semantic.provenance
                state.phase = "协议不确定（可能等待交互）"
            else:
                state.phase = "协议状态不确定"
        token_event = self._latest(all_events, "TOKEN_USAGE")
        rate_event = self._latest(all_events, "TOKEN_USAGE", "RATE_LIMIT")
        state.token_used, state.token_limit = self._tokens(token_event)
        state.token_usage = self._token_summary(token_event)
        state.cumulative_token_usage = self._token_summary(token_event, cumulative=True)
        if state.token_usage:
            state.token_used = state.token_usage.context_tokens or state.token_usage.total_tokens
            state.token_limit = state.token_usage.context_window
        state.rate_limits = self._rate_limits(rate_event)
        state.tool_executions = self._tool_summaries(all_events)
        state.turns = self._turn_summaries(all_events, state.tool_executions, process)
        state.compactions = deepcopy(self.compactions.get(key, []))
        state.agents = self._agent_tree(all_events)
        state.protocol_capabilities = self._capabilities(all_events)
        state.event_telemetry = self._event_telemetry(key, all_events)
        state.observation = self._finalize_observation(
            state, all_events, observation or ObservationPulse(), now
        )
        self._apply_completeness(key, state, authoritative_events)
        state.silence = self._silence_assessment(state, now)
        state.current_operation = self._operation_summary(state, all_events)
        state.diagnosis = self._diagnosis_findings(state, all_events, now)

        if current_turn:
            self._derive_alert(state, relevant, now)
        if (
            state.silence.state == SilenceState.STALL_SUSPECT
            and state.silence.severity == "severe"
            and not state.alert
        ):
            state.alert = "SILENCE_STALL"
            state.alert_level = "严重"
            state.alert_reason = state.silence.reason
            state.alert_age_seconds = max(
                0,
                int(now - (state.observation.last_semantic_at or now)),
            )
        agent_errors = []
        pending_agents = list(state.agents)
        while pending_agents:
            agent = pending_agents.pop()
            if agent.error:
                agent_errors.append(agent.error)
            pending_agents.extend(agent.children)
        if agent_errors and not state.alert:
            state.alert = "SUBAGENT_ERROR"
            state.alert_level = "警告"
            state.alert_reason = agent_errors[-1].message
            state.alert_age_seconds = max(0, int(now - agent_errors[-1].timestamp))
        self._reconcile_alert(key, state, now)
        return state

    @classmethod
    def _finalize_observation(
        cls,
        state: SessionHealth,
        events: list[NormalizedEvent],
        pulse: ObservationPulse,
        now: float,
    ) -> ObservationPulse:
        semantic = next(
            (event for event in reversed(events) if event.kind not in NON_SEMANTIC_KINDS),
            None,
        )
        turn_start = cls._latest(events, "TURN_STARTED")
        token_event = cls._latest(events, "TOKEN_USAGE")
        auto_limit = cls._int(
            token_event.metadata.get("auto_compact_token_limit") if token_event else None
        )
        auto_expected = bool(
            auto_limit is not None
            and state.token_used is not None
            and state.token_used >= auto_limit
        )
        last_semantic_at = pulse.last_semantic_at
        last_semantic_kind = pulse.last_semantic_kind
        last_semantic_source = pulse.last_semantic_source
        if semantic and (last_semantic_at is None or semantic.timestamp >= last_semantic_at):
            last_semantic_at = semantic.timestamp
            last_semantic_kind = semantic.kind
            last_semantic_source = semantic.source
        evidence = [
            (
                last_semantic_at,
                last_semantic_source or "protocol",
                EVENT_LABELS.get(last_semantic_kind, last_semantic_kind),
            ),
            (pulse.last_rollout_growth_at, "rollout", "rollout 文件增长"),
            (pulse.last_process_activity_at, "process", pulse.process_activity.detail),
            (pulse.last_network_progress_at, "network", "TCP RX/TX/ACK 有进展"),
            (pulse.last_log_activity_at, "log", "structured log 有新记录"),
        ]
        available: list[tuple[float, str, str]] = [
            (float(observed_at), source, detail)
            for observed_at, source, detail in evidence
            if observed_at is not None
        ]
        latest = max(available, key=lambda item: float(item[0])) if available else None
        return replace(
            pulse,
            sampled_at=pulse.sampled_at or now,
            turn_started_at=turn_start.timestamp if turn_start else pulse.turn_started_at,
            phase_started_at=state.phase_since or pulse.phase_started_at,
            last_transition_at=state.phase_since or pulse.last_transition_at,
            last_semantic_at=last_semantic_at,
            last_semantic_kind=last_semantic_kind,
            last_semantic_source=last_semantic_source,
            last_evidence_at=float(latest[0]) if latest else pulse.last_evidence_at,
            last_evidence_source=str(latest[1]) if latest else pulse.last_evidence_source,
            last_evidence_detail=str(latest[2]) if latest else pulse.last_evidence_detail,
            auto_compact_expected=auto_expected,
            auto_compact_reason=(
                f"context {state.token_used} 已达到 auto compact boundary {auto_limit}"
                if auto_expected
                else ""
            ),
        )

    @staticmethod
    def _silence_assessment(state: SessionHealth, now: float) -> SilenceAssessment:
        pulse = state.observation
        semantic_at = pulse.last_semantic_at or state.phase_since
        if (
            state.lifecycle
            in {
                LifecycleState.IDLE,
                LifecycleState.COMPLETED,
                LifecycleState.FAILED,
                LifecycleState.ABORTED,
            }
            or semantic_at is None
        ):
            return SilenceAssessment(assessed_at=now)
        silence_age = max(0.0, now - semantic_at)
        evidence_age = (
            max(0.0, now - pulse.last_evidence_at) if pulse.last_evidence_at is not None else None
        )
        if pulse.collector_stale:
            return SilenceAssessment(
                SilenceState.OBSERVER_BLIND,
                pulse.collector_stale_reason or "监测证据不足",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "warning",
                Provenance("collector-health", Confidence.HIGH, derived=True, complete=False),
            )
        if silence_age < 30:
            return SilenceAssessment(
                SilenceState.NORMAL,
                "最近仍有语义事件",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "info",
            )
        recent_activity = evidence_age is not None and evidence_age <= 10
        if recent_activity and pulse.last_evidence_at and pulse.last_evidence_at > semantic_at:
            return SilenceAssessment(
                SilenceState.QUIET_ACTIVE,
                "暂无新语义事件，但进程仍有可观察活动",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "info",
                Provenance(
                    pulse.last_evidence_source or "observation",
                    Confidence.MEDIUM,
                    derived=True,
                ),
            )
        if (
            state.lifecycle == LifecycleState.WAITING_RESPONSE
            and state.network.state.value == "IDLE"
            and not pulse.process_activity.active
            and silence_age >= 30
        ):
            return SilenceAssessment(
                SilenceState.WAITING_UPSTREAM,
                "正在等待上游响应",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "info",
                Provenance("state-machine", Confidence.MEDIUM, derived=True),
            )
        thresholds: dict[LifecycleState, float] = {
            LifecycleState.STARTING: 60,
            LifecycleState.WAITING_RESPONSE: 90,
            LifecycleState.GENERATING: 120,
            LifecycleState.RUNNING_TOOL: 90,
            LifecycleState.COMPACTING: 120,
        }
        threshold = thresholds.get(state.lifecycle, 120)
        if pulse.silence_baseline_samples >= 3 and pulse.silence_p95_seconds is not None:
            threshold = max(threshold, pulse.silence_p95_seconds * 1.5)
        phase_age = max(0.0, now - (state.phase_since or semantic_at))
        if phase_age >= threshold and pulse.quiet_full_samples >= 2:
            severe_thresholds: dict[LifecycleState, float] = {
                LifecycleState.STARTING: 180,
                LifecycleState.WAITING_RESPONSE: 180,
                LifecycleState.GENERATING: 300,
                LifecycleState.RUNNING_TOOL: 240,
                LifecycleState.COMPACTING: 300,
            }
            severe_threshold = severe_thresholds.get(state.lifecycle, 300)
            if pulse.silence_baseline_samples >= 3 and pulse.silence_p95_seconds is not None:
                severe_threshold = max(severe_threshold, pulse.silence_p95_seconds * 3)
            return SilenceAssessment(
                SilenceState.STALL_SUSPECT,
                "疑似停滞，连续采样未发现 rollout、process、network 或 log 活动",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "severe" if phase_age >= severe_threshold else "warning",
                Provenance("silence-assessment", Confidence.MEDIUM, derived=True),
            )
        if silence_age >= 30:
            return SilenceAssessment(
                SilenceState.QUIET_UNKNOWN,
                "内部阶段暂时不可见",
                now,
                semantic_at,
                pulse.last_evidence_at,
                "info",
                Provenance("silence-assessment", Confidence.LOW, derived=True),
            )
        return SilenceAssessment(
            SilenceState.NORMAL,
            "语义事件暂时安静",
            now,
            semantic_at,
            pulse.last_evidence_at,
            "info",
        )

    def _derive_alert(
        self,
        state: SessionHealth,
        events: list[NormalizedEvent],
        now: float,
    ) -> None:
        turn_start = self._latest(events, "TURN_STARTED")
        request = self._latest(events, "REQUEST_SENT")
        response = self._latest(events, "RESPONSE_STARTED")
        model = self._latest(
            events,
            "MODEL_PROGRESS",
            "REASONING_SUMMARY",
            "PLAN_UPDATED",
            "TOOL_RUNNING",
            "FILE_CHANGE_APPLIED",
            "FILE_CHANGE_FAILED",
            "TURN_COMPLETED",
            "TURN_FAILED",
        )
        tool_done = self._latest(events, "TOOL_COMPLETED")
        keepalive = self._latest(events, "KEEPALIVE")
        request_progress = self._latest(
            events,
            "REQUEST_SENT",
            "RESPONSE_STARTED",
            "MODEL_PROGRESS",
            "REASONING_SUMMARY",
            "PLAN_UPDATED",
            "TOOL_RUNNING",
            "TOOL_COMPLETED",
            "FILE_CHANGE_APPLIED",
            "FILE_CHANGE_FAILED",
            "COMPACTING",
            "COMPACT_COMPLETED",
        )
        alert = ""
        since = 0.0
        reason = ""
        if turn_start and not request_progress:
            alert, since, reason = (
                ALERT_PRE_REQUEST,
                turn_start.timestamp,
                "turn 已开始但尚未进入模型请求",
            )
        elif request and (not response or response.timestamp < request.timestamp):
            alert = ALERT_HTTP_RESPONSE
            since = request.timestamp
            reason = "请求已发出但尚未收到 response.created"
        elif (
            tool_done
            and response
            and response.timestamp > tool_done.timestamp
            and (not model or model.timestamp < response.timestamp)
        ):
            alert, since, reason = (
                ALERT_POST_TOOL,
                response.timestamp,
                "工具结果返回后没有新的模型进展",
            )
        elif response:
            last_semantic = model or response
            if keepalive and keepalive.timestamp > last_semantic.timestamp:
                alert = ALERT_KEEPALIVE_ONLY
                since = last_semantic.timestamp
                reason = "持续收到 keepalive，但没有模型语义进展"
        if not alert:
            return
        age = max(0, int(now - since))
        warning, severe = ALERT_THRESHOLDS[alert]
        if age >= warning:
            state.alert = alert
            state.alert_level = "严重" if age >= severe else "警告"
            state.alert_reason = reason
            state.alert_age_seconds = age

    def prune(self, active_keys: set[str | SessionIdentity], now: float | None = None) -> None:
        now = time.time() if now is None else now
        expiry = now - self.lookback_seconds
        for key in list(self.events):
            if key in active_keys:
                continue
            latest = self.events[key][-1].timestamp if self.events[key] else 0
            if latest < expiry:
                self.events.pop(key, None)
                self.seen.pop(key, None)
                self.dedupe_matches.pop(key, None)
                self.dedupe_degraded_drops.pop(key, None)
                self.stream_generations.pop(key, None)
                self.stale_stream_generation_dropped.pop(key, None)
                self.stream_identity_limit_dropped.pop(key, None)
                self.stream_generation_advances.pop(key, None)
                self.pending_recovery.pop(key, None)
                self.alerts.pop(key, None)
                self.compactions.pop(key, None)
                self.clock_state.pop(key, None)
                self.clock_observed_at.pop(key, None)
                self.clock_decision_at.pop(key, None)
                self.coverage_sources.pop(key, None)
                self.coverage_gap_at.pop(key, None)
                self.coverage_gap_reasons.pop(key, None)
                self.coverage_backlog.pop(key, None)
                self.terminal_probe_complete.pop(key, None)
                self.terminal_probe_complete_at.pop(key, None)
                self.network_probe_complete.pop(key, None)
                self.silence_probe_complete.pop(key, None)
                self.event_retention_dropped.pop(key, None)
                self.axis_baselines.pop(key, None)

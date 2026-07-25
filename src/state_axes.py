"""Evidence telemetry and per-axis completeness derivation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from models import (
    AxisCompleteness,
    Confidence,
    EventTelemetrySummary,
    NormalizedEvent,
    Provenance,
    SessionCompleteness,
    SessionHealth,
    SessionIdentity,
)


class AxisDerivationMixin:
    """Derive evidence quality without owning ingestion state."""

    AXIS_BASELINE_KINDS: dict[str, set[str]]
    seen: dict[str | SessionIdentity, Any]
    dedupe_matches: dict[str | SessionIdentity, int]
    dedupe_degraded_drops: dict[str | SessionIdentity, int]
    stale_stream_generation_dropped: dict[str | SessionIdentity, int]
    stream_identity_limit_dropped: dict[str | SessionIdentity, int]
    stream_generation_advances: dict[str | SessionIdentity, int]
    coverage_backlog: dict[str | SessionIdentity, bool]
    coverage_gap_at: dict[str | SessionIdentity, float]
    coverage_gap_reasons: dict[str | SessionIdentity, tuple[str, ...]]
    terminal_probe_complete_at: dict[str | SessionIdentity, float]
    terminal_probe_complete: dict[str | SessionIdentity, bool]
    network_probe_complete: dict[str | SessionIdentity, bool]
    silence_probe_complete: dict[str | SessionIdentity, bool]

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
            key, "lifecycle", events, self.AXIS_BASELINE_KINDS["lifecycle"]
        )
        attention = self._axis_after_gap(
            key, "attention", events, self.AXIS_BASELINE_KINDS["attention"]
        )
        failure_recovery = self._axis_after_gap(
            key, "failure_recovery", events, self.AXIS_BASELINE_KINDS["failure_recovery"]
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
                key, "terminal_ownership", events, self.AXIS_BASELINE_KINDS["terminal_ownership"]
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

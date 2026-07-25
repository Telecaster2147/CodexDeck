"""Build explicit multi-source observation windows for published snapshots."""

from __future__ import annotations

from dataclasses import replace

from models import (
    Confidence,
    CollectorHealth,
    EvidenceObservation,
    InstanceSnapshot,
    SnapshotTemporalCut,
)


TEMPORAL_SOURCES = ("process", "rollout", "terminal", "sqlite", "hook", "socket", "packet")

# A source window is about when CodexDeck observed a source, not when the source
# last emitted application data. Terminal and TLS summaries retain their last
# output/handshake time, which can legitimately be much older than a healthy
# sample. Rollout owns durable terminal updates; socket owns packet attribution.
COHERENCE_SOURCES = ("process", "rollout", "sqlite", "socket", "packet")
AXIS_SOURCES = {
    "lifecycle": ("rollout",),
    "attention": ("rollout",),
    "failure_recovery": ("rollout",),
    "terminal_ownership": ("process", "terminal"),
    "network": ("socket",),
    "silence": ("process", "rollout", "socket"),
}


def apply_temporal_completeness(
    instances: list[InstanceSnapshot], cut: SnapshotTemporalCut
) -> list[InstanceSnapshot]:
    """Downgrade only conclusions whose required source windows are disjoint."""

    if cut.coherent:
        return instances

    sources = {item.source: item for item in cut.sources}

    def axis_has_disjoint_windows(axis_name: str) -> bool:
        windows = [
            sources[source].observed_to
            for source in AXIS_SOURCES[axis_name]
            if source in sources
            and sources[source].complete
            and sources[source].observed_to is not None
        ]
        return len(windows) >= 2 and max(windows) - min(windows) > cut.max_source_skew_seconds

    def downgrade(axis: object) -> object:
        if not getattr(axis, "complete", False):
            return axis
        evidence = tuple(getattr(axis, "evidence", ())) + (
            f"temporal_skew={cut.actual_source_skew_seconds:.3f}s",
        )
        return replace(
            axis,
            complete=False,
            confidence=Confidence.LOW,
            reason="跨来源观察窗口不相交",
            baseline_kind="temporal_cut",
            evidence=evidence,
        )

    updated: list[InstanceSnapshot] = []
    for instance in instances:
        sessions = []
        for session in instance.sessions:
            completeness = session.completeness
            sessions.append(
                replace(
                    session,
                    completeness=replace(
                        completeness,
                        lifecycle=(
                            downgrade(completeness.lifecycle)
                            if axis_has_disjoint_windows("lifecycle")
                            else completeness.lifecycle
                        ),
                        attention=(
                            downgrade(completeness.attention)
                            if axis_has_disjoint_windows("attention")
                            else completeness.attention
                        ),
                        failure_recovery=(
                            downgrade(completeness.failure_recovery)
                            if axis_has_disjoint_windows("failure_recovery")
                            else completeness.failure_recovery
                        ),
                        terminal_ownership=(
                            downgrade(completeness.terminal_ownership)
                            if axis_has_disjoint_windows("terminal_ownership")
                            else completeness.terminal_ownership
                        ),
                        network=(
                            downgrade(completeness.network)
                            if axis_has_disjoint_windows("network")
                            else completeness.network
                        ),
                        silence=(
                            downgrade(completeness.silence)
                            if axis_has_disjoint_windows("silence")
                            else completeness.silence
                        ),
                    ),
                )
            )
        updated.append(replace(instance, sessions=sessions))
    return updated


def _latest(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _collector_time(collectors: list[CollectorHealth], prefix: str) -> tuple[float | None, bool]:
    matching = [item for item in collectors if item.name == prefix or item.name.startswith(prefix)]
    observed = _latest([item.last_success_at for item in matching])
    complete = bool(matching) and all(
        not item.error
        and not item.consecutive_failures
        and not item.budget_exceeded
        and item.stale_age_seconds is None
        for item in matching
    )
    return observed, complete


def _source_times(
    instances: list[InstanceSnapshot], collectors: list[CollectorHealth]
) -> dict[str, tuple[float | None, bool]]:
    process = _collector_time(collectors, "process")
    socket = _collector_time(collectors, "socket")
    sqlite = _collector_time(collectors, "state_db")
    rollout_values = [
        float(activity.get("observed_at"))
        for instance in instances
        for activity in instance.rollout_activity
        if activity.get("observed_at") is not None
    ]
    rollout_observed = _latest(rollout_values)
    terminal_present = any(
        session.terminal_sessions for instance in instances for session in instance.sessions
    )
    hook_values = [instance.hook_events.last_probe_at for instance in instances]
    return {
        "process": process,
        "rollout": (rollout_observed, bool(rollout_values)),
        "terminal": (
            rollout_observed if terminal_present else None,
            terminal_present and rollout_observed is not None,
        ),
        "sqlite": sqlite,
        "hook": (_latest(hook_values), any(value is not None for value in hook_values)),
        "socket": socket,
        "packet": _collector_time(collectors, "packet"),
    }


def build_temporal_cut(
    instances: list[InstanceSnapshot],
    collectors: list[CollectorHealth],
    *,
    now: float,
    interval: float,
    generation: int,
    previous: SnapshotTemporalCut | None = None,
    fast: bool = False,
) -> SnapshotTemporalCut:
    current = _source_times(instances, collectors)
    previous_by_source = {item.source: item for item in previous.sources} if previous else {}
    observations: list[EvidenceObservation] = []
    fast_sources = {"rollout", "terminal", "hook"}
    for source in TEMPORAL_SOURCES:
        observed_at, complete = current[source]
        inherited = previous_by_source.get(source)
        if fast and source not in fast_sources and inherited is not None:
            observed_at = inherited.observed_to
            complete = inherited.complete
            source_generation = inherited.sample_generation
        elif fast and inherited is not None and observed_at is None:
            observed_at = inherited.observed_to
            complete = inherited.complete
            source_generation = inherited.sample_generation
        else:
            source_generation = generation
        validity = interval if source not in fast_sources else max(0.2, interval)
        observations.append(
            EvidenceObservation(
                source=source,
                observed_from=observed_at,
                observed_to=observed_at,
                sample_generation=source_generation,
                valid_through=observed_at + validity if observed_at is not None else None,
                stale_age_seconds=max(0.0, now - observed_at) if observed_at is not None else None,
                complete=complete,
            )
        )
    present = [
        item.observed_to
        for item in observations
        if item.source in COHERENCE_SOURCES and item.complete and item.observed_to is not None
    ]
    observed_from = min(present) if present else None
    observed_to = max(present) if present else None
    skew = (observed_to - observed_from) if observed_from is not None and observed_to is not None else 0.0
    max_skew = interval * 2
    coherent = skew <= max_skew
    return SnapshotTemporalCut(
        sample_generation=generation,
        observed_from=observed_from,
        observed_to=observed_to,
        max_source_skew_seconds=max_skew,
        actual_source_skew_seconds=skew,
        coherent=coherent,
        reason="" if coherent else "source_observation_windows_disjoint",
        sources=tuple(observations),
    )

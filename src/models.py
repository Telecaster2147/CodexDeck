"""Immutable domain values used by collection, state derivation, and rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any


class StringEnum(str, Enum):
    """Python 3.10 compatible string enum."""

    def __str__(self) -> str:
        return self.value


class LifecycleState(StringEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    GENERATING = "GENERATING"
    RUNNING_TOOL = "RUNNING_TOOL"
    COMPACTING = "COMPACTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class RecoveryState(StringEnum):
    NONE = "NONE"
    SUSPECT = "SUSPECT"
    RECONNECTING = "RECONNECTING"
    TRANSPORT_FALLBACK = "TRANSPORT_FALLBACK"
    RECOVERED = "RECOVERED"


class AttentionState(StringEnum):
    NONE = "NONE"
    APPROVAL = "APPROVAL"
    PERMISSIONS = "PERMISSIONS"
    USER_INPUT = "USER_INPUT"
    MCP_ELICITATION = "MCP_ELICITATION"
    AUTH_ELICITATION = "AUTH_ELICITATION"


class AlertStatus(StringEnum):
    OPENED = "OPENED"
    ESCALATED = "ESCALATED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class NetworkState(StringEnum):
    UNKNOWN = "UNKNOWN"
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    SUSPECT = "SUSPECT"
    STALLED = "STALLED"
    CLOSED = "CLOSED"


class SilenceState(StringEnum):
    NORMAL = "NORMAL"
    QUIET_ACTIVE = "QUIET_ACTIVE"
    WAITING_UPSTREAM = "WAITING_UPSTREAM"
    QUIET_UNKNOWN = "QUIET_UNKNOWN"
    STALL_SUSPECT = "STALL_SUSPECT"
    OBSERVER_BLIND = "OBSERVER_BLIND"


class Confidence(StringEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class AxisCompleteness:
    axis: str
    complete: bool = True
    confidence: Confidence = Confidence.HIGH
    reason: str = "连续证据可用"
    baseline_kind: str = "continuous"
    baseline_at: float | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionCompleteness:
    lifecycle: AxisCompleteness = field(default_factory=lambda: AxisCompleteness("lifecycle"))
    attention: AxisCompleteness = field(default_factory=lambda: AxisCompleteness("attention"))
    failure_recovery: AxisCompleteness = field(
        default_factory=lambda: AxisCompleteness("failure_recovery")
    )
    terminal_ownership: AxisCompleteness = field(
        default_factory=lambda: AxisCompleteness("terminal_ownership")
    )
    network: AxisCompleteness = field(default_factory=lambda: AxisCompleteness("network"))
    silence: AxisCompleteness = field(default_factory=lambda: AxisCompleteness("silence"))

    @property
    def incomplete_axes(self) -> tuple[str, ...]:
        return tuple(
            item.axis
            for item in (
                self.lifecycle,
                self.attention,
                self.failure_recovery,
                self.terminal_ownership,
                self.network,
                self.silence,
            )
            if not item.complete
        )


@dataclass(frozen=True)
class EvidenceCoverage:
    observed_at: float
    source_epoch: str = ""
    bootstrap_truncated: bool = False
    gap_count: int = 0
    generation_changed: bool = False
    copy_truncated: bool = False
    stream_uncertainty_count: int = 0
    backlog_pending: bool = False
    terminal_probe_complete: bool | None = None
    network_probe_complete: bool | None = None
    silence_probe_complete: bool | None = None


class CapabilityMode(StringEnum):
    DIRECT = "direct"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class TerminalCapability(StringEnum):
    STREAMING = "STREAMING"
    FILE_TAIL = "FILE_TAIL"
    POLL_TRANSCRIPT = "POLL_TRANSCRIPT"
    FINAL_TRANSCRIPT = "FINAL_TRANSCRIPT"
    METADATA_ONLY = "METADATA_ONLY"


RUNNING_TERMINAL_STATUSES = frozenset({"running", "in_progress"})


@dataclass(frozen=True)
class Provenance:
    source: str
    confidence: Confidence = Confidence.HIGH
    derived: bool = False
    complete: bool = True


@dataclass(frozen=True)
class UnparsedPayload:
    source_type: str
    length: int
    sha256: str
    preview: str
    truncated: bool = False


@dataclass(frozen=True)
class CapabilityStatus:
    mode: CapabilityMode = CapabilityMode.UNAVAILABLE
    source: str = ""
    confidence: Confidence = Confidence.LOW

    @property
    def available(self) -> bool:
        return self.mode != CapabilityMode.UNAVAILABLE


@dataclass(frozen=True)
class ProtocolCapabilities:
    turn_timing: CapabilityStatus = field(default_factory=CapabilityStatus)
    item_timing: CapabilityStatus = field(default_factory=CapabilityStatus)
    tool_timing: CapabilityStatus = field(default_factory=CapabilityStatus)
    token_usage: CapabilityStatus = field(default_factory=CapabilityStatus)
    rate_limits: CapabilityStatus = field(default_factory=CapabilityStatus)
    collab_status: CapabilityStatus = field(default_factory=CapabilityStatus)
    subagent_path: CapabilityStatus = field(default_factory=CapabilityStatus)
    action_required: CapabilityStatus = field(default_factory=CapabilityStatus)


@dataclass(frozen=True)
class CommandExecutionSummary:
    command_name: str = ""
    complete: bool = False
    reason: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout_bytes_read: int = 0
    stdout_bytes_retained: int = 0
    stdout_bytes_filtered: int = 0
    stderr_bytes_read: int = 0
    stderr_bytes_retained: int = 0
    stdout_lines_read: int = 0
    stderr_lines_read: int = 0
    records_retained: int = 0
    records_filtered: int = 0
    records_dropped: int = 0


@dataclass(frozen=True)
class CollectorHealth:
    name: str
    duration_seconds: float = 0.0
    last_success_at: float | None = None
    stale_age_seconds: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    budget_exceeded: bool = False
    command: CommandExecutionSummary | None = None


@dataclass(frozen=True)
class ObserverHealth:
    sample_kind: str = "full"
    scheduled_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_seconds: float = 0.0
    scheduling_lag_seconds: float = 0.0
    event_loop_lag_seconds: float = 0.0
    snapshot_age_seconds: float = 0.0
    worker_in_flight_age_seconds: float = 0.0
    last_success_at: float | None = None
    skipped_ticks: int = 0
    coalesced_ticks: int = 0
    consecutive_overdue: int = 0
    degraded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DiagnosticParameter:
    name: str
    value: str | int | float | bool | None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    domain: str
    source: str
    observed_at: float | None
    privacy_class: str
    parameters: tuple[DiagnosticParameter, ...]
    message_key: str
    recovery_state: str = "active"


class AdapterStatus(StringEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class AdapterResult:
    status: AdapterStatus
    source: str
    observed_at: float
    error_code: str = ""
    partial_count: int = 0
    complete: bool = False
    value: Any = field(default=None, metadata={"public": False})


@dataclass(frozen=True)
class EvidenceObservation:
    source: str
    observed_from: float | None = None
    observed_to: float | None = None
    sample_generation: int = 0
    valid_through: float | None = None
    stale_age_seconds: float | None = None
    complete: bool = False


@dataclass(frozen=True)
class SnapshotTemporalCut:
    kind: str = "composite_interval"
    sample_generation: int = 0
    observed_from: float | None = None
    observed_to: float | None = None
    max_source_skew_seconds: float = 0.0
    actual_source_skew_seconds: float = 0.0
    coherent: bool = True
    reason: str = ""
    sources: tuple[EvidenceObservation, ...] = ()


@dataclass(frozen=True)
class HistoryWindowStats:
    label: str
    window_seconds: int
    sample_count: int = 0
    turn_count: int = 0
    failure_count: int = 0
    failure_rate: float | None = None
    ttft_samples: int = 0
    ttft_p50_seconds: float | None = None
    ttft_p95_seconds: float | None = None
    tool_samples: int = 0
    tool_p50_seconds: float | None = None
    tool_p95_seconds: float | None = None
    reconnect_count: int = 0
    fallback_count: int = 0
    recovery_samples: int = 0
    recovery_average_seconds: float | None = None
    compact_count: int = 0
    compact_per_hour: float = 0.0
    silence_samples: int = 0
    silence_p50_seconds: float | None = None
    silence_p95_seconds: float | None = None
    compact_manual_count: int = 0
    compact_auto_count: int = 0
    compact_failure_count: int = 0
    compact_retry_count: int = 0
    compact_duration_samples: int = 0
    compact_duration_p50_seconds: float | None = None
    compact_duration_p95_seconds: float | None = None
    compact_context_samples: int = 0
    compact_context_before_average: float | None = None
    compact_context_after_average: float | None = None
    phase_duration_seconds: tuple[tuple[str, float], ...] = ()
    waiting_upstream_seconds: float = 0.0
    attention_wait_seconds: float = 0.0
    observer_blind_samples: int = 0
    observer_blind_frequency: float | None = None
    protocol_degraded_samples: int = 0
    protocol_degraded_frequency: float | None = None


@dataclass(frozen=True)
class HistoryPersistenceStatus:
    enabled: bool = False
    queue_depth: int = 0
    queue_capacity: int = 0
    enqueued_samples: int = 0
    persisted_samples: int = 0
    dropped_samples: int = 0
    coalesced_samples: int = 0
    last_persisted_sample_at: str = ""
    last_success_at: float | None = None
    stats_generated_at: float | None = None
    stats_age_seconds: float | None = None
    writer_lag_seconds: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    maintenance_error: str = ""
    shutdown_timed_out: bool = False
    shared_path_policy: str = "unsupported_for_low_latency_writes"


@dataclass(frozen=True)
class EventTelemetrySummary:
    total_events: int = 0
    observed_events: int = 0
    unparsed_events: int = 0
    unknown_rate: float = 0.0
    observation_p50_seconds: float | None = None
    observation_p95_seconds: float | None = None
    rendered_events: int = 0
    render_p50_seconds: float | None = None
    render_p95_seconds: float | None = None
    dedupe_filter_bits_set: int = 0
    dedupe_filter_capacity_bits: int = 0
    dedupe_filter_fill_ratio: float = 0.0
    dedupe_filter_matches: int = 0
    dedupe_filter_degraded_drops: int = 0
    dedupe_filter_degraded: bool = False
    stale_stream_generation_dropped: int = 0
    stream_identity_limit_dropped: int = 0
    stream_generation_advances: int = 0


@dataclass(frozen=True)
class CompactSourceStatus:
    configured: bool = False
    readable: bool = False
    source: str = ""
    last_probe_at: float | None = None
    last_event_at: float | None = None
    error: str = ""
    bytes_read: int = 0
    consumed_bytes: int = 0
    record_count: int = 0
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
    device: int = 0
    inode: int = 0
    generation: int = 0
    anchor_hash: str = ""
    stream_uncertain: bool = False
    stream_uncertainty_count: int = 0
    stream_uncertainty_reason: str = ""
    parse_validity: Confidence = Confidence.HIGH
    source_authenticity: Confidence = Confidence.HIGH
    identity_binding: Confidence = Confidence.HIGH
    semantic_confidence: Confidence = Confidence.HIGH
    binding_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int

    @property
    def key(self) -> str:
        return self.storage_key

    @property
    def storage_key(self) -> str:
        return f"{self.pid}:{self.start_time}"

    @property
    def display_key(self) -> str:
        return str(self.pid)


@dataclass(frozen=True)
class InstanceIdentity:
    codex_home: Path
    sqlite_home: Path
    storage_id: str = field(default="", repr=False)

    @classmethod
    def from_storage_key(cls, storage_key: str) -> "InstanceIdentity":
        marker = Path("/") / storage_key
        return cls(marker, marker, storage_key)

    @property
    def storage_key(self) -> str:
        if self.storage_id:
            return self.storage_id
        return self.canonical_storage_key

    @property
    def canonical_storage_key(self) -> str:
        payload = f"{self.codex_home}\0{self.sqlite_home}".encode()
        return hashlib.blake2s(payload, digest_size=16).hexdigest()

    @property
    def legacy_storage_key(self) -> str:
        payload = f"{self.codex_home}\0{self.sqlite_home}".encode()
        return hashlib.blake2s(payload, digest_size=8).hexdigest()

    @property
    def canonical_key(self) -> tuple[str, str]:
        return (str(self.codex_home), str(self.sqlite_home))

    @property
    def display_key(self) -> str:
        return self.storage_key[:8]


class InstanceIdentityRegistry:
    """Resolve surrogate IDs without using them as factual identity."""

    def __init__(self) -> None:
        self._canonical_by_storage: dict[str, tuple[str, str]] = {}

    def register(self, identity: InstanceIdentity) -> tuple[str, bool]:
        requested = identity.storage_key
        canonical = identity.canonical_key
        existing = self._canonical_by_storage.get(requested)
        if existing is None or existing == canonical:
            self._canonical_by_storage[requested] = canonical
            return requested, False
        resolved = identity.canonical_storage_key
        suffix = 0
        while (
            resolved in self._canonical_by_storage
            and self._canonical_by_storage[resolved] != canonical
        ):
            suffix += 1
            payload = f"{canonical[0]}\0{canonical[1]}\0{suffix}".encode()
            resolved = hashlib.blake2s(payload, digest_size=16).hexdigest()
        self._canonical_by_storage[resolved] = canonical
        return resolved, True


@dataclass(frozen=True)
class SessionIdentity:
    instance: InstanceIdentity
    session_id: str

    @property
    def storage_key(self) -> str:
        return f"{self.instance.storage_key}:{self.session_id}"

    @property
    def display_key(self) -> str:
        return self.session_id[:8] or self.instance.display_key


@dataclass(frozen=True)
class RolloutIdentity:
    path: Path
    device: int
    inode: int
    generation: int = 0

    @property
    def storage_key(self) -> str:
        return f"{self.device}:{self.inode}:{self.generation}:{self.path}"

    @property
    def display_key(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class TerminalIdentity:
    session: SessionIdentity
    process_id: str
    root_call_id: str
    invocation: int

    @property
    def storage_key(self) -> str:
        anchor = self.process_id or self.root_call_id or "terminal"
        return f"{self.session.storage_key}:{anchor}:{self.invocation}"

    @property
    def display_key(self) -> str:
        return self.process_id or self.root_call_id or f"terminal-{self.invocation}"


@dataclass(frozen=True)
class SocketFlowIdentity:
    local: str
    peer: str
    pid: int
    fd: int | None = None

    @property
    def storage_key(self) -> str:
        fd = "" if self.fd is None else str(self.fd)
        return f"{self.pid}:{fd}:{self.local}->{self.peer}"

    @property
    def display_key(self) -> str:
        return f"{self.local}->{self.peer}"


@dataclass(frozen=True)
class CodexPaths:
    codex_home: Path
    sqlite_home: Path
    state_db: Path
    log_db: Path
    session_index: Path
    sessions_dir: Path


@dataclass(frozen=True)
class SourceCapabilities:
    threads: bool = False
    rollout_path: bool = False
    logs: bool = False
    thread_id: bool = False
    process_uuid: bool = False


@dataclass(frozen=True)
class ChildProcessActivity:
    identity: ProcessIdentity
    parent_pid: int = 0
    command: str = ""
    state: str = ""
    elapsed_seconds: float = 0.0
    cpu_seconds_delta: float = 0.0
    io_bytes_delta: int = 0
    io_operations_delta: int = 0
    context_switches_delta: int = 0
    active: bool = False


@dataclass(frozen=True)
class ProcessTreeActivity:
    available: bool = False
    sampled_at: float | None = None
    cpu_seconds_delta: float = 0.0
    io_bytes_delta: int = 0
    io_operations_delta: int = 0
    context_switches_delta: int = 0
    thread_count: int = 0
    child_count: int = 0
    children_created: int = 0
    children_exited: int = 0
    child_state_changes: int = 0
    active: bool = False
    detail: str = ""
    children: tuple[ChildProcessActivity, ...] = ()


@dataclass(frozen=True)
class ObservationPulse:
    sampled_at: float | None = None
    turn_started_at: float | None = None
    phase_started_at: float | None = None
    last_transition_at: float | None = None
    last_semantic_at: float | None = None
    last_semantic_kind: str = ""
    last_semantic_source: str = ""
    last_rollout_growth_at: float | None = None
    last_process_activity_at: float | None = None
    last_network_progress_at: float | None = None
    last_log_activity_at: float | None = None
    last_evidence_at: float | None = None
    last_evidence_source: str = ""
    last_evidence_detail: str = ""
    last_probe_at: float | None = None
    rollout_probe_at: float | None = None
    process_probe_at: float | None = None
    network_probe_at: float | None = None
    log_probe_at: float | None = None
    rollout_partial_bytes: int = 0
    rollout_bytes_delta: int = 0
    process_activity: ProcessTreeActivity = field(default_factory=ProcessTreeActivity)
    network_bytes_delta: int = 0
    quiet_full_samples: int = 0
    collector_stale: bool = False
    collector_stale_reason: str = ""
    auto_compact_expected: bool = False
    auto_compact_reason: str = ""
    silence_baseline_samples: int = 0
    silence_p50_seconds: float | None = None
    silence_p95_seconds: float | None = None


@dataclass(frozen=True)
class SilenceAssessment:
    state: SilenceState = SilenceState.NORMAL
    reason: str = ""
    assessed_at: float | None = None
    silence_started_at: float | None = None
    evidence_at: float | None = None
    severity: str = "info"
    provenance: Provenance = field(
        default_factory=lambda: Provenance("state-machine", Confidence.MEDIUM, derived=True)
    )


@dataclass
class ProcessInfo:
    identity: ProcessIdentity
    ppid: int
    command: str
    elapsed_seconds: int
    cpu_percent: float
    process_state: str
    wait_channel: str
    args: str
    role: str
    cwd: str = ""
    instance_id: str = ""
    discovery_method: str = "default"
    session_id: str = ""
    session_title: str = ""
    current_task: str = ""
    model: str = ""
    reasoning_effort: str = ""
    rollout_path: str = ""
    activity: ProcessTreeActivity = field(default_factory=ProcessTreeActivity)
    process_group_id: int | None = None
    foreground_process_group_id: int | None = None
    terminal: str = ""
    discovery_confidence: Confidence = Confidence.HIGH
    discovery_evidence: tuple[str, ...] = ()
    instance_identity: InstanceIdentity | None = field(
        default=None,
        metadata={"public": False},
    )

    @property
    def pid(self) -> int:
        return self.identity.pid

    @property
    def stable_key(self) -> str:
        return self.identity.key

    @property
    def foreground_active(self) -> bool | None:
        """Whether this process owns its controlling terminal's foreground job."""

        if "Z" in self.process_state or "X" in self.process_state:
            return False
        if "T" in self.process_state:
            return False
        if self.terminal in {"", "?", "-"}:
            return None
        if not self.process_group_id or not self.foreground_process_group_id:
            return None
        if self.foreground_process_group_id < 0:
            return None
        return self.process_group_id == self.foreground_process_group_id


@dataclass
class SocketInfo:
    state: str
    recv_q: int
    send_q: int
    local: str
    peer: str
    pid: int
    fd: int | None = None
    bytes_sent: int = 0
    bytes_acked: int = 0
    bytes_received: int = 0
    retrans_current: int = 0
    retrans_total: int = 0
    lastsnd_ms: int | None = None
    lastrcv_ms: int | None = None
    rtt_ms: float | None = None
    route: str = "unknown"
    tls_server_name: str = ""
    tls_alpn_protocols: tuple[str, ...] = ()
    tls_versions: tuple[str, ...] = ()
    tls_observed_at: float | None = None

    @property
    def key(self) -> str:
        return self.identity.display_key

    @property
    def identity(self) -> SocketFlowIdentity:
        return SocketFlowIdentity(self.local, self.peer, self.pid, self.fd)


@dataclass
class ConnectionAssessment:
    key: str
    state: str
    local: str
    peer: str
    route: str
    recv_q: int
    send_q: int
    sent_delta: int
    received_delta: int
    acked_delta: int
    retrans_delta: int
    idle_seconds: float | None
    health: NetworkState
    reason: str
    tls_server_name: str = ""
    tls_alpn_protocols: tuple[str, ...] = ()
    tls_versions: tuple[str, ...] = ()
    tls_observed_at: float | None = None


@dataclass(frozen=True)
class FailureInfo:
    category: str
    message: str
    additional_details: str = ""
    turn_id: str = ""
    timestamp: float = 0.0
    source: str = "rollout"


@dataclass(frozen=True)
class AlertTransition:
    status: AlertStatus
    timestamp: float
    reason: str = ""


@dataclass
class AlertOccurrence:
    id: str
    kind: str
    severity: str
    status: AlertStatus
    reason: str
    opened_at: float
    updated_at: float
    escalated_at: float | None = None
    acknowledged_at: float | None = None
    resolved_at: float | None = None
    transitions: list[AlertTransition] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status != AlertStatus.RESOLVED


@dataclass(frozen=True)
class NormalizedEvent:
    timestamp: float
    kind: str
    summary: str
    detail: str = ""
    source: str = "codex"
    confidence: Confidence = Confidence.HIGH
    turn_id: str = ""
    source_id: str = ""
    failure: FailureInfo | None = None
    derived: bool = False
    complete: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: float | None = None
    rendered_at: float | None = None
    unparsed: UnparsedPayload | None = None
    source_timestamp: float | None = None
    adjudicated_at: float | None = None
    clock_domain: str = "source_wall_clock"
    clock_trust: Confidence = Confidence.HIGH
    clock_uncertain: bool = False
    clock_reason: str = ""
    clock_sequence: int = 0
    parse_validity: Confidence = Confidence.HIGH
    source_authenticity: Confidence = Confidence.HIGH
    identity_binding: Confidence = Confidence.HIGH
    semantic_confidence: Confidence = Confidence.HIGH
    binding_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_timestamp is None:
            object.__setattr__(self, "source_timestamp", self.timestamp)

    @property
    def provenance(self) -> Provenance:
        return Provenance(self.source, self.confidence, self.derived, self.complete)

    @property
    def freshness_seconds(self) -> float | None:
        if self.observed_at is None:
            return None
        source_timestamp = self.source_timestamp or self.timestamp
        return max(0.0, self.observed_at - min(source_timestamp, self.observed_at))

    @property
    def presentation_timestamp(self) -> float:
        return self.source_timestamp or self.timestamp

    @property
    def decision_timestamp(self) -> float:
        return self.adjudicated_at if self.adjudicated_at is not None else self.timestamp


@dataclass(frozen=True)
class ClockAssessment:
    source: str
    clock_domain: str
    source_timestamp: float
    observed_at: float | None
    adjudicated_at: float
    reason: str


@dataclass(frozen=True)
class AttentionRequest:
    state: AttentionState
    request_id: str = ""
    call_id: str = ""
    turn_id: str = ""
    summary: str = ""
    detail: str = ""
    started_at: float | None = None
    observed_at: float | None = None
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )


@dataclass(frozen=True)
class AttentionItem:
    session: SessionIdentity
    workspace: str
    category: str
    severity: str
    summary: str
    detail: str = ""
    opened_at: float | None = None
    last_evidence_at: float | None = None
    source: str = ""
    confidence: Confidence = Confidence.LOW
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )


@dataclass(frozen=True)
class CurrentOperationSummary:
    category: str = "idle"
    label: str = "空闲"
    detail: str = ""
    started_at: float | None = None
    tool_count: int = 0
    file_count: int = 0
    agent: str = ""
    provenance: Provenance = field(
        default_factory=lambda: Provenance("state-machine", Confidence.MEDIUM, derived=True)
    )


@dataclass(frozen=True)
class DiagnosisFinding:
    severity: str
    conclusion: str
    reason: str
    evidence: tuple[str, ...] = ()
    provenance: Provenance = field(
        default_factory=lambda: Provenance("state-machine", Confidence.MEDIUM, derived=True)
    )
    freshness_seconds: float | None = None
    action: str = ""


@dataclass(frozen=True)
class TokenUsageSummary:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    context_tokens: int | None = None
    context_window: int | None = None
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )

    @property
    def context_percent(self) -> float | None:
        if self.context_tokens is None or not self.context_window:
            return None
        return 100.0 * self.context_tokens / self.context_window


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float | None = None
    reset_at: float | None = None
    window_minutes: int | None = None


@dataclass(frozen=True)
class RateLimitSummary:
    primary: RateLimitWindow | None = None
    secondary: RateLimitWindow | None = None
    credits: float | None = None
    has_credits: bool | None = None
    reached: bool | None = None
    reason: str = ""
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )


@dataclass(frozen=True)
class ToolExecutionSummary:
    call_id: str
    turn_id: str = ""
    category: str = "tool"
    display_name: str = "tool"
    tool_name: str = ""
    started_at: float | None = None
    completed_at: float | None = None
    duration_seconds: float | None = None
    status: str = "running"
    exit_code: int | None = None
    completion_status: str = ""
    command: str = ""
    cwd: str = ""
    arguments: str = ""
    output: str = ""
    files: tuple[str, ...] = ()
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )


@dataclass(frozen=True)
class TerminalChunk:
    source_id: str
    observed_at: float
    stream: str = "combined"
    text: str = ""
    sequence: int = 0
    complete: bool = True


@dataclass(frozen=True)
class TerminalSessionSummary:
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
    retained_bytes: int = 0
    dropped_bytes: int = 0
    upstream_truncated: bool = False
    stale: bool = False
    process_active: bool = False
    source: str = "rollout"
    association_status: str = "unresolved"
    correlation_source: str = ""
    association_reason: str = "missing_correlation_identity"
    chunks: tuple[TerminalChunk, ...] = ()
    identity: TerminalIdentity | None = field(
        default=None,
        metadata={"public": False},
    )


@dataclass(frozen=True)
class TerminalAssociationSummary:
    eligible_operations: int = 0
    associated_operations: int = 0
    confirmed: int = 0
    ambiguous: int = 0
    conflicting: int = 0
    unresolved: int = 0
    dropped: int = 0
    reasons: tuple[tuple[str, int], ...] = ()
    labeled_correct: int = 0
    labeled_incorrect: int = 0
    association_coverage: float | None = None
    unresolved_rate: float | None = None
    precision: float | None = None
    private_state_entries: int = 0
    private_state_estimated_bytes: int = 0
    private_state_evictions: int = 0
    private_state_dropped: int = 0
    private_state_recoveries: int = 0
    private_state_reasons: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class TurnSummary:
    turn_id: str
    started_at: float | None = None
    completed_at: float | None = None
    duration_seconds: float | None = None
    time_to_first_token_seconds: float | None = None
    status: str = "running"
    result: str = ""
    model: str = ""
    reasoning_effort: str = ""
    collaboration_mode: str = ""
    trace_id: str = ""
    token_usage: TokenUsageSummary | None = None
    tool_count: int = 0
    tool_duration_seconds: float | None = None
    longest_tool: ToolExecutionSummary | None = None
    reconnect_count: int = 0
    fallback_count: int = 0
    recovery_duration_seconds: float | None = None
    compact_count: int = 0
    failure: FailureInfo | None = None
    tools: tuple[ToolExecutionSummary, ...] = ()
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )


@dataclass(frozen=True)
class CompactionEvidence:
    edge: str
    timestamp: float
    source: str
    observed_at: float | None = None
    confidence: Confidence = Confidence.HIGH
    direct: bool = True
    detail: str = ""
    parse_validity: Confidence = Confidence.HIGH
    source_authenticity: Confidence = Confidence.HIGH
    identity_binding: Confidence = Confidence.HIGH
    semantic_confidence: Confidence = Confidence.HIGH
    binding_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompactionSummary:
    operation_id: str = ""
    status: str = "pending"
    requested_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    failed_at: float | None = None
    aborted_at: float | None = None
    trigger: str = ""
    context_tokens: int | None = None
    context_tokens_after: int | None = None
    context_window: int | None = None
    auto_compact_token_limit: int | None = None
    turn_id: str = ""
    source: str = ""
    confidence: Confidence = Confidence.LOW
    reconstructed: bool = False
    retry_count: int = 0
    failure: FailureInfo | None = None
    evidence: tuple[CompactionEvidence, ...] = ()

    @property
    def terminal_at(self) -> float | None:
        return self.completed_at or self.failed_at or self.aborted_at

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.terminal_at is None:
            return None
        return max(0.0, self.terminal_at - self.started_at)


@dataclass
class AgentNode:
    thread_id: str
    parent_thread_id: str = ""
    agent_path: str = ""
    nickname: str = ""
    role: str = ""
    model: str = ""
    reasoning_effort: str = ""
    status: str = "pending"
    spawned_at: float | None = None
    updated_at: float | None = None
    interaction_count: int = 0
    interaction_seconds: float = 0.0
    wait_count: int = 0
    wait_seconds: float = 0.0
    resume_count: int = 0
    resume_seconds: float = 0.0
    close_count: int = 0
    close_seconds: float = 0.0
    error: FailureInfo | None = None
    provenance: Provenance = field(
        default_factory=lambda: Provenance("", Confidence.LOW, complete=False)
    )
    children: list["AgentNode"] = field(default_factory=list)


@dataclass
class NetworkEvidence:
    state: NetworkState = NetworkState.UNKNOWN
    reason: str = ""
    stale: bool = False
    stale_age_seconds: float | None = None
    connections: list[ConnectionAssessment] = field(default_factory=list)


@dataclass
class SessionHealth:
    instance_id: str
    session_id: str
    process: ProcessInfo
    lifecycle: LifecycleState = LifecycleState.IDLE
    lifecycle_confidence: Confidence = Confidence.MEDIUM
    lifecycle_provenance: Provenance = field(
        default_factory=lambda: Provenance("state-machine", Confidence.MEDIUM, derived=True)
    )
    recovery: RecoveryState = RecoveryState.NONE
    attention: AttentionState = AttentionState.NONE
    attention_confidence: Confidence = Confidence.MEDIUM
    attention_provenance: Provenance = field(
        default_factory=lambda: Provenance("state-machine", Confidence.MEDIUM, derived=True)
    )
    attention_request: AttentionRequest | None = None
    protocol_uncertain: bool = False
    protocol_uncertainty_scope: str = ""
    protocol_uncertainty_reason: str = ""
    clock_uncertain: bool = False
    clock_assessments: tuple[ClockAssessment, ...] = ()
    completeness: SessionCompleteness = field(default_factory=SessionCompleteness)
    current_operation: CurrentOperationSummary = field(default_factory=CurrentOperationSummary)
    diagnosis: list[DiagnosisFinding] = field(default_factory=list)
    event_telemetry: EventTelemetrySummary = field(default_factory=EventTelemetrySummary)
    observation: ObservationPulse = field(default_factory=ObservationPulse)
    silence: SilenceAssessment = field(default_factory=SilenceAssessment)
    network: NetworkEvidence = field(default_factory=NetworkEvidence)
    phase: str = "空闲"
    phase_since: float | None = None
    alert: str = ""
    alert_level: str = ""
    alert_reason: str = ""
    alert_age_seconds: int = 0
    alerts: list[AlertOccurrence] = field(default_factory=list)
    current_failure: FailureInfo | None = None
    latest_failure: FailureInfo | None = None
    token_used: int | None = None
    token_limit: int | None = None
    token_usage: TokenUsageSummary | None = None
    cumulative_token_usage: TokenUsageSummary | None = None
    rate_limits: RateLimitSummary | None = None
    turns: list[TurnSummary] = field(default_factory=list)
    compactions: list[CompactionSummary] = field(default_factory=list)
    tool_executions: list[ToolExecutionSummary] = field(default_factory=list)
    terminal_sessions: list[TerminalSessionSummary] = field(default_factory=list)
    terminal_association: TerminalAssociationSummary = field(
        default_factory=TerminalAssociationSummary
    )
    agents: list[AgentNode] = field(default_factory=list)
    protocol_capabilities: ProtocolCapabilities = field(default_factory=ProtocolCapabilities)
    process_exited: bool = False
    process_exited_at: float | None = None
    events: list[NormalizedEvent] = field(default_factory=list)
    identity: SessionIdentity | None = field(
        default=None,
        metadata={"public": False},
    )

    @property
    def key(self) -> str:
        return self.session_identity.storage_key

    @property
    def session_identity(self) -> SessionIdentity:
        return self.identity or SessionIdentity(
            InstanceIdentity.from_storage_key(self.instance_id),
            self.session_id,
        )


@dataclass
class InstanceSnapshot:
    instance_id: str
    paths: CodexPaths
    display_codex_home: str
    display_sqlite_home: str
    discovery_method: str
    capabilities: SourceCapabilities = field(default_factory=SourceCapabilities)
    protocol_capabilities: ProtocolCapabilities = field(default_factory=ProtocolCapabilities)
    collector_health: list[CollectorHealth] = field(default_factory=list)
    adapter_results: tuple[AdapterResult, ...] = ()
    diagnostics: list[Diagnostic | str] = field(default_factory=list)
    unknown_event_types: dict[str, int] = field(default_factory=dict)
    protocol_shape_families: dict[str, int] = field(default_factory=dict)
    protocol_family_counters: dict[str, int] = field(default_factory=dict)
    rollout_context_truncated: bool = False
    rollout_activity: list[dict[str, Any]] = field(default_factory=list)
    process_data_stale_age_seconds: float | None = None
    socket_data_stale_age_seconds: float | None = None
    auto_compact_token_limit: int | None = None
    auto_compact_token_limit_scope: str = ""
    compact_prompt_overridden: bool = False
    auto_compact_config_source: str = ""
    history_windows: list[HistoryWindowStats] = field(default_factory=list)
    tui_session_log: CompactSourceStatus = field(default_factory=CompactSourceStatus)
    hook_events: CompactSourceStatus = field(default_factory=CompactSourceStatus)
    processes: list[ProcessInfo] = field(default_factory=list)
    sessions: list[SessionHealth] = field(default_factory=list)
    identity: InstanceIdentity | None = field(
        default=None,
        metadata={"public": False},
    )

    @property
    def instance_identity(self) -> InstanceIdentity:
        return self.identity or InstanceIdentity.from_storage_key(self.instance_id)


@dataclass(frozen=True)
class DiscoveryCandidateDiagnostic:
    pid: int
    command: str
    role: str
    outcome: str
    reason: str
    confidence: Confidence = Confidence.LOW


@dataclass(frozen=True)
class DiscoverySummary:
    candidates: int = 0
    confirmed: int = 0
    rejected: int = 0
    unresolved: int = 0
    diagnostics: tuple[DiscoveryCandidateDiagnostic, ...] = ()
    labeled_true_positive: int = 0
    labeled_false_positive: int = 0
    labeled_false_negative: int = 0
    precision: float | None = None
    recall: float | None = None


@dataclass
class MonitorSnapshot:
    generated_at: str
    interval_seconds: float
    instances: list[InstanceSnapshot] = field(default_factory=list)
    collection_duration_seconds: float = 0.0
    diagnostics: list[Diagnostic | str] = field(default_factory=list)
    process_data_stale_age_seconds: float | None = None
    socket_data_stale_age_seconds: float | None = None
    collector_health: list[CollectorHealth] = field(default_factory=list)
    observer: ObserverHealth = field(default_factory=ObserverHealth)
    temporal: SnapshotTemporalCut = field(default_factory=SnapshotTemporalCut)
    discovery: DiscoverySummary = field(default_factory=lambda: DiscoverySummary())
    history: HistoryPersistenceStatus = field(default_factory=HistoryPersistenceStatus)

    @property
    def sessions(self) -> list[SessionHealth]:
        return [session for instance in self.instances for session in instance.sessions]

    def summary(self) -> dict[str, int]:
        sessions = self.sessions
        return {
            "instances": len(self.instances),
            "sessions": len(sessions),
            "current_failures": sum(item.lifecycle == LifecycleState.FAILED for item in sessions),
            "alerts": sum(bool(item.alert) for item in sessions),
            "severe_stalls": sum(item.alert_level == "严重" for item in sessions),
            "network_stalls": sum(item.network.state == NetworkState.STALLED for item in sessions),
            "action_required": sum(bool(item.attention_request) for item in sessions),
            "issues": sum(
                bool(item.current_failure)
                or bool(item.attention_request)
                or bool(item.alert)
                or item.network.state == NetworkState.STALLED
                or item.silence.state in {SilenceState.STALL_SUSPECT, SilenceState.OBSERVER_BLIND}
                for item in sessions
            ),
            "stall_suspects": sum(
                item.silence.state == SilenceState.STALL_SUSPECT for item in sessions
            ),
            "observer_blind": sum(
                item.silence.state == SilenceState.OBSERVER_BLIND for item in sessions
            ),
            "degraded_sources": sum(bool(item.diagnostics) for item in self.instances),
        }


def json_value(value: Any) -> Any:
    """Convert domain values into JSON-safe primitives without losing nulls."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: json_value(getattr(value, name))
            for name, definition in value.__dataclass_fields__.items()
            if definition.metadata.get("public", True)
        }
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value

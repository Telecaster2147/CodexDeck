"""Immutable domain values used by collection, state derivation, and rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
class CollectorHealth:
    name: str
    duration_seconds: float = 0.0
    last_success_at: float | None = None
    stale_age_seconds: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    budget_exceeded: bool = False


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


@dataclass(frozen=True)
class CompactSourceStatus:
    configured: bool = False
    readable: bool = False
    source: str = ""
    last_probe_at: float | None = None
    last_event_at: float | None = None
    error: str = ""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start_time}"


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
        return f"{self.local}->{self.peer}"


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
        return max(0.0, self.observed_at - self.timestamp)

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
    chunks: tuple[TerminalChunk, ...] = ()


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
    recovery: RecoveryState = RecoveryState.NONE
    attention: AttentionState = AttentionState.NONE
    attention_request: AttentionRequest | None = None
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
    agents: list[AgentNode] = field(default_factory=list)
    protocol_capabilities: ProtocolCapabilities = field(default_factory=ProtocolCapabilities)
    process_exited: bool = False
    process_exited_at: float | None = None
    events: list[NormalizedEvent] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.instance_id}:{self.session_id}:{self.process.stable_key}"


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
    diagnostics: list[str] = field(default_factory=list)
    unknown_event_types: dict[str, int] = field(default_factory=dict)
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


@dataclass
class MonitorSnapshot:
    generated_at: str
    interval_seconds: float
    instances: list[InstanceSnapshot] = field(default_factory=list)
    collection_duration_seconds: float = 0.0
    diagnostics: list[str] = field(default_factory=list)
    process_data_stale_age_seconds: float | None = None
    socket_data_stale_age_seconds: float | None = None
    collector_health: list[CollectorHealth] = field(default_factory=list)

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
                or item.silence.state
                in {SilenceState.STALL_SUSPECT, SilenceState.OBSERVER_BLIND}
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
            for name in value.__dataclass_fields__
        }
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value

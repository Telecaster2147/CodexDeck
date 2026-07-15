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


class NetworkState(StringEnum):
    UNKNOWN = "UNKNOWN"
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    SUSPECT = "SUSPECT"
    STALLED = "STALLED"
    CLOSED = "CLOSED"


class Confidence(StringEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


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

    @property
    def pid(self) -> int:
        return self.identity.pid

    @property
    def stable_key(self) -> str:
        return self.identity.key


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


@dataclass(frozen=True)
class FailureInfo:
    category: str
    message: str
    additional_details: str = ""
    turn_id: str = ""
    timestamp: float = 0.0
    source: str = "rollout"


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
    network: NetworkEvidence = field(default_factory=NetworkEvidence)
    phase: str = "空闲"
    phase_since: float | None = None
    alert: str = ""
    alert_level: str = ""
    alert_reason: str = ""
    alert_age_seconds: int = 0
    current_failure: FailureInfo | None = None
    latest_failure: FailureInfo | None = None
    token_used: int | None = None
    token_limit: int | None = None
    process_exited: bool = False
    process_exited_at: float | None = None
    events: list[NormalizedEvent] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.instance_id}:{self.session_id}:{self.process.stable_key}"


@dataclass
class ProcessAssessment:
    process: ProcessInfo
    network: NetworkEvidence

    @property
    def health(self) -> str:
        return self.network.state.value

    @property
    def reason(self) -> str:
        return self.network.reason

    @property
    def connections(self) -> list[ConnectionAssessment]:
        return self.network.connections


@dataclass
class InstanceSnapshot:
    instance_id: str
    paths: CodexPaths
    display_codex_home: str
    display_sqlite_home: str
    discovery_method: str
    capabilities: SourceCapabilities = field(default_factory=SourceCapabilities)
    diagnostics: list[str] = field(default_factory=list)
    unknown_event_types: dict[str, int] = field(default_factory=dict)
    rollout_context_truncated: bool = False
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

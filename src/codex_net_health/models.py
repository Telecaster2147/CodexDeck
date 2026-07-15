"""Domain models shared by monitoring, classification, and rendering code."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    command: str
    elapsed_seconds: int
    cpu_percent: float
    process_state: str
    wait_channel: str
    args: str
    role: str
    cwd: str = ""
    session_id: str = ""
    session_title: str = ""
    current_task: str = ""
    model: str = ""
    reasoning_effort: str = ""
    rollout_path: str = ""


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
    health: str
    reason: str


@dataclass
class ProcessAssessment:
    process: ProcessInfo
    health: str
    network_hang: str
    reason: str
    connections: list[ConnectionAssessment] = field(default_factory=list)


@dataclass(frozen=True)
class SseEvent:
    log_id: int
    timestamp: int
    thread_id: str
    kind: str


@dataclass(frozen=True)
class SseHealth:
    available: bool
    lookback_seconds: int
    recent_idle_timeouts: int = 0
    active_session_idle_timeouts: int = 0
    recent_auto_compactions: int = 0
    active_session_auto_compactions: int = 0
    recent_websocket_401s: int = 0
    last_idle_timeout_at: int | None = None
    last_idle_timeout_age_seconds: int | None = None
    error: str = ""

    @property
    def has_sse_timeout(self) -> bool:
        return self.recent_idle_timeouts > 0


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: float
    kind: str
    summary: str
    detail: str = ""
    source: str = "codex"


@dataclass
class ConversationActivity:
    session_id: str
    pid: int
    phase: str = "空闲"
    phase_since: float | None = None
    alert: str = ""
    alert_level: str = ""
    alert_age_seconds: int = 0
    alert_reason: str = ""
    compacting: bool = False
    compact_mode: str = ""
    compact_phase: str = ""
    compact_age_seconds: int = 0
    compact_result: str = ""
    token_used: int | None = None
    token_limit: int | None = None
    last_meaningful_at: float | None = None
    last_keepalive_at: float | None = None
    events: list[ActivityEvent] = field(default_factory=list)

"""Derive recovery-aware session health from normalized Codex events."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from copy import deepcopy

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
    AlertOccurrence,
    AlertStatus,
    AlertTransition,
    CapabilityMode,
    CapabilityStatus,
    Confidence,
    FailureInfo,
    LifecycleState,
    NetworkEvidence,
    NormalizedEvent,
    ProcessInfo,
    ProtocolCapabilities,
    Provenance,
    RateLimitSummary,
    RateLimitWindow,
    RecoveryState,
    SessionHealth,
    TokenUsageSummary,
    ToolExecutionSummary,
    TurnSummary,
)


PROGRESS_KINDS = {
    "RESPONSE_STARTED",
    "MODEL_PROGRESS",
    "TOOL_RUNNING",
    "TOOL_COMPLETED",
    "COMPACTING",
    "COMPACT_COMPLETED",
    "TURN_COMPLETED",
    "TURN_FAILED",
    "TURN_ABORTED",
}
TERMINAL_KINDS = {"TURN_COMPLETED", "TURN_FAILED", "TURN_ABORTED"}


class SessionStateMachine:
    def __init__(self, lookback_seconds: int) -> None:
        self.lookback_seconds = lookback_seconds
        self.events: dict[str, list[NormalizedEvent]] = defaultdict(list)
        self.seen: dict[str, set[str]] = defaultdict(set)
        self.pending_recovery: dict[str, RecoveryState] = {}
        self.alerts: dict[str, list[AlertOccurrence]] = defaultdict(list)

    @staticmethod
    def _alert_id(key: str, kind: str, opened_at: float) -> str:
        identity = f"{key}\0{kind}\0{opened_at:.6f}".encode()
        return "alert_" + hashlib.sha256(identity).hexdigest()[:20]

    def acknowledge_alert(
        self,
        key: str,
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
                occurrence.transitions.append(
                    AlertTransition(AlertStatus.ACKNOWLEDGED, timestamp)
                )
            return True
        return False

    def retained_events(self, key: str) -> tuple[NormalizedEvent, ...]:
        """Return the complete bounded event history, independent of UI lookback."""

        return tuple(self.events.get(key, ()))

    def _reconcile_alert(self, key: str, state: SessionHealth, now: float) -> None:
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

    def ingest(self, key: str, incoming: list[NormalizedEvent]) -> None:
        bucket = self.events[key]
        for event in sorted(incoming, key=lambda item: (item.timestamp, item.source_id)):
            dedupe_key = event.source_id or (
                f"{event.source}:{event.timestamp}:{event.kind}:{event.turn_id}:{event.detail}"
            )
            if dedupe_key in self.seen[key]:
                continue
            self.seen[key].add(dedupe_key)
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
                    new_details = (
                        event.failure.additional_details if event.failure else ""
                    )
                    old_size = len(old.detail) + len(old_details)
                    new_size = len(event.detail) + len(new_details)
                    if new_size > old_size:
                        bucket[duplicate_index] = event
                    continue
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
            bucket.append(event)
        bucket.sort(key=lambda item: (item.timestamp, item.source_id))
        if len(bucket) > MAX_EVENTS_PER_SESSION:
            removed = bucket[:-MAX_EVENTS_PER_SESSION]
            self.events[key] = bucket[-MAX_EVENTS_PER_SESSION:]
            for event in removed:
                if event.source_id:
                    self.seen[key].discard(event.source_id)

    @staticmethod
    def _latest(events: list[NormalizedEvent], *kinds: str) -> NormalizedEvent | None:
        wanted = set(kinds)
        return next((event for event in reversed(events) if event.kind in wanted), None)

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
        used = usage.get("total_tokens") or usage.get("input_tokens")
        limit = payload.get("model_context_window") or payload.get("context_window")
        return (
            int(used) if isinstance(used, (int, float)) else None,
            int(limit) if isinstance(limit, (int, float)) else None,
        )

    @staticmethod
    def _int(value: object) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

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
                    "context_window": raw.get("model_context_window")
                    or raw.get("context_window"),
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
                bool(credits_dict.get("has_credits"))
                if "has_credits" in credits_dict
                else None
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
            started_at = float(started_at) if isinstance(started_at, (int, float)) else start.timestamp
            completed_at = (
                float(completed_at)
                if isinstance(completed_at, (int, float))
                else end.timestamp
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
            summaries.append(
                ToolExecutionSummary(
                    call_id=identity,
                    turn_id=event.turn_id or (start.turn_id if start else ""),
                    category=str(
                        event.metadata.get("category")
                        or (start.metadata.get("category") if start else "tool")
                    ),
                    display_name=str(
                        event.metadata.get("display_name")
                        or (start.metadata.get("display_name") if start else "")
                        or event.detail
                        or "tool"
                    ),
                    started_at=(
                        float(start.metadata["started_at"])
                        if start and isinstance(start.metadata.get("started_at"), (int, float))
                        else start.timestamp if start else None
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
                    started_at=(
                        float(start.metadata["started_at"])
                        if isinstance(start.metadata.get("started_at"), (int, float))
                        else start.timestamp
                    ),
                    status="running",
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
                        started_at=start.timestamp,
                        status="running",
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
                        else start.timestamp if start else None
                    ),
                    completed_at=(
                        float(terminal_metadata["completed_at"])
                        if isinstance(terminal_metadata.get("completed_at"), (int, float))
                        else terminal.timestamp if terminal else None
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
                    compact_count=sum(
                        item.kind == "COMPACT_COMPLETED" for item in turn_events
                    ),
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
        direct = lambda source="rollout": CapabilityStatus(
            CapabilityMode.DIRECT, source, Confidence.HIGH
        )
        derived = lambda source="rollout": CapabilityStatus(
            CapabilityMode.DERIVED, source, Confidence.HIGH
        )
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
        )

    def derive(
        self,
        key: str,
        process: ProcessInfo,
        network: NetworkEvidence,
        now: float | None = None,
    ) -> SessionHealth:
        now = time.time() if now is None else now
        all_events = self.events.get(key, [])
        visible_cutoff = now - self.lookback_seconds
        visible = [event for event in all_events if event.timestamp >= visible_cutoff]
        state = SessionHealth(
            process.instance_id,
            process.session_id,
            process,
            network=network,
            events=visible,
        )
        if not all_events:
            self._reconcile_alert(key, state, now)
            return state

        latest_failure = self._latest(all_events, "TURN_FAILED")
        state.latest_failure = latest_failure.failure if latest_failure else None

        process_resume = self._latest(all_events, "PROCESS_RESUMED")
        state_events = [
            event
            for event in all_events
            if not process_resume or event.timestamp >= process_resume.timestamp
        ]
        task_start = self._latest(state_events, "TURN_STARTED")
        task_terminal = self._latest(state_events, *TERMINAL_KINDS)
        current_turn = bool(
            task_start
            and (not task_terminal or task_start.timestamp > task_terminal.timestamp)
        )
        relevant = [
            event
            for event in state_events
            if not task_start or event.timestamp >= task_start.timestamp
        ]
        latest = relevant[-1] if relevant else state_events[-1]
        failure_event = self._latest(relevant, "TURN_FAILED")
        process_exit = self._latest(all_events, "PROCESS_EXITED")
        if process_exit and process_exit is all_events[-1]:
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
        elif latest.kind == "TURN_ABORTED":
            state.lifecycle = LifecycleState.ABORTED
        elif latest.kind == "TURN_COMPLETED":
            state.lifecycle = LifecycleState.COMPLETED
        elif current_turn:
            if latest.kind in {"COMPACTING"}:
                state.lifecycle = LifecycleState.COMPACTING
            elif latest.kind == "TOOL_RUNNING":
                state.lifecycle = LifecycleState.RUNNING_TOOL
            elif latest.kind in {"MODEL_PROGRESS", "RESPONSE_STARTED", "TOOL_COMPLETED"}:
                state.lifecycle = LifecycleState.GENERATING
            elif latest.kind == "REQUEST_SENT":
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

        if state.process_exited and process_exit:
            phase_event = process_exit
        else:
            phase_event = task_terminal if task_terminal and not current_turn else latest
        state.phase = EVENT_LABELS.get(phase_event.kind, LIFECYCLE_LABELS[state.lifecycle.value])
        state.phase_since = phase_event.timestamp
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
        state.agents = self._agent_tree(all_events)
        state.protocol_capabilities = self._capabilities(all_events)

        if current_turn:
            self._derive_alert(state, relevant, now)
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
            "TOOL_RUNNING",
            "TURN_COMPLETED",
            "TURN_FAILED",
        )
        tool_done = self._latest(events, "TOOL_COMPLETED")
        keepalive = self._latest(events, "KEEPALIVE")
        alert = ""
        since = 0.0
        reason = ""
        if turn_start and not request:
            alert, since, reason = ALERT_PRE_REQUEST, turn_start.timestamp, "turn 已开始但尚未进入模型请求"
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
            alert, since, reason = ALERT_POST_TOOL, response.timestamp, "工具结果返回后没有新的模型进展"
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

    def prune(self, active_keys: set[str], now: float | None = None) -> None:
        now = time.time() if now is None else now
        expiry = now - self.lookback_seconds
        for key in list(self.events):
            if key in active_keys:
                continue
            latest = self.events[key][-1].timestamp if self.events[key] else 0
            if latest < expiry:
                self.events.pop(key, None)
                self.seen.pop(key, None)
                self.pending_recovery.pop(key, None)
                self.alerts.pop(key, None)

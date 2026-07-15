"""Derive recovery-aware session health from normalized Codex events."""

from __future__ import annotations

import json
import time
from collections import defaultdict

from .config import (
    ALERT_HTTP_RESPONSE,
    ALERT_KEEPALIVE_ONLY,
    ALERT_POST_TOOL,
    ALERT_PRE_REQUEST,
    ALERT_THRESHOLDS,
    EVENT_LABELS,
    LIFECYCLE_LABELS,
    MAX_EVENTS_PER_SESSION,
)
from .models import (
    Confidence,
    LifecycleState,
    NetworkEvidence,
    NormalizedEvent,
    ProcessInfo,
    RecoveryState,
    SessionHealth,
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
        state.token_used, state.token_limit = self._tokens(token_event)

        if current_turn:
            self._derive_alert(state, relevant, now)
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

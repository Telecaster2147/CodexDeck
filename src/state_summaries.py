"""Bounded turn, tool, agent, capability, and operation summaries."""

from __future__ import annotations

import json
from collections import defaultdict

from config import MAX_TURNS_PER_SESSION
from models import (
    AgentNode,
    CapabilityMode,
    CapabilityStatus,
    ClockAssessment,
    Confidence,
    CurrentOperationSummary,
    FailureInfo,
    LifecycleState,
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


TERMINAL_KINDS = {"TURN_COMPLETED", "TURN_FAILED", "TURN_ABORTED"}


class SummaryDerivationMixin:
    """Pure derivation helpers for bounded session summaries."""

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
        return sorted(results, key=lambda item: item.started_at or item.completed_at or 0.0)[
            -MAX_TURNS_PER_SESSION:
        ]

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
            recovery = SummaryDerivationMixin._latest(
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
        file_event = SummaryDerivationMixin._latest(
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

"""Sampling orchestration shared by one-shot, watch, and interactive modes."""

from __future__ import annotations

import time

from .activity import SseHealthTracker
from .collectors import assess_process, discover_processes, socket_snapshot
from .config import STATE_DISCONNECTED
from .models import ProcessAssessment, SocketInfo, SseHealth


def collect_assessments(
    interval: float,
    idle_threshold: float,
    selected_pids: set[int] | None,
    sse_tracker: SseHealthTracker,
) -> tuple[list[ProcessAssessment], SseHealth]:
    processes = discover_processes(selected_pids)
    if not processes:
        return [], sse_tracker.health(set())
    pids = {process.pid for process in processes}
    before = socket_snapshot(pids)
    time.sleep(interval)
    refreshed = {process.pid: process for process in discover_processes(selected_pids)}
    surviving_pids = pids & set(refreshed)
    after = socket_snapshot(surviving_pids)
    assessments: list[ProcessAssessment] = []
    for process in processes:
        if process.pid not in refreshed:
            assessments.append(
                ProcessAssessment(
                    process=process,
                    health=STATE_DISCONNECTED,
                    network_hang="进程已退出",
                    reason="采样期间进程结束",
                )
            )
            continue
        assessments.append(
            assess_process(
                refreshed[process.pid],
                before.get(process.pid, []),
                after.get(process.pid, []),
                idle_threshold,
            )
        )
    active_session_ids = {item.process.session_id for item in assessments}
    return assessments, sse_tracker.health(active_session_ids)


class LiveSampler:
    """Take non-blocking snapshots so the TUI can remain responsive to keys."""

    def __init__(
        self,
        idle_threshold: float,
        selected_pids: set[int] | None,
        sse_tracker: SseHealthTracker,
    ) -> None:
        self.idle_threshold = idle_threshold
        self.selected_pids = selected_pids
        self.sse_tracker = sse_tracker
        self.previous: dict[int, list[SocketInfo]] = {}

    def sample(self) -> tuple[list[ProcessAssessment], SseHealth]:
        processes = discover_processes(self.selected_pids)
        pids = {process.pid for process in processes}
        current = socket_snapshot(pids)
        assessments = [
            assess_process(
                process,
                self.previous.get(process.pid, current.get(process.pid, [])),
                current.get(process.pid, []),
                self.idle_threshold,
            )
            for process in processes
        ]
        self.previous = current
        session_ids = {item.process.session_id for item in assessments}
        return assessments, self.sse_tracker.health(session_ids)

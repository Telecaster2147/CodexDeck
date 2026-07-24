"""Sampling cadence and worker coordination for the Textual application."""

from __future__ import annotations

from dataclasses import dataclass

from models import ObserverHealth


@dataclass
class SamplingCoordinator:
    interval: float
    next_full_at: float
    next_fast_at: float
    monotonic_anchor: float
    wall_anchor: float
    fast_interval: float = 0.1
    in_flight: bool = False
    sample_kind: str = ""
    scheduled_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_seconds: float = 0.0
    scheduling_lag_seconds: float = 0.0
    event_loop_lag_seconds: float = 0.0
    last_success_at: float | None = None
    skipped_ticks: int = 0
    coalesced_ticks: int = 0
    consecutive_overdue: int = 0

    @classmethod
    def starting_at(
        cls,
        interval: float,
        now: float,
        *,
        wall_now: float | None = None,
        fast_interval: float = 0.1,
    ) -> "SamplingCoordinator":
        return cls(
            interval=interval,
            next_full_at=now + interval,
            next_fast_at=now,
            monotonic_anchor=now,
            wall_anchor=now if wall_now is None else wall_now,
            fast_interval=fast_interval,
        )

    def _wall_time(self, monotonic_time: float) -> float:
        return self.wall_anchor + monotonic_time - self.monotonic_anchor

    def _begin(self, kind: str, scheduled_at: float, now: float) -> None:
        self.sample_kind = kind
        self.scheduled_at = scheduled_at
        self.started_at = now
        self.scheduling_lag_seconds = max(0.0, now - scheduled_at)
        self.event_loop_lag_seconds = self.scheduling_lag_seconds
        self.in_flight = True

    def begin_initial(self, now: float | None = None) -> bool:
        if self.in_flight:
            return False
        started = self.monotonic_anchor if now is None else now
        self._begin("initial", started, started)
        return True

    def begin_manual(self, now: float) -> bool:
        if self.in_flight:
            self.skipped_ticks += 1
            self.coalesced_ticks += 1
            return False
        self.next_full_at = now + self.interval
        self.next_fast_at = now + self.fast_interval
        self._begin("manual_full", now, now)
        return True

    def begin_due(self, now: float) -> bool | None:
        if self.in_flight:
            self.skipped_ticks += 1
            self.coalesced_ticks += 1
            cadence = self.interval if "full" in self.sample_kind else self.fast_interval
            if now - self.started_at > cadence:
                self.consecutive_overdue += 1
            return None
        full = now >= self.next_full_at
        if full:
            scheduled_at = self.next_full_at
            self.next_full_at = now + self.interval
            kind = "full"
        else:
            scheduled_at = self.next_fast_at
            kind = "fast"
        self.next_fast_at = now + self.fast_interval
        self._begin(kind, scheduled_at, now)
        return full

    def finish(self, now: float | None = None, *, success: bool = True) -> None:
        completed = self.started_at if now is None else now
        self.completed_at = completed
        self.duration_seconds = max(0.0, completed - self.started_at)
        cadence = self.interval if "full" in self.sample_kind else self.fast_interval
        if self.duration_seconds <= cadence and self.scheduling_lag_seconds <= cadence:
            self.consecutive_overdue = 0
        if success:
            self.last_success_at = self._wall_time(completed)
        self.in_flight = False

    def summary(self, now: float) -> ObserverHealth:
        wall_now = self._wall_time(now)
        snapshot_age = (
            max(0.0, wall_now - self.last_success_at) if self.last_success_at is not None else 0.0
        )
        in_flight_age = max(0.0, now - self.started_at) if self.in_flight else 0.0
        stale = snapshot_age > self.interval * 2
        degraded = self.consecutive_overdue >= 2 or stale
        reason = ""
        if stale:
            reason = "snapshot_age_exceeded"
        elif self.consecutive_overdue >= 2:
            reason = "consecutive_sample_overdue"
        return ObserverHealth(
            sample_kind=self.sample_kind or "full",
            scheduled_at=self._wall_time(self.scheduled_at) if self.scheduled_at else None,
            started_at=self._wall_time(self.started_at) if self.started_at else None,
            completed_at=self._wall_time(self.completed_at) if self.completed_at else None,
            duration_seconds=self.duration_seconds,
            scheduling_lag_seconds=self.scheduling_lag_seconds,
            event_loop_lag_seconds=self.event_loop_lag_seconds,
            snapshot_age_seconds=snapshot_age,
            worker_in_flight_age_seconds=in_flight_age,
            last_success_at=self.last_success_at,
            skipped_ticks=self.skipped_ticks,
            coalesced_ticks=self.coalesced_ticks,
            consecutive_overdue=self.consecutive_overdue,
            degraded=degraded,
            reason=reason,
        )

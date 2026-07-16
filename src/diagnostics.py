"""Runtime health tracking for independent monitor collectors."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from models import CollectorHealth


@dataclass
class _CollectorState:
    last_success_at: float | None = None
    consecutive_failures: int = 0
    error: str = ""
    duration_seconds: float = 0.0


class CollectorTracker:
    """Track collector timings and failures without coupling adapters together."""

    def __init__(
        self,
        budget_seconds: float,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget_seconds = budget_seconds
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.states: dict[str, _CollectorState] = {}

    def record(self, name: str, started: float, error: BaseException | str | None = None) -> None:
        state = self.states.setdefault(name, _CollectorState())
        state.duration_seconds = max(0.0, self.monotonic() - started)
        if error is None:
            state.last_success_at = self.wall_clock()
            state.consecutive_failures = 0
            state.error = ""
        else:
            state.consecutive_failures += 1
            state.error = str(error)

    def snapshot(self) -> list[CollectorHealth]:
        now = self.wall_clock()
        return [
            CollectorHealth(
                name=name,
                duration_seconds=state.duration_seconds,
                last_success_at=state.last_success_at,
                stale_age_seconds=(
                    max(0.0, now - state.last_success_at)
                    if state.last_success_at is not None and state.consecutive_failures
                    else None
                ),
                consecutive_failures=state.consecutive_failures,
                error=state.error,
                budget_exceeded=state.duration_seconds > self.budget_seconds,
            )
            for name, state in sorted(self.states.items())
        ]

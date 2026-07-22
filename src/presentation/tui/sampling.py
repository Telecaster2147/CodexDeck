"""Sampling cadence and worker coordination for the Textual application."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SamplingCoordinator:
    interval: float
    next_full_at: float
    in_flight: bool = False

    @classmethod
    def starting_at(cls, interval: float, now: float) -> "SamplingCoordinator":
        return cls(interval=interval, next_full_at=now + interval)

    def begin_initial(self) -> bool:
        if self.in_flight:
            return False
        self.in_flight = True
        return True

    def begin_manual(self, now: float) -> bool:
        if self.in_flight:
            return False
        self.next_full_at = now + self.interval
        self.in_flight = True
        return True

    def begin_due(self, now: float) -> bool | None:
        if self.in_flight:
            return None
        full = now >= self.next_full_at
        if full:
            self.next_full_at = now + self.interval
        self.in_flight = True
        return full

    def finish(self) -> None:
        self.in_flight = False

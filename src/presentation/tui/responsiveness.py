"""Structured, non-gating TUI responsiveness evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsivenessSample:
    scenario: str
    callback_latency_seconds: float
    event_loop_lag_seconds: float
    screen_update_seconds: float
    key_to_visible_seconds: float | None = None
    focus_stable: bool = True
    scroll_stable: bool = True
    follow_stable: bool = True
    visible_update: bool = True

    def exceeded(self, cadence_seconds: float) -> tuple[str, ...]:
        reasons: list[str] = []
        for name, value in (
            ("callback_latency", self.callback_latency_seconds),
            ("event_loop_lag", self.event_loop_lag_seconds),
            ("screen_update", self.screen_update_seconds),
            ("key_to_visible", self.key_to_visible_seconds),
        ):
            if value is not None and value > cadence_seconds:
                reasons.append(f"{self.scenario}:{name}")
        return tuple(reasons)


@dataclass(frozen=True)
class ResponsivenessReport:
    cadence_seconds: float
    correctness_passed: bool
    samples: tuple[ResponsivenessSample, ...]

    @property
    def degraded_reasons(self) -> tuple[str, ...]:
        reasons = [
            reason
            for sample in self.samples
            for reason in sample.exceeded(self.cadence_seconds)
        ]
        for sample in self.samples:
            if not sample.focus_stable:
                reasons.append(f"{sample.scenario}:focus_changed")
            if not sample.scroll_stable:
                reasons.append(f"{sample.scenario}:scroll_changed")
            if not sample.follow_stable:
                reasons.append(f"{sample.scenario}:follow_changed")
            if not sample.visible_update:
                reasons.append(f"{sample.scenario}:visible_update_missing")
        return tuple(reasons)

    @property
    def responsiveness_status(self) -> str:
        return "DEGRADED" if self.degraded_reasons else "PASS"

    def as_dict(self) -> dict[str, object]:
        return {
            "correctness": "PASS" if self.correctness_passed else "FAIL",
            "responsiveness": self.responsiveness_status,
            "cadence_seconds": self.cadence_seconds,
            "degraded_reasons": list(self.degraded_reasons),
            "samples": [
                {
                    "scenario": sample.scenario,
                    "callback_latency_seconds": sample.callback_latency_seconds,
                    "event_loop_lag_seconds": sample.event_loop_lag_seconds,
                    "screen_update_seconds": sample.screen_update_seconds,
                    "key_to_visible_seconds": sample.key_to_visible_seconds,
                    "focus_stable": sample.focus_stable,
                    "scroll_stable": sample.scroll_stable,
                    "follow_stable": sample.follow_stable,
                    "visible_update": sample.visible_update,
                }
                for sample in self.samples
            ],
        }

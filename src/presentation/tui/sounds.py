"""Deterministic terminal-bell scheduling for actionable TUI transitions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from models import MonitorSnapshot


ATTENTION_DELAY_SECONDS = 5.0
ATTENTION_REPEAT_SECONDS = 60.0
COMPLETION_MIN_SECONDS = 10.0
MERGE_SECONDS = 2.0


@dataclass
class _AttentionTimer:
    due_at: float
    repeated: bool = False


@dataclass(frozen=True)
class _PendingSound:
    key: str
    kind: str
    ready_at: float


class SoundScheduler:
    """Merge, prioritize and pulse terminal BEL without depending on Textual."""

    def __init__(
        self,
        bell: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = False,
        attention_enabled: bool = False,
        completion_enabled: bool = True,
    ) -> None:
        self._bell = bell
        self._clock = clock
        self.enabled = enabled
        self.attention_enabled = attention_enabled
        self.completion_enabled = completion_enabled
        self._initialized = False
        self._seen_completions: set[str] = set()
        self._attention: dict[str, _AttentionTimer] = {}
        self._pending: dict[str, _PendingSound] = {}
        self._pulses: list[float] = []

    def configure(
        self,
        *,
        enabled: bool,
        attention_enabled: bool,
        completion_enabled: bool,
    ) -> None:
        self.enabled = enabled
        self.attention_enabled = attention_enabled
        self.completion_enabled = completion_enabled
        if not enabled:
            self._pending.clear()
            self._pulses.clear()
        if not attention_enabled:
            self._attention.clear()
            self._pending = {
                key: sound
                for key, sound in self._pending.items()
                if not sound.kind.startswith("attention")
            }

    def observe(self, snapshot: MonitorSnapshot) -> None:
        now = self._clock()
        active_attention: set[str] = set()
        completion_candidates: list[tuple[str, float | None]] = []
        for session in snapshot.sessions:
            if session.attention_request is not None and not session.process_exited:
                request = session.attention_request
                request_key = (
                    request.request_id
                    or request.call_id
                    or request.turn_id
                    or f"{request.state.value}:{request.started_at or request.observed_at or 0}"
                )
                key = f"{session.key}:{request_key}"
                active_attention.add(key)
                self._attention.setdefault(key, _AttentionTimer(now + ATTENTION_DELAY_SECONDS))
            for turn in session.turns:
                if turn.status != "completed" or turn.completed_at is None:
                    continue
                key = f"{session.key}:{turn.turn_id}:{turn.completed_at}"
                completion_candidates.append((key, turn.duration_seconds))
                if (
                    self._initialized
                    and not session.process_exited
                    and key not in self._seen_completions
                    and (turn.duration_seconds is None or turn.duration_seconds >= COMPLETION_MIN_SECONDS)
                ):
                    self._queue(key, "completion", now)
        self._seen_completions.update(key for key, _ in completion_candidates)

        for key in set(self._attention) - active_attention:
            del self._attention[key]
            self._pending.pop(key, None)
        self._initialized = True

    def tick(self) -> None:
        now = self._clock()
        if self.enabled and self.attention_enabled:
            for key, timer in tuple(self._attention.items()):
                if timer.due_at > now:
                    continue
                kind = "attention_repeat" if timer.repeated else "attention_first"
                self._queue(key, kind, now)
                timer.repeated = True
                timer.due_at = now + ATTENTION_REPEAT_SECONDS

        if self.enabled:
            ready = [sound for sound in self._pending.values() if sound.ready_at <= now]
            if ready:
                chosen = min(ready, key=self._priority)
                self._pending.clear()
                self._schedule_pattern(chosen.kind, now)

        due = [pulse for pulse in self._pulses if pulse <= now]
        self._pulses = [pulse for pulse in self._pulses if pulse > now]
        for _ in due:
            self._bell()

    def _queue(self, key: str, kind: str, now: float) -> None:
        if not self.enabled:
            return
        if kind == "completion" and not self.completion_enabled:
            return
        if kind.startswith("attention") and not self.attention_enabled:
            return
        ready_at = (
            min(sound.ready_at for sound in self._pending.values())
            if self._pending
            else now + MERGE_SECONDS
        )
        self._pending[key] = _PendingSound(key, kind, ready_at)

    @staticmethod
    def _priority(sound: _PendingSound) -> tuple[int, float]:
        rank = 0 if sound.kind.startswith("attention") else 1
        return rank, sound.ready_at

    def _schedule_pattern(self, kind: str, now: float) -> None:
        if kind == "attention_first":
            self._pulses.extend((now, now + 0.25, now + 0.50))
        elif kind == "attention_repeat":
            self._pulses.append(now)
        else:
            self._pulses.extend((now, now + 0.15))

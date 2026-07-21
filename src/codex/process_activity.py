"""Read bounded per-process activity counters from procfs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from models import ChildProcessActivity, ProcessIdentity, ProcessTreeActivity


@dataclass(frozen=True)
class ProcSample:
    identity: ProcessIdentity
    ppid: int
    command: str
    state: str
    cpu_ticks: int
    io_bytes: int
    io_operations: int
    context_switches: int
    thread_count: int
    elapsed_seconds: float


class ProcessActivityCollector:
    """Track a Codex process tree by PID and kernel start time."""

    def __init__(self, root: Path = Path("/proc")) -> None:
        self.root = root
        self.clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        self.previous: dict[str, dict[str, ProcSample]] = {}

    def snapshot(self, identity: ProcessIdentity) -> ProcessTreeActivity:
        sampled_at = time.time()
        samples = self._tree(identity.pid, sampled_at)
        root_sample = samples.get(identity.key)
        if root_sample is None or root_sample.identity != identity:
            self.previous.pop(identity.key, None)
            return ProcessTreeActivity(sampled_at=sampled_at)
        before = self.previous.get(identity.key, {})
        has_before = identity.key in self.previous
        current_keys = set(samples)
        before_keys = set(before)
        created = len(current_keys - before_keys - {identity.key}) if has_before else 0
        exited = len(before_keys - current_keys - {identity.key}) if has_before else 0
        state_changes = sum(
            key in before and before[key].state != sample.state
            for key, sample in samples.items()
        )
        cpu_ticks = 0
        io_bytes = 0
        io_operations = 0
        switches = 0
        children: list[ChildProcessActivity] = []
        for key, sample in samples.items():
            previous = before.get(key)
            cpu_delta_ticks = max(0, sample.cpu_ticks - previous.cpu_ticks) if previous else 0
            io_delta = max(0, sample.io_bytes - previous.io_bytes) if previous else 0
            io_operations_delta = (
                max(0, sample.io_operations - previous.io_operations)
                if previous
                else 0
            )
            switch_delta = (
                max(0, sample.context_switches - previous.context_switches)
                if previous
                else 0
            )
            cpu_ticks += cpu_delta_ticks
            io_bytes += io_delta
            io_operations += io_operations_delta
            switches += switch_delta
            if key == identity.key:
                continue
            children.append(
                ChildProcessActivity(
                    identity=sample.identity,
                    parent_pid=sample.ppid,
                    command=sample.command,
                    state=sample.state,
                    elapsed_seconds=sample.elapsed_seconds,
                    cpu_seconds_delta=cpu_delta_ticks / self.clock_ticks,
                    io_bytes_delta=io_delta,
                    io_operations_delta=io_operations_delta,
                    context_switches_delta=switch_delta,
                    active=bool(
                        cpu_delta_ticks
                        or io_delta
                        or io_operations_delta
                        or switch_delta
                    ),
                )
            )
        active = bool(
            cpu_ticks
            or io_bytes
            or io_operations
            or switches
            or created
            or exited
            or state_changes
        )
        parts = []
        if cpu_ticks:
            parts.append(f"CPU +{cpu_ticks / self.clock_ticks:.3f}s")
        if io_bytes:
            parts.append(f"IO +{io_bytes} B")
        if io_operations:
            parts.append(f"IO ops +{io_operations}")
        if created:
            parts.append(f"child +{created}")
        if exited:
            parts.append(f"child -{exited}")
        if state_changes:
            parts.append(f"state Δ{state_changes}")
        self.previous[identity.key] = samples
        return ProcessTreeActivity(
            available=True,
            sampled_at=sampled_at,
            cpu_seconds_delta=cpu_ticks / self.clock_ticks,
            io_bytes_delta=io_bytes,
            io_operations_delta=io_operations,
            context_switches_delta=switches,
            thread_count=sum(item.thread_count for item in samples.values()),
            child_count=max(0, len(samples) - 1),
            children_created=created,
            children_exited=exited,
            child_state_changes=state_changes,
            active=active,
            detail=" · ".join(parts),
            children=tuple(sorted(children, key=lambda item: item.identity.pid)),
        )

    def prune(self, active_identities: set[str]) -> None:
        self.previous = {
            key: value for key, value in self.previous.items() if key in active_identities
        }

    def _tree(self, root_pid: int, sampled_at: float) -> dict[str, ProcSample]:
        result: dict[str, ProcSample] = {}
        pending = [root_pid]
        seen: set[int] = set()
        while pending and len(seen) < 256:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            sample = self._sample(pid, sampled_at)
            if sample is None:
                continue
            result[sample.identity.key] = sample
            pending.extend(self._children(pid))
        return result

    def _children(self, pid: int) -> list[int]:
        task_root = self.root / str(pid) / "task"
        try:
            task_directories = tuple(task_root.iterdir())
        except OSError:
            return []
        children: set[int] = set()
        for task_directory in task_directories:
            try:
                children.update(
                    int(value)
                    for value in (task_directory / "children").read_text().split()
                )
            except (OSError, ValueError):
                continue
        return sorted(children)

    def _sample(self, pid: int, sampled_at: float) -> ProcSample | None:
        directory = self.root / str(pid)
        try:
            raw_stat = (directory / "stat").read_text(errors="replace")
            prefix, suffix = raw_stat.rsplit(")", 1)
            command = prefix.split("(", 1)[1]
            fields = suffix.split()
            state = fields[0]
            ppid = int(fields[1])
            cpu_ticks = int(fields[11]) + int(fields[12])
            start_time = int(fields[19])
        except (OSError, IndexError, ValueError):
            return None
        command = self._command_line(directory, command)
        io_values = self._key_values(directory / "io")
        status_values = self._key_values(directory / "status")
        io_bytes = sum(
            io_values.get(name, 0)
            for name in ("rchar", "wchar", "read_bytes", "write_bytes")
        )
        io_operations = sum(io_values.get(name, 0) for name in ("syscr", "syscw"))
        switches = sum(
            status_values.get(name, 0)
            for name in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")
        )
        try:
            uptime = float((self.root / "uptime").read_text().split()[0])
        except (OSError, IndexError, ValueError):
            uptime = 0.0
        return ProcSample(
            identity=ProcessIdentity(pid, start_time),
            ppid=ppid,
            command=command,
            state=state,
            cpu_ticks=cpu_ticks,
            io_bytes=io_bytes,
            io_operations=io_operations,
            context_switches=switches,
            thread_count=status_values.get("Threads", 0),
            elapsed_seconds=max(0.0, uptime - start_time / self.clock_ticks),
        )

    @staticmethod
    def _command_line(directory: Path, fallback: str) -> str:
        try:
            with (directory / "cmdline").open("rb") as handle:
                raw = handle.read(8 * 1024)
        except OSError:
            return fallback
        values = [value.decode(errors="replace") for value in raw.split(b"\0") if value]
        return " ".join(values) or fallback

    @staticmethod
    def _key_values(path: Path) -> dict[str, int]:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return {}
        values: dict[str, int] = {}
        for line in lines:
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            token = raw.strip().split(maxsplit=1)[0] if raw.strip() else ""
            try:
                values[key] = int(token)
            except ValueError:
                continue
        return values

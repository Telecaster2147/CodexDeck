"""Discover Codex processes and associate them with per-process homes."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from models import ProcessInfo
from utils import CommandRunner
from .paths import ProcReader, ResolvedInstance, resolve_instance


def classify_role(command: str, args: str) -> str:
    lowered = args.lower()
    if "codex-code-mode-host" in lowered or command.startswith("codex-code-mode"):
        return "component"
    if command in {"node", "nodejs"} and re.search(r"(?:^|/)bin/codex(?:\s|$)", args):
        return "launcher"
    if "app-server" in lowered:
        return "app-server"
    return "session"


def is_codex_process(command: str, args: str) -> bool:
    basename = os.path.basename(args.split(maxsplit=1)[0]) if args else command
    return bool(
        command == "codex"
        or command.startswith("codex-code-mode")
        or basename == "codex"
        or (command in {"node", "nodejs"} and re.search(r"(?:^|/)bin/codex(?:\s|$)", args))
    )


@dataclass
class DiscoveryResult:
    processes: list[ProcessInfo]
    instances: dict[str, ResolvedInstance]


class ProcessDiscovery:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        proc: ProcReader | None = None,
        user_id: int | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.proc = proc or ProcReader()
        self.user_id = os.getuid() if user_id is None else user_id
        self.path_cache: dict[str, tuple[float, ResolvedInstance, str]] = {}

    def discover(
        self,
        selected_pids: set[int] | None = None,
        selected_homes: set[Path] | None = None,
    ) -> DiscoveryResult:
        output = self.runner.run(
            [
                "ps",
                "-eo",
                "pid=,ppid=,uid=,pgid=,tpgid=,tty=,comm=,etimes=,pcpu=,stat=,"
                "wchan:32=,args=",
                "--cols",
                "4096",
            ]
        )
        processes: list[ProcessInfo] = []
        instances: dict[str, ResolvedInstance] = {}
        canonical_homes = {
            path.expanduser().resolve(strict=False) for path in selected_homes or set()
        }
        for raw_line in output.splitlines():
            fields = raw_line.strip().split(maxsplit=11)
            if len(fields) < 12:
                continue
            (
                pid_s,
                ppid_s,
                uid_s,
                pgid_s,
                tpgid_s,
                terminal,
                command,
                elapsed_s,
                cpu_s,
                state,
                wait_channel,
                args,
            ) = fields
            try:
                pid = int(pid_s)
                ppid = int(ppid_s)
                uid = int(uid_s)
                process_group_id = int(pgid_s)
                foreground_process_group_id = int(tpgid_s)
                elapsed = int(elapsed_s)
                cpu = float(cpu_s)
            except ValueError:
                continue
            if uid != self.user_id:
                continue
            if not is_codex_process(command, args) or (selected_pids and pid not in selected_pids):
                continue
            identity = self.proc.identity(pid)
            cached = self.path_cache.get(identity.key)
            if cached and time.monotonic() - cached[0] < 10:
                resolved, cwd = cached[1], cached[2]
            else:
                resolved = resolve_instance(pid, self.proc)
                cwd = str(self.proc.cwd(pid) or "")
                self.path_cache[identity.key] = (time.monotonic(), resolved, cwd)
            if canonical_homes and resolved.paths.codex_home not in canonical_homes:
                continue
            instances[resolved.instance_id] = resolved
            processes.append(
                ProcessInfo(
                    identity=identity,
                    ppid=ppid,
                    command=command,
                    elapsed_seconds=elapsed,
                    cpu_percent=cpu,
                    process_state=state,
                    wait_channel=wait_channel,
                    args=args,
                    role=classify_role(command, args),
                    cwd=cwd,
                    instance_id=resolved.instance_id,
                    discovery_method=resolved.method,
                    process_group_id=process_group_id,
                    foreground_process_group_id=foreground_process_group_id,
                    terminal=terminal,
                    instance_identity=resolved.identity,
                )
            )
        active = {process.stable_key for process in processes}
        self.path_cache = {key: value for key, value in self.path_cache.items() if key in active}
        by_pid = {process.pid: process for process in processes}
        for process in processes:
            if process.role == "session" or process.discovery_method not in {
                "default",
                "unresolved",
            }:
                continue
            relatives = [
                candidate
                for candidate in processes
                if candidate.role == "session"
                and (candidate.pid == process.ppid or candidate.ppid == process.pid)
            ]
            if not relatives:
                parent = by_pid.get(process.ppid)
                if parent and parent.role != process.role:
                    relatives = [parent]
            if relatives:
                relative = relatives[0]
                process.instance_id = relative.instance_id
                process.discovery_method = "process-family"
                process.instance_identity = relative.instance_identity
        used_instances = {process.instance_id for process in processes}
        instances = {key: value for key, value in instances.items() if key in used_instances}
        return DiscoveryResult(sorted(processes, key=lambda item: item.pid), instances)

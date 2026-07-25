"""Discover Codex processes and associate them with per-process homes."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from models import (
    Confidence,
    DiscoveryCandidateDiagnostic,
    DiscoverySummary,
    ProcessInfo,
)
from utils import CommandBudget, CommandError, CommandExecutionResult, CommandRunner
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


def is_codex_candidate(command: str, args: str) -> bool:
    """Return whether cheap process-list evidence merits a bounded confirmation probe."""

    basename = os.path.basename(args.split(maxsplit=1)[0]) if args else command
    command_lower = command.lower()
    basename_lower = basename.lower()
    codex_token = re.search(r"(?:^|[\s/])codex(?:[-_][\w.-]+)?(?:\s|$)", args.lower())
    return bool(
        command_lower == "codex"
        or command_lower.startswith("codex-code-mode")
        or basename_lower == "codex"
        or (basename_lower.startswith("codex-") and basename_lower != "codexdeck")
        or (command in {"node", "nodejs"} and re.search(r"(?:^|/)bin/codex(?:\s|$)", args))
        or bool(codex_token)
    )


is_codex_process = is_codex_candidate


@dataclass
class DiscoveryResult:
    processes: list[ProcessInfo]
    instances: dict[str, ResolvedInstance]
    summary: DiscoverySummary = DiscoverySummary()
    command_result: CommandExecutionResult | None = None


PROCESS_COMMAND_BUDGET = CommandBudget(
    stdout_bytes=8 * 1024 * 1024,
    stdout_retained_bytes=2 * 1024 * 1024,
    stderr_bytes=64 * 1024,
    stderr_retained_bytes=16 * 1024,
    stdout_lines=100_000,
    stderr_lines=1_024,
    retained_records=8_192,
)


@dataclass
class _Candidate:
    process: ProcessInfo
    resolved: ResolvedInstance
    environment_readable: bool
    confirmed: bool = False
    conflict_reason: str = ""


def _confirmation_evidence(
    environment: dict[str, str] | None,
    targets: list[Path],
    cwd: Path,
) -> tuple[tuple[str, ...], str]:
    evidence: list[str] = []
    if environment and any(
        str(environment.get(name, "")).strip() for name in ("CODEX_HOME", "CODEX_SQLITE_HOME")
    ):
        evidence.append("official_environment")
    rollout_homes: set[Path] = set()
    sqlite_homes: set[Path] = set()
    for target in targets:
        if target.suffix == ".jsonl" and "sessions" in target.parts:
            index = target.parts.index("sessions")
            if index > 0:
                rollout_homes.add(Path(*target.parts[:index]).resolve(strict=False))
        if target.suffix == ".sqlite" and target.name.startswith(("state_", "logs_")):
            sqlite_homes.add(target.parent.resolve(strict=False))
    if rollout_homes:
        evidence.extend(("active_rollout_file", "session_identity"))
    if sqlite_homes:
        evidence.append("active_sqlite_file")

    environment = environment or {}
    raw_codex_home = environment.get("CODEX_HOME", "").strip()
    if raw_codex_home:
        path = Path(raw_codex_home).expanduser()
        environment_home = (path if path.is_absolute() else cwd / path).resolve(strict=False)
        if rollout_homes and rollout_homes != {environment_home}:
            return tuple(evidence), "conflicting_codex_home_evidence"
    raw_sqlite_home = environment.get("CODEX_SQLITE_HOME", "").strip()
    if raw_sqlite_home:
        path = Path(raw_sqlite_home).expanduser()
        environment_home = (path if path.is_absolute() else cwd / path).resolve(strict=False)
        if sqlite_homes and sqlite_homes != {environment_home}:
            return tuple(evidence), "conflicting_sqlite_home_evidence"
    if len(rollout_homes) > 1:
        return tuple(evidence), "multiple_rollout_homes"
    if len(sqlite_homes) > 1:
        return tuple(evidence), "multiple_sqlite_homes"
    return tuple(evidence), ""


def _discovery_method(evidence: tuple[str, ...]) -> str:
    if "active_rollout_file" in evidence or "active_sqlite_file" in evidence:
        return "file-descriptor"
    return "environment"


class ProcessDiscovery:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        proc: ProcReader | None = None,
        user_id: int | None = None,
        labeled_codex_pids: set[int] | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.proc = proc or ProcReader()
        self.user_id = os.getuid() if user_id is None else user_id
        self.labeled_codex_pids = labeled_codex_pids
        self.path_cache: dict[str, tuple[float, ResolvedInstance, str]] = {}

    def discover(
        self,
        selected_pids: set[int] | None = None,
        selected_homes: set[Path] | None = None,
    ) -> DiscoveryResult:
        columns = "pid=,ppid=,uid=,pgid=,tpgid=,tty=,comm=,etimes=,pcpu=,stat=,wchan:32=,args="
        ps_command = ["ps"]
        if selected_pids:
            ps_command.extend(("-p", ",".join(str(pid) for pid in sorted(selected_pids))))
        else:
            ps_command.extend(("-U", str(self.user_id)))
        ps_command.extend(("-o", columns, "--cols", "4096"))

        def retain_candidate(raw_line: str) -> bool:
            fields = raw_line.strip().split(maxsplit=11)
            if len(fields) < 12:
                return False
            try:
                pid = int(fields[0])
                uid = int(fields[2])
            except ValueError:
                return False
            return bool(
                uid == self.user_id
                and (not selected_pids or pid in selected_pids)
                and is_codex_candidate(fields[6], fields[11])
            )

        run_result = getattr(self.runner, "run_result", None)
        command_result: CommandExecutionResult | None = None
        if callable(run_result):
            command_result = run_result(
                ps_command,
                budget=PROCESS_COMMAND_BUDGET,
                stdout_line_filter=retain_candidate,
            )
            if command_result.stderr.strip():
                command_result = replace(
                    command_result,
                    complete=False,
                    reason="stderr_warning" if command_result.stdout.strip() else "stderr_output",
                )
                if not command_result.stdout.strip():
                    raise CommandError("stderr_output", "ps", command_result)
            output = command_result.stdout
        else:
            output = self.runner.run(ps_command)
        candidates: list[_Candidate] = []
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
            if not is_codex_candidate(command, args) or (
                selected_pids and pid not in selected_pids
            ):
                continue
            environment = self.proc.environ(pid)
            targets = self.proc.fd_targets(pid)
            process_cwd = self.proc.cwd(pid) or Path.cwd()
            evidence, conflict_reason = _confirmation_evidence(
                environment,
                targets,
                process_cwd,
            )
            identity = self.proc.identity(pid)
            cached = self.path_cache.get(identity.key)
            if cached and time.monotonic() - cached[0] < 10:
                resolved, cwd = cached[1], cached[2]
            else:
                resolved = resolve_instance(pid, self.proc)
                cwd = str(process_cwd)
                self.path_cache[identity.key] = (time.monotonic(), resolved, cwd)
            candidates.append(
                _Candidate(
                    process=ProcessInfo(
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
                        discovery_method=(
                            _discovery_method(evidence) if evidence else resolved.method
                        ),
                        process_group_id=process_group_id,
                        foreground_process_group_id=foreground_process_group_id,
                        terminal=terminal,
                        discovery_confidence=(Confidence.HIGH if evidence else Confidence.LOW),
                        discovery_evidence=evidence,
                        instance_identity=resolved.identity,
                    ),
                    resolved=resolved,
                    environment_readable=environment is not None,
                    confirmed=bool(evidence) and not conflict_reason,
                    conflict_reason=conflict_reason,
                )
            )

        # Only direct parent/child ancestry can promote a name-only candidate. Iterate so a
        # wrapper chain can inherit from a directly confirmed leaf without widening the family.
        changed = True
        while changed:
            changed = False
            confirmed = [candidate for candidate in candidates if candidate.confirmed]
            for candidate in candidates:
                if candidate.confirmed or candidate.conflict_reason:
                    continue
                relative = next(
                    (
                        item
                        for item in confirmed
                        if item.process.pid == candidate.process.ppid
                        or item.process.ppid == candidate.process.pid
                    ),
                    None,
                )
                if relative is None:
                    continue
                candidate.confirmed = True
                candidate.resolved = relative.resolved
                candidate.process.instance_id = relative.process.instance_id
                candidate.process.instance_identity = relative.process.instance_identity
                candidate.process.discovery_method = "process-family"
                candidate.process.discovery_confidence = Confidence.MEDIUM
                candidate.process.discovery_evidence = ("trusted_ancestry",)
                changed = True

        included = [
            candidate
            for candidate in candidates
            if not canonical_homes or candidate.resolved.paths.codex_home in canonical_homes
        ]
        confirmed_candidates = [candidate for candidate in included if candidate.confirmed]
        processes = [candidate.process for candidate in confirmed_candidates]
        instances = {
            candidate.resolved.instance_id: candidate.resolved for candidate in confirmed_candidates
        }
        active = {process.stable_key for process in processes}
        self.path_cache = {key: value for key, value in self.path_cache.items() if key in active}

        rejected = [
            candidate
            for candidate in included
            if not candidate.confirmed
            and candidate.environment_readable
            and not candidate.conflict_reason
        ]
        unresolved = [
            candidate
            for candidate in included
            if not candidate.confirmed
            and (not candidate.environment_readable or bool(candidate.conflict_reason))
        ]
        diagnostics = tuple(
            DiscoveryCandidateDiagnostic(
                pid=candidate.process.pid,
                command=candidate.process.command,
                role=candidate.process.role,
                outcome=(
                    "rejected"
                    if candidate.environment_readable and not candidate.conflict_reason
                    else "unresolved"
                ),
                reason=candidate.conflict_reason
                or (
                    "no_confirming_codex_evidence"
                    if candidate.environment_readable
                    else "process_environment_unreadable"
                ),
            )
            for candidate in [*rejected, *unresolved][:64]
        )
        labeled = self.labeled_codex_pids
        confirmed_pids = {candidate.process.pid for candidate in confirmed_candidates}
        true_positive = len(confirmed_pids & labeled) if labeled is not None else 0
        false_positive = len(confirmed_pids - labeled) if labeled is not None else 0
        false_negative = len(labeled - confirmed_pids) if labeled is not None else 0
        summary = DiscoverySummary(
            candidates=len(included),
            confirmed=len(confirmed_candidates),
            rejected=len(rejected),
            unresolved=len(unresolved),
            diagnostics=diagnostics,
            labeled_true_positive=true_positive,
            labeled_false_positive=false_positive,
            labeled_false_negative=false_negative,
            precision=(
                true_positive / (true_positive + false_positive)
                if labeled is not None and true_positive + false_positive
                else None
            ),
            recall=(
                true_positive / (true_positive + false_negative)
                if labeled is not None and true_positive + false_negative
                else None
            ),
        )
        return DiscoveryResult(
            sorted(processes, key=lambda item: item.pid),
            instances,
            summary,
            command_result,
        )

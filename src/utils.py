"""Small cross-layer helpers and injectable operating-system boundaries."""

from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from config import COMMAND_TIMEOUT


@dataclass(frozen=True)
class CommandBudget:
    stdout_bytes: int = 8 * 1024 * 1024
    stdout_retained_bytes: int = 2 * 1024 * 1024
    stderr_bytes: int = 64 * 1024
    stderr_retained_bytes: int = 16 * 1024
    stdout_lines: int = 100_000
    stderr_lines: int = 1_024
    retained_records: int = 8_192
    read_chunk_bytes: int = 8_192


@dataclass(frozen=True)
class CommandExecutionResult:
    command_name: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    complete: bool = False
    reason: str = ""
    duration_seconds: float = 0.0
    stdout_bytes_read: int = 0
    stdout_bytes_retained: int = 0
    stdout_bytes_filtered: int = 0
    stderr_bytes_read: int = 0
    stderr_bytes_retained: int = 0
    stdout_lines_read: int = 0
    stderr_lines_read: int = 0
    records_retained: int = 0
    records_filtered: int = 0
    records_dropped: int = 0


class CommandError(RuntimeError):
    def __init__(
        self,
        reason: str,
        command_name: str = "",
        result: CommandExecutionResult | None = None,
    ) -> None:
        self.reason = reason
        self.command_name = command_name
        self.result = result
        detail = reason
        if result is not None:
            detail += (
                f" stdout_read={result.stdout_bytes_read}"
                f" stdout_retained={result.stdout_bytes_retained}"
                f" stderr_read={result.stderr_bytes_read}"
                f" records={result.records_retained}"
            )
            if result.exit_code is not None:
                detail += f" exit={result.exit_code}"
        super().__init__(f"{command_name}: {detail}" if command_name else detail)


class PrivateFileError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrivateFileHandle:
    path: Path
    descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        os.close(self.descriptor)

    def verify_path(self) -> None:
        try:
            file_stat = os.fstat(self.descriptor)
            path_stat = os.lstat(self.path)
        except OSError as exc:
            raise PrivateFileError(f"私有文件身份检查失败：{exc}") from exc
        expected = (self.device, self.inode)
        if (file_stat.st_dev, file_stat.st_ino) != expected:
            raise PrivateFileError("私有文件 descriptor 身份已变化")
        if (path_stat.st_dev, path_stat.st_ino) != expected:
            raise PrivateFileError("私有文件路径在打开后被替换")
        if not stat.S_ISREG(file_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise PrivateFileError("私有文件目标不是 regular file")
        if file_stat.st_uid != os.getuid() or path_stat.st_uid != os.getuid():
            raise PrivateFileError("私有文件目标不属于当前用户")


def open_private_regular_file(
    path: str | Path,
    flags: int,
    *,
    create_parent: bool = True,
    tighten_mode: bool = True,
) -> PrivateFileHandle:
    """Open an owner-only regular file without following the final path component."""

    requested = Path(path).expanduser()
    if create_parent:
        requested.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    normalized = requested.parent.resolve(strict=True) / requested.name
    try:
        parent_stat = os.lstat(normalized.parent)
    except OSError as exc:
        raise PrivateFileError(f"私有文件父目录检查失败：{exc}") from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise PrivateFileError("私有文件父路径不是 directory")
    if parent_stat.st_uid != os.getuid():
        raise PrivateFileError("私有文件父目录不属于当前用户")
    if stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise PrivateFileError("私有文件父目录允许 group/world 写入")
    open_flags = flags | os.O_CLOEXEC | os.O_NONBLOCK
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(normalized, open_flags | nofollow, 0o600)
    except OSError as exc:
        raise PrivateFileError(f"私有文件打开失败：{exc}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PrivateFileError("私有文件目标不是 regular file")
        if file_stat.st_uid != os.getuid():
            raise PrivateFileError("私有文件目标不属于当前用户")
        current_mode = stat.S_IMODE(file_stat.st_mode)
        if current_mode != 0o600 and tighten_mode:
            os.fchmod(descriptor, 0o600)
            file_stat = os.fstat(descriptor)
        elif current_mode != 0o600:
            raise PrivateFileError("私有文件权限必须为 0600")
        handle = PrivateFileHandle(
            normalized,
            descriptor,
            file_stat.st_dev,
            file_stat.st_ino,
        )
        handle.verify_path()
        return handle
    except Exception:
        os.close(descriptor)
        raise


class CommandRunner:
    """Run bounded read-only system commands used by the monitor."""

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.5)

    def run_result(
        self,
        command: Sequence[str],
        timeout: float = COMMAND_TIMEOUT,
        *,
        budget: CommandBudget | None = None,
        stdout_line_filter: Callable[[str], bool] | None = None,
    ) -> CommandExecutionResult:
        limits = budget or CommandBudget()
        command_name = Path(command[0]).name if command else "command"
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise CommandError("missing", command_name) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout_retained = bytearray()
        stderr_retained = bytearray()
        stdout_pending = bytearray()
        stdout_bytes_read = 0
        stderr_bytes_read = 0
        stdout_lines = 0
        stderr_lines = 0
        retained_records = 0
        filtered_records = 0
        filtered_bytes = 0
        dropped_records = 0
        failure_reason = ""

        def retain_stdout(line: bytes) -> None:
            nonlocal retained_records, filtered_records, filtered_bytes
            nonlocal dropped_records, failure_reason
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if stdout_line_filter is not None and not stdout_line_filter(text):
                filtered_records += 1
                filtered_bytes += len(line)
                return
            if retained_records >= limits.retained_records:
                dropped_records += 1
                failure_reason = "stdout_record_budget"
                return
            if len(stdout_retained) + len(line) > limits.stdout_retained_bytes:
                dropped_records += 1
                failure_reason = "stdout_retained_budget"
                return
            retained_records += 1
            stdout_retained.extend(line)

        try:
            while selector.get_map() and not failure_reason:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    failure_reason = "timeout"
                    break
                events = selector.select(min(0.05, remaining))
                if not events and process.poll() is not None:
                    events = selector.select(0)
                    if not events:
                        break
                for key, _ in events:
                    stream = str(key.data)
                    read_so_far = stdout_bytes_read if stream == "stdout" else stderr_bytes_read
                    byte_limit = limits.stdout_bytes if stream == "stdout" else limits.stderr_bytes
                    request = min(limits.read_chunk_bytes, max(1, byte_limit - read_so_far + 1))
                    file_object = key.fileobj
                    descriptor = (
                        file_object if isinstance(file_object, int) else file_object.fileno()
                    )
                    chunk = os.read(descriptor, request)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if stream == "stdout":
                        stdout_bytes_read += len(chunk)
                        if stdout_bytes_read > limits.stdout_bytes:
                            failure_reason = "stdout_byte_budget"
                            break
                        stdout_pending.extend(chunk)
                        while b"\n" in stdout_pending and not failure_reason:
                            newline = stdout_pending.index(b"\n") + 1
                            line = bytes(stdout_pending[:newline])
                            del stdout_pending[:newline]
                            stdout_lines += 1
                            if stdout_lines > limits.stdout_lines:
                                failure_reason = "stdout_line_budget"
                                break
                            retain_stdout(line)
                    else:
                        stderr_bytes_read += len(chunk)
                        stderr_lines += chunk.count(b"\n")
                        if stderr_bytes_read > limits.stderr_bytes:
                            failure_reason = "stderr_byte_budget"
                            break
                        if stderr_lines > limits.stderr_lines:
                            failure_reason = "stderr_line_budget"
                            break
                        remaining_stderr = limits.stderr_retained_bytes - len(stderr_retained)
                        stderr_retained.extend(chunk[: max(0, remaining_stderr)])
            if failure_reason:
                self._stop_process(process)
            else:
                if stdout_pending:
                    stdout_lines += 1
                    if stdout_lines > limits.stdout_lines:
                        failure_reason = "stdout_line_budget"
                    else:
                        retain_stdout(bytes(stdout_pending))
                if stderr_bytes_read and not stderr_retained.endswith(b"\n"):
                    stderr_lines += 1
                if stderr_lines > limits.stderr_lines:
                    failure_reason = "stderr_line_budget"
                if process.poll() is None:
                    process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            failure_reason = "timeout"
            self._stop_process(process)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                self._stop_process(process)

        result = CommandExecutionResult(
            command_name=command_name,
            stdout=stdout_retained.decode("utf-8", errors="replace"),
            stderr=stderr_retained.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
            complete=not failure_reason and process.returncode == 0,
            reason=failure_reason or ("nonzero_exit" if process.returncode else ""),
            duration_seconds=max(0.0, time.monotonic() - started),
            stdout_bytes_read=stdout_bytes_read,
            stdout_bytes_retained=len(stdout_retained),
            stdout_bytes_filtered=filtered_bytes,
            stderr_bytes_read=stderr_bytes_read,
            stderr_bytes_retained=len(stderr_retained),
            stdout_lines_read=stdout_lines,
            stderr_lines_read=stderr_lines,
            records_retained=retained_records,
            records_filtered=filtered_records,
            records_dropped=dropped_records,
        )
        if not result.complete:
            raise CommandError(result.reason or "incomplete", command_name, result)
        return result

    def run(self, command: Sequence[str], timeout: float = COMMAND_TIMEOUT) -> str:
        return self.run_result(command, timeout).stdout


def format_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def message_text(item: dict[str, object]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def compact_path(path: str | Path) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + "/"):
        return "~" + value[len(home) :]
    return value


BIDI_CONTROLS = frozenset(
    chr(value)
    for value in (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)
ZERO_WIDTH_CONTROLS = frozenset(chr(value) for value in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))


def contains_invisible_text(value: str) -> bool:
    return any(
        character in BIDI_CONTROLS
        or character in ZERO_WIDTH_CONTROLS
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )


def _display_cells(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def operator_text(value: object, *, max_cells: int = 160, max_characters: int = 512) -> str:
    """Render decision-bearing external text without invisible layout control."""

    source = str(value)
    rendered: list[str] = []
    cells = 0
    combining_run = 0
    for index, character in enumerate(source):
        if index >= max_characters:
            marker = "...[chars]"
        elif (
            character in BIDI_CONTROLS
            or character in ZERO_WIDTH_CONTROLS
            or unicodedata.category(character) in {"Cc", "Cf"}
        ):
            marker = f"<U+{ord(character):04X}>"
            combining_run = 0
        elif unicodedata.combining(character):
            combining_run += 1
            marker = character if combining_run <= 4 else f"<U+{ord(character):04X}>"
        else:
            combining_run = 0
            marker = character
        marker_cells = sum(_display_cells(item) for item in marker)
        if cells + marker_cells > max_cells:
            while rendered and cells + 3 > max_cells:
                removed = rendered.pop()
                cells -= sum(_display_cells(item) for item in removed)
            rendered.append("...")
            break
        rendered.append(marker)
        cells += marker_cells
        if index >= max_characters:
            break
    return "".join(rendered)


def redact_sensitive(text: str) -> str:
    """Best-effort redaction for known credential formats in local text."""

    if not text:
        return text
    redacted = re.sub(
        r"(?is)-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
        r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
    )
    redacted = re.sub(
        r"(?im)^(authorization\s*:\s*basic)\s+[A-Za-z0-9+/=]+\s*$",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)^((?:set-)?cookie\s*:)\s*[^\r\n]+",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b((?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|"
        r"amqps?)://)[^\s/@:]+:[^\s/@]+@",
        r"\1[REDACTED]@",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password|passwd|aws_access_key_id|"
        r"aws_secret_access_key|aws_session_token)"
        r"([\s\"']*[:=][\s\"']*)([^\s\"',;}]+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", redacted)
    redacted = re.sub(
        r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b",
        "[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b",
        "[REDACTED]",
        redacted,
    )
    return redacted


SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "set_cookie",
        "token",
    }
)
SENSITIVE_FIELD_SUFFIXES = (
    "_api_key",
    "_cookie",
    "_credential",
    "_credentials",
    "_password",
    "_passwd",
    "_private_key",
    "_secret",
    "_token",
)


def redact_structured(value: Any) -> Any:
    """Redact secret-bearing fields before diagnostic payload serialization."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if normalized in SENSITIVE_FIELD_NAMES or normalized.endswith(SENSITIVE_FIELD_SUFFIXES):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_structured(item)
        return redacted
    if isinstance(value, list):
        return [redact_structured(item) for item in value]
    if isinstance(value, tuple):
        return [redact_structured(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value)
    return value


TRANSCRIPT_BODY_FIELDS = frozenset(
    {
        "aggregated_output",
        "chunks",
        "diagnostic_payload",
        "formatted_output",
        "interaction_input",
        "output",
        "stderr",
        "stdout",
        "transcript",
    }
)


def strip_transcript_bodies(value: Any) -> Any:
    """Return a public/history projection with terminal body fields removed."""

    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "chunks":
                continue
            if name in TRANSCRIPT_BODY_FIELDS:
                projected[name] = ""
            else:
                projected[name] = strip_transcript_bodies(item)
        return projected
    if isinstance(value, list):
        return [strip_transcript_bodies(item) for item in value]
    if isinstance(value, tuple):
        return [strip_transcript_bodies(item) for item in value]
    return value


def one_line(text: str) -> str:
    return " ".join(text.split())

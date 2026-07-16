"""Read Codex SQLite state and structured logs in bounded read-only batches."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import SQLITE_TIMEOUT
from models import CodexPaths, SourceCapabilities


THREAD_COLUMNS = {
    "id",
    "rollout_path",
    "cwd",
    "title",
    "model",
    "reasoning_effort",
    "preview",
    "first_user_message",
}
LOG_COLUMNS = {"id", "ts", "level", "target", "thread_id", "process_uuid"}


@dataclass(frozen=True)
class ThreadRecord:
    session_id: str
    rollout_path: str
    cwd: str
    title: str
    model: str
    reasoning_effort: str
    preview: str
    first_user_message: str


@dataclass(frozen=True)
class LogRecord:
    log_id: int
    timestamp: float
    level: str
    target: str
    thread_id: str
    process_uuid: str
    body: str


def _version(path: Path) -> int:
    match = re.search(r"_(\d+)\.sqlite$", path.name)
    return int(match.group(1)) if match else -1


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT)
    connection.execute("PRAGMA query_only=ON")
    connection.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT * 1000)}")
    return connection


class StateStore:
    def __init__(self, paths: CodexPaths) -> None:
        self.paths = paths
        self.state_db, self.state_connection = self._select_db(
            "state",
            paths.state_db,
            "threads",
            {"id"},
        )
        self.log_db, self.log_connection = self._select_db(
            "logs",
            paths.log_db,
            "logs",
            {"id", "ts", "target"},
        )
        self._state_identity = self._identity(self.state_db)
        self._log_identity = self._identity(self.log_db)
        self.capabilities = self._capabilities()

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    def is_current(self) -> bool:
        """Return false when Codex replaces either selected database file."""

        return (
            self._identity(self.state_db) == self._state_identity
            and self._identity(self.log_db) == self._log_identity
        )

    def close(self) -> None:
        for connection in (self.state_connection, self.log_connection):
            if connection is not None:
                connection.close()

    def _select_db(
        self,
        prefix: str,
        preferred: Path,
        table: str,
        required: set[str],
    ) -> tuple[Path, sqlite3.Connection | None]:
        candidates = [preferred]
        candidates.extend(
            sorted(
                (
                    path
                    for path in self.paths.sqlite_home.glob(f"{prefix}_*.sqlite")
                    if path != preferred
                ),
                key=_version,
                reverse=True,
            )
        )
        for path in candidates:
            if not path.exists():
                continue
            try:
                connection = _open(path)
                if required <= _columns(connection, table):
                    return path, connection
                connection.close()
            except sqlite3.Error:
                continue
        return preferred, None

    def _capabilities(self) -> SourceCapabilities:
        thread_columns = (
            _columns(self.state_connection, "threads") if self.state_connection else set()
        )
        log_columns = (
            _columns(self.log_connection, "logs") if self.log_connection else set()
        )
        return SourceCapabilities(
            threads="id" in thread_columns,
            rollout_path="rollout_path" in thread_columns,
            logs="id" in log_columns,
            thread_id="thread_id" in log_columns,
            process_uuid="process_uuid" in log_columns,
        )

    def active_threads(self, pids: Iterable[int], cutoff: int = 0) -> dict[int, str]:
        pids = sorted(set(pids))
        if not pids or not (
            self.log_connection
            and self.capabilities.logs
            and self.capabilities.process_uuid
            and self.capabilities.thread_id
        ):
            return {}
        result: dict[int, str] = {}
        try:
            for pid in pids:
                row = self.log_connection.execute(
                    "SELECT thread_id FROM logs WHERE ts >= ? AND process_uuid LIKE ? "
                    "AND thread_id IS NOT NULL AND thread_id != '' "
                    "ORDER BY id DESC LIMIT 1",
                    (cutoff, f"pid:{pid}:%"),
                ).fetchone()
                if row:
                    result[pid] = str(row[0])
        except sqlite3.Error:
            return {}
        return result

    def threads(self, session_ids: Iterable[str]) -> dict[str, ThreadRecord]:
        ids = sorted({session_id for session_id in session_ids if session_id})
        if not ids or not self.capabilities.threads or self.state_connection is None:
            return {}
        available: set[str]
        try:
            available = _columns(self.state_connection, "threads")
            wanted = [column for column in THREAD_COLUMNS if column in available]
            placeholders = ",".join("?" for _ in ids)
            rows = self.state_connection.execute(
                f"SELECT {','.join(wanted)} FROM threads WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        except sqlite3.Error:
            return {}
        records: dict[str, ThreadRecord] = {}
        for row in rows:
            values = dict(zip(wanted, row))
            session_id = str(values.get("id") or "")
            records[session_id] = ThreadRecord(
                session_id=session_id,
                rollout_path=str(values.get("rollout_path") or ""),
                cwd=str(values.get("cwd") or ""),
                title=str(values.get("title") or ""),
                model=str(values.get("model") or ""),
                reasoning_effort=str(values.get("reasoning_effort") or ""),
                preview=str(values.get("preview") or ""),
                first_user_message=str(values.get("first_user_message") or ""),
            )
        return records

    def logs_since(self, pids: Iterable[int], cursor: int, cutoff: int) -> list[LogRecord]:
        pids = sorted(set(pids))
        if not pids or not self.capabilities.logs or self.log_connection is None:
            return []
        try:
            available = _columns(self.log_connection, "logs")
            clauses = " OR ".join("process_uuid LIKE ?" for _ in pids)
            cursor_clause = "id > ?" if cursor else "ts >= ?"
            params: list[object] = [cursor if cursor else cutoff]
            params.extend(f"pid:{pid}:%" for pid in pids)
            timestamp = (
                "ts + ts_nanos / 1000000000.0" if "ts_nanos" in available else "ts"
            )
            level = "level" if "level" in available else "''"
            thread_id = (
                "coalesce(thread_id, '')" if "thread_id" in available else "''"
            )
            process_uuid = (
                "coalesce(process_uuid, '')" if "process_uuid" in available else "''"
            )
            body_columns = [
                column
                for column in ("message", "feedback_log_body")
                if column in available
            ]
            body_value = (
                f"coalesce({','.join(body_columns)}, '')" if body_columns else "''"
            )
            body = f"substr({body_value}, 1, 8192)"
            targets = (
                "'codex_core::responses_retry','codex_http_client::transport',"
                "'codex_api::sse::responses','codex_core::tasks',"
                "'codex_core::session::turn'"
            )
            query = (
                f"SELECT id, {timestamp}, {level}, target, {thread_id}, "
                f"{process_uuid}, {body} FROM logs WHERE {cursor_clause} "
                f"AND target IN ({targets}) AND ({clauses}) ORDER BY id LIMIT 5000"
            )
            rows = self.log_connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        return [
            LogRecord(
                int(row[0]),
                float(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
            )
            for row in rows
        ]

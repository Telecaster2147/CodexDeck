"""Linux process, Codex session, and TCP socket collection and assessment."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .config import (
    MAX_SESSION_TAIL,
    SESSION_INDEX,
    STATE_ACTIVE,
    STATE_AUXILIARY,
    STATE_DB,
    STATE_DISCONNECTED,
    STATE_HEALTHY_IDLE,
    STATE_NETWORK_STALL,
    STATE_NO_OUTBOUND,
    STATE_UPSTREAM_WAIT,
    STATUS_PRIORITY,
)
from .models import ConnectionAssessment, ProcessAssessment, ProcessInfo, SocketInfo
from .utils import message_text


def run_command(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少命令：{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit={exc.returncode}"
        raise RuntimeError(f"命令执行失败：{' '.join(command)}：{detail}") from exc
    return completed.stdout


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
    if command == "codex" or command.startswith("codex-code-mode"):
        return True
    if basename == "codex":
        return True
    return bool(
        command in {"node", "nodejs"}
        and re.search(r"(?:^|/)bin/codex(?:\s|$)", args)
    )


def parse_ps_output(text: str) -> list[ProcessInfo]:
    processes: list[ProcessInfo] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split(maxsplit=7)
        if len(fields) < 8:
            continue
        pid_s, ppid_s, command, elapsed_s, cpu_s, state, wait_channel, args = fields
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
            elapsed = int(elapsed_s)
            cpu = float(cpu_s)
        except ValueError:
            continue
        if not is_codex_process(command, args):
            continue
        processes.append(
            ProcessInfo(
                pid=pid,
                ppid=ppid,
                command=command,
                elapsed_seconds=elapsed,
                cpu_percent=cpu,
                process_state=state,
                wait_channel=wait_channel,
                args=args,
                role=classify_role(command, args),
            )
        )
    return sorted(processes, key=lambda item: item.pid)


def compact_path(path: str) -> str:
    if not path:
        return "-"
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def open_rollout_paths(pid: int) -> list[Path]:
    paths: set[Path] = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        descriptors = list(fd_dir.iterdir())
    except OSError:
        return []
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if "/.codex/sessions/" not in target or not target.endswith(".jsonl"):
            continue
        paths.add(Path(target.removesuffix(" (deleted)")))
    return sorted(paths)


def rollout_identity(path: Path) -> tuple[str, bool]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return "", True
    if record.get("type") != "session_meta":
        return "", True
    payload = record.get("payload") or {}
    session_id = str(payload.get("session_id") or payload.get("id") or "")
    source = payload.get("source")
    is_subagent = isinstance(source, dict) and "subagent" in source
    return session_id, is_subagent


def main_rollout_for_pid(pid: int) -> tuple[Path | None, str]:
    candidates: list[tuple[bool, int, Path, str]] = []
    for path in open_rollout_paths(pid):
        session_id, is_subagent = rollout_identity(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        candidates.append((not is_subagent, size, path, session_id))
    if not candidates:
        return None, ""
    _, _, path, session_id = max(candidates, key=lambda item: (item[0], item[1]))
    return path, session_id


def load_session_names() -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        with SESSION_INDEX.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(record.get("id") or "")
                name = str(record.get("thread_name") or "").strip()
                if session_id and name:
                    names[session_id] = name
    except OSError:
        pass
    return names


def load_thread_config(session_id: str) -> dict[str, str]:
    if not session_id or not STATE_DB.exists():
        return {}
    query = (
        "SELECT title, cwd, model, reasoning_effort, preview, first_user_message "
        "FROM threads WHERE id = ?"
    )
    try:
        connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
        try:
            row = connection.execute(query, (session_id,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    keys = ("title", "cwd", "model", "reasoning_effort", "preview", "first_user_message")
    return {key: str(value or "") for key, value in zip(keys, row)}


def meaningful_task(text: str) -> str:
    text = text.strip()
    objective = re.search(r"<objective>\s*(.*?)\s*</objective>", text, re.DOTALL)
    if objective:
        text = objective.group(1)
    ignored_prefixes = (
        "<environment_context>",
        "<turn_aborted>",
        "# AGENTS.md instructions",
        "<codex_internal_context",
    )
    if not text or text.startswith(ignored_prefixes):
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def latest_user_task(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_SESSION_TAIL:
                handle.seek(size - MAX_SESSION_TAIL)
                handle.readline()
            payload = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    latest = ""
    for line in payload.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        item = record.get("payload") or {}
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
            continue
        candidate = meaningful_task(message_text(item))
        if candidate:
            latest = candidate
    return latest


def enrich_process(process: ProcessInfo, session_names: dict[str, str]) -> ProcessInfo:
    cwd = process_cwd(process.pid)
    if process.role in {"launcher", "component"}:
        return replace(process, cwd=cwd)
    rollout, session_id = main_rollout_for_pid(process.pid)
    config = load_thread_config(session_id)
    title = session_names.get(session_id, "") or config.get("title", "")
    fallback_task = config.get("preview", "") or config.get("first_user_message", "")
    task = latest_user_task(rollout) if rollout else ""
    if not task:
        task = meaningful_task(fallback_task)
    if process.role == "app-server" and not title:
        title = "VS Code Codex App Server"
    return replace(
        process,
        cwd=cwd or config.get("cwd", ""),
        session_id=session_id,
        session_title=" ".join(title.split()),
        current_task=task,
        model=config.get("model", ""),
        reasoning_effort=config.get("reasoning_effort", ""),
        rollout_path=str(rollout or ""),
    )


def discover_processes(selected_pids: set[int] | None = None) -> list[ProcessInfo]:
    output = run_command(
        [
            "ps",
            "-eo",
            "pid=,ppid=,comm=,etimes=,pcpu=,stat=,wchan:32=,args=",
            "--cols",
            "4096",
        ]
    )
    processes = parse_ps_output(output)
    if selected_pids:
        processes = [process for process in processes if process.pid in selected_pids]
    session_names = load_session_names()
    return [enrich_process(process, session_names) for process in processes]


def parse_endpoint(endpoint: str) -> tuple[str, int | None]:
    if endpoint.startswith("[") and "]:" in endpoint:
        host, port_s = endpoint[1:].rsplit("]:", 1)
    elif ":" in endpoint:
        host, port_s = endpoint.rsplit(":", 1)
    else:
        return endpoint, None
    try:
        return host, int(port_s)
    except ValueError:
        return host, None


def proxy_ports_from_environment() -> set[int]:
    ports = {1080, 10810, 7890, 7891, 8080}
    for name, value in os.environ.items():
        if "proxy" not in name.lower() or not value:
            continue
        match = re.search(r":(\d{2,5})(?:/|$)", value)
        if match:
            ports.add(int(match.group(1)))
    return ports


def classify_route(peer: str, proxy_ports: set[int] | None = None) -> str:
    host, port = parse_endpoint(peer)
    proxy_ports = proxy_ports or proxy_ports_from_environment()
    normalized = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "proxy" if port in proxy_ports else "loopback"
    if address.is_private or address.is_link_local:
        return "lan"
    return "external"


def metric(info: str, name: str, *, floating: bool = False) -> int | float | None:
    pattern = rf"(?:^|\s){re.escape(name)}:([0-9]+(?:\.[0-9]+)?)"
    match = re.search(pattern, info)
    if not match:
        return None
    return float(match.group(1)) if floating else int(float(match.group(1)))


def retrans_metrics(info: str) -> tuple[int, int]:
    match = re.search(r"(?:^|\s)retrans:(\d+)(?:/(\d+))?", info)
    if not match:
        return 0, 0
    current = int(match.group(1))
    total = int(match.group(2) or match.group(1))
    return current, total


def parse_ss_output(text: str, interested_pids: set[int]) -> dict[int, list[SocketInfo]]:
    by_pid: dict[int, list[SocketInfo]] = {pid: [] for pid in interested_pids}
    header_re = re.compile(
        r"^(?P<state>\S+)\s+(?P<recv>\d+)\s+(?P<send>\d+)\s+"
        r"(?P<local>\S+)\s+(?P<peer>\S+)(?:\s+(?P<owner>.*))?$"
    )
    current: list[SocketInfo] = []

    def apply_info(info: str) -> None:
        for socket in current:
            socket.bytes_sent = int(metric(info, "bytes_sent") or 0)
            socket.bytes_acked = int(metric(info, "bytes_acked") or 0)
            socket.bytes_received = int(metric(info, "bytes_received") or 0)
            socket.lastsnd_ms = metric(info, "lastsnd")  # type: ignore[assignment]
            socket.lastrcv_ms = metric(info, "lastrcv")  # type: ignore[assignment]
            socket.rtt_ms = metric(info, "rtt", floating=True)  # type: ignore[assignment]
            socket.retrans_current, socket.retrans_total = retrans_metrics(info)

    for raw_line in text.splitlines():
        if raw_line[:1].isspace():
            apply_info(raw_line.strip())
            continue
        match = header_re.match(raw_line.strip())
        if not match:
            current = []
            continue
        owner = match.group("owner") or ""
        pid_matches = [int(value) for value in re.findall(r"pid=(\d+)", owner)]
        fd_match = re.search(r"fd=(\d+)", owner)
        fd = int(fd_match.group(1)) if fd_match else None
        current = []
        for pid in pid_matches:
            if pid not in interested_pids:
                continue
            socket = SocketInfo(
                state=match.group("state"),
                recv_q=int(match.group("recv")),
                send_q=int(match.group("send")),
                local=match.group("local"),
                peer=match.group("peer"),
                pid=pid,
                fd=fd,
                route=classify_route(match.group("peer")),
            )
            by_pid[pid].append(socket)
            current.append(socket)
    return by_pid


def socket_snapshot(pids: set[int]) -> dict[int, list[SocketInfo]]:
    if not pids:
        return {}
    output = run_command(["ss", "-tinpH"])
    return parse_ss_output(output, pids)


def idle_seconds(socket: SocketInfo) -> float | None:
    values = [value for value in (socket.lastsnd_ms, socket.lastrcv_ms) if value is not None]
    if not values:
        return None
    return min(values) / 1000.0


def assess_connection(
    before: SocketInfo | None,
    after: SocketInfo,
    idle_threshold: float,
) -> ConnectionAssessment:
    sent_delta = max(0, after.bytes_sent - (before.bytes_sent if before else after.bytes_sent))
    received_delta = max(
        0, after.bytes_received - (before.bytes_received if before else after.bytes_received)
    )
    acked_delta = max(0, after.bytes_acked - (before.bytes_acked if before else after.bytes_acked))
    retrans_delta = max(
        0, after.retrans_total - (before.retrans_total if before else after.retrans_total)
    )
    progress = sent_delta + received_delta + acked_delta
    idle = idle_seconds(after)
    state = after.state.upper()

    if state in {"SYN-SENT", "SYN-RECV"} and progress == 0:
        health = STATE_NETWORK_STALL
        reason = "TCP 握手尚未完成，采样期间没有进展"
    elif after.send_q > 0 and progress == 0:
        health = STATE_NETWORK_STALL
        reason = f"发送队列积压 {after.send_q} 字节，采样期间没有 ACK 或字节进展"
    elif retrans_delta > 0 and progress == 0:
        health = STATE_NETWORK_STALL
        reason = f"重传增加 {retrans_delta} 次，采样期间没有有效进展"
    elif state not in {"ESTAB", "ESTABLISHED"}:
        health = STATE_DISCONNECTED
        reason = f"连接状态为 {state}"
    elif progress > 0:
        health = STATE_ACTIVE
        reason = (
            f"采样期间发送 +{sent_delta} B、接收 +{received_delta} B、"
            f"ACK +{acked_delta} B"
        )
    elif idle is not None and idle >= idle_threshold:
        health = STATE_UPSTREAM_WAIT
        reason = f"TCP 队列为空，最近业务流量约在 {idle:.1f} 秒前"
    else:
        health = STATE_HEALTHY_IDLE
        reason = "TCP 已建立、队列为空，当前采样窗口没有业务流量"

    return ConnectionAssessment(
        key=after.key,
        state=state,
        local=after.local,
        peer=after.peer,
        route=after.route,
        recv_q=after.recv_q,
        send_q=after.send_q,
        sent_delta=sent_delta,
        received_delta=received_delta,
        acked_delta=acked_delta,
        retrans_delta=retrans_delta,
        idle_seconds=idle,
        health=health,
        reason=reason,
    )


def assess_process(
    process: ProcessInfo,
    before: list[SocketInfo],
    after: list[SocketInfo],
    idle_threshold: float,
) -> ProcessAssessment:
    before_by_key = {socket.key: socket for socket in before}
    routed_after = [
        socket for socket in after if socket.route in {"external", "proxy", "lan"}
    ]
    routed_before = [
        socket for socket in before if socket.route in {"external", "proxy", "lan"}
    ]
    connections = [
        assess_connection(before_by_key.get(socket.key), socket, idle_threshold)
        for socket in routed_after
    ]

    if not connections:
        if routed_before:
            health = STATE_DISCONNECTED
            reason = "采样开始时存在外联，结束时连接已经消失"
            network_hang = "连接已断开，需结合重连日志判断"
        elif process.role in {"launcher", "component"}:
            health = STATE_AUXILIARY
            reason = "该进程是 Codex 启动器或辅助组件"
            network_hang = "否"
        else:
            health = STATE_NO_OUTBOUND
            reason = "当前没有归属于该 PID 的外部、代理或局域网 TCP 连接"
            network_hang = "缺少网络卡死证据"
        return ProcessAssessment(process, health, network_hang, reason, connections)

    health = max(connections, key=lambda item: STATUS_PRIORITY[item.health]).health
    selected = [connection for connection in connections if connection.health == health]
    reason = "; ".join(connection.reason for connection in selected)
    if health == STATE_NETWORK_STALL:
        network_hang = "是，存在持续队列/握手/重传异常"
    elif health == STATE_DISCONNECTED:
        network_hang = "连接异常，需要观察是否自动重连"
    elif health == STATE_UPSTREAM_WAIT:
        network_hang = "否，TCP 层通畅，更像等待上游响应"
    else:
        network_hang = "否"
    return ProcessAssessment(process, health, network_hang, reason, connections)

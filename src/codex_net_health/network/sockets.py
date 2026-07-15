"""Collect and parse Linux `ss -tinp` snapshots."""

from __future__ import annotations

import ipaddress
import os
import re

from ..models import SocketInfo
from ..utils import CommandRunner


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
    normalized = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "proxy" if port in (proxy_ports or proxy_ports_from_environment()) else "loopback"
    if address.is_private or address.is_link_local:
        return "lan"
    return "external"


def _metric(info: str, name: str, *, floating: bool = False) -> int | float | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}:([0-9]+(?:\.[0-9]+)?)", info)
    if not match:
        return None
    return float(match.group(1)) if floating else int(float(match.group(1)))


def _apply_info(sockets: list[SocketInfo], info: str) -> None:
    retrans = re.search(r"(?:^|\s)retrans:(\d+)(?:/(\d+))?", info)
    for socket in sockets:
        for attribute, metric_name, floating in (
            ("bytes_sent", "bytes_sent", False),
            ("bytes_acked", "bytes_acked", False),
            ("bytes_received", "bytes_received", False),
            ("lastsnd_ms", "lastsnd", False),
            ("lastrcv_ms", "lastrcv", False),
            ("rtt_ms", "rtt", True),
        ):
            value = _metric(info, metric_name, floating=floating)
            if value is not None:
                setattr(socket, attribute, value)
        if retrans:
            socket.retrans_current = int(retrans.group(1))
            socket.retrans_total = int(retrans.group(2) or retrans.group(1))


def parse_ss_output(text: str, interested_pids: set[int]) -> dict[int, list[SocketInfo]]:
    by_pid: dict[int, list[SocketInfo]] = {pid: [] for pid in interested_pids}
    header = re.compile(
        r"^(?P<state>\S+)\s+(?P<recv>\d+)\s+(?P<send>\d+)\s+"
        r"(?P<local>\S+)\s+(?P<peer>\S+)(?:\s+(?P<owner>.*))?$"
    )
    current: list[SocketInfo] = []
    for raw_line in text.splitlines():
        if raw_line[:1].isspace():
            _apply_info(current, raw_line.strip())
            continue
        match = header.match(raw_line.strip())
        if not match:
            current = []
            continue
        owner = match.group("owner") or ""
        pids = [int(value) for value in re.findall(r"pid=(\d+)", owner)]
        fd_match = re.search(r"fd=(\d+)", owner)
        fd = int(fd_match.group(1)) if fd_match else None
        current = []
        for pid in pids:
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


class SocketCollector:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def snapshot(self, pids: set[int]) -> dict[int, list[SocketInfo]]:
        if not pids:
            return {}
        return parse_ss_output(self.runner.run(["ss", "-tinpH"]), pids)

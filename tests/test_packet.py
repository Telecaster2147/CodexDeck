"""Unit tests for passive packet metadata decoding without live capture."""

from __future__ import annotations

import socket
import struct
import sys
import unittest
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cli import build_parser  # noqa: E402
from models import (  # noqa: E402
    CodexPaths,
    ConnectionAssessment,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    SocketInfo,
)
from network.packet import (  # noqa: E402
    PacketInspector,
    parse_ethernet_frame,
    parse_tls_client_hello,
)
from presentation.json_output import render_json  # noqa: E402
from presentation.text import render_text  # noqa: E402


def _extension(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value


def client_hello() -> bytes:
    """Return a compact valid ClientHello with SNI, ALPN, and TLS versions."""

    host = b"api.openai.com"
    server_name = struct.pack("!H", 3 + len(host)) + b"\x00" + struct.pack("!H", len(host)) + host
    alpn_items = b"\x02h2\x08http/1.1"
    extensions = b"".join(
        (
            _extension(0, server_name),
            _extension(16, struct.pack("!H", len(alpn_items)) + alpn_items),
            _extension(43, b"\x04\x03\x04\x03\x03"),
        )
    )
    body = b"".join(
        (
            b"\x03\x03",
            bytes(range(32)),
            b"\x00",
            b"\x00\x02\x13\x01",
            b"\x01\x00",
            struct.pack("!H", len(extensions)),
            extensions,
        )
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake


def client_hello_split_across_tls_records() -> bytes:
    record = client_hello()
    handshake = record[5:]
    first, second = handshake[:20], handshake[20:]
    return b"".join(
        (
            b"\x16\x03\x01" + struct.pack("!H", len(first)) + first,
            b"\x16\x03\x01" + struct.pack("!H", len(second)) + second,
        )
    )


def ethernet_ipv4(payload: bytes, *, sequence: int = 100, vlan: bool = False) -> bytes:
    tcp = struct.pack("!HHIIHHHH", 43122, 443, sequence, 0, 0x5018, 65535, 0, 0) + payload
    source = socket.inet_aton("192.0.2.10")
    destination = socket.inet_aton("198.51.100.20")
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(tcp),
        1,
        0,
        64,
        6,
        0,
        source,
        destination,
    )
    ethernet = b"\x00" * 12
    if vlan:
        return ethernet + struct.pack("!HHH", 0x8100, 7, 0x0800) + ip + tcp
    return ethernet + struct.pack("!H", 0x0800) + ip + tcp


def ethernet_ipv6(payload: bytes, *, sequence: int = 200) -> bytes:
    tcp = struct.pack("!HHIIHHHH", 43122, 443, sequence, 0, 0x5018, 65535, 0, 0) + payload
    ip = struct.pack(
        "!IHBB16s16s",
        0x60000000,
        len(tcp),
        6,
        64,
        socket.inet_pton(socket.AF_INET6, "2001:db8::10"),
        socket.inet_pton(socket.AF_INET6, "2606:4700::1111"),
    )
    return b"\x00" * 12 + struct.pack("!H", 0x86DD) + ip + tcp


def packet_output_snapshot() -> MonitorSnapshot:
    home = Path("/fixtures/codex-home")
    paths = CodexPaths(
        home,
        home,
        home / "state.sqlite",
        home / "logs.sqlite",
        home / "session-index.jsonl",
        home / "sessions",
    )
    process = ProcessInfo(
        ProcessIdentity(42, 10),
        1,
        "codex",
        1,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="fixture",
        session_id="session",
    )
    session = SessionHealth(
        "fixture",
        "session",
        process,
        LifecycleState.GENERATING,
        network=NetworkEvidence(NetworkState.IDLE),
    )
    instance = InstanceSnapshot(
        "fixture",
        paths,
        "CODEX_HOME",
        "CODEX_HOME",
        "fixture",
        processes=[process],
        sessions=[session],
    )
    return MonitorSnapshot("2026-07-16T00:00:00+00:00", 2.0, [instance])


class PacketParsingTests(unittest.TestCase):
    def test_cli_exposes_opt_in_packet_inspection(self) -> None:
        args = build_parser().parse_args(["--packet-inspection", "--once"])
        self.assertTrue(args.packet_inspection)
        self.assertTrue(args.once)

    def test_extracts_client_hello_metadata_from_ipv4_vlan_frame(self) -> None:
        payload = client_hello()
        segment = parse_ethernet_frame(ethernet_ipv4(payload, vlan=True))
        self.assertIsNotNone(segment)
        self.assertEqual(segment.flow.client_host, "192.0.2.10")
        self.assertEqual(segment.flow.server_port, 443)
        metadata = parse_tls_client_hello(segment.payload)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.server_name, "api.openai.com")
        self.assertEqual(metadata.alpn_protocols, ("h2", "http/1.1"))
        self.assertEqual(metadata.tls_versions, ("TLS 1.3", "TLS 1.2"))

    def test_extracts_client_hello_metadata_from_ipv6_frame(self) -> None:
        segment = parse_ethernet_frame(ethernet_ipv6(client_hello()))
        self.assertIsNotNone(segment)
        self.assertEqual(segment.flow.client_host, "2001:db8::10")
        metadata = parse_tls_client_hello(segment.payload)
        self.assertEqual(metadata.server_name, "api.openai.com")

    def test_reassembles_client_hello_across_tcp_segments(self) -> None:
        payload = client_hello()
        inspector = PacketInspector(clock=lambda: 1000.0, monotonic=lambda: 10.0)
        first = inspector.observe_frame(ethernet_ipv4(payload[:19], sequence=100), now=1000.0)
        second = inspector.observe_frame(ethernet_ipv4(payload[19:], sequence=119), now=1000.0)
        self.assertIsNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(second.metadata.server_name, "api.openai.com")

    def test_reassembles_client_hello_across_tls_records(self) -> None:
        inspector = PacketInspector(clock=lambda: 1000.0, monotonic=lambda: 10.0)
        observation = inspector.observe_frame(
            ethernet_ipv4(client_hello_split_across_tls_records()), now=1000.0
        )
        self.assertIsNotNone(observation)
        self.assertEqual(observation.metadata.server_name, "api.openai.com")

    def test_discards_truncated_or_non_tls_payloads(self) -> None:
        self.assertIsNone(parse_ethernet_frame(b"\x00" * 13))
        self.assertIsNone(parse_tls_client_hello(client_hello()[:12]))
        segment = parse_ethernet_frame(ethernet_ipv4(b"GET / HTTP/1.1\r\n"))
        self.assertIsNotNone(segment)
        self.assertIsNone(parse_tls_client_hello(segment.payload))
        fragmented = bytearray(ethernet_ipv4(client_hello()))
        fragmented[20:22] = b"\x20\x00"
        self.assertIsNone(parse_ethernet_frame(bytes(fragmented)))

    def test_annotates_only_the_exact_current_socket_flow(self) -> None:
        inspector = PacketInspector(clock=lambda: 1000.0, monotonic=lambda: 10.0)
        inspector.observe_frame(ethernet_ipv4(client_hello()), now=1000.0)
        matching = SocketInfo(
            "ESTAB", 0, 0, "192.0.2.10:43122", "198.51.100.20:443", 10, route="external"
        )
        different = SocketInfo(
            "ESTAB", 0, 0, "192.0.2.10:43123", "198.51.100.20:443", 10, route="external"
        )
        inspector.annotate({10: [matching, different]})
        self.assertEqual(matching.tls_server_name, "api.openai.com")
        self.assertEqual(matching.tls_alpn_protocols, ("h2", "http/1.1"))
        self.assertEqual(different.tls_server_name, "")

    def test_permission_failure_is_a_nonfatal_collector_status(self) -> None:
        def denied(*_args: object, **_kwargs: object) -> socket.socket:
            raise PermissionError("operation not permitted")

        inspector = PacketInspector(socket_factory=denied)
        self.assertFalse(inspector.start())
        self.assertIn("AF_PACKET", inspector.error)
        self.assertIn("operation not permitted", inspector.error)

    def test_packet_metadata_is_additive_in_json_and_text(self) -> None:
        snapshot = packet_output_snapshot()
        snapshot.sessions[0].network.connections = [
            ConnectionAssessment(
                "192.0.2.10:43122->198.51.100.20:443",
                "ESTAB",
                "192.0.2.10:43122",
                "198.51.100.20:443",
                "external",
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
                NetworkState.IDLE,
                "TCP 已建立且队列为空",
                tls_server_name="api.openai.com",
                tls_alpn_protocols=("h2",),
                tls_versions=("TLS 1.3",),
                tls_observed_at=100.0,
            )
        ]
        payload = json.loads(render_json(snapshot, pretty=False))
        connection = payload["instances"][0]["sessions"][0]["network"]["connections"][0]
        self.assertEqual(connection["tls_server_name"], "api.openai.com")
        self.assertEqual(connection["tls_alpn_protocols"], ["h2"])
        self.assertIn("TLS：SNI api.openai.com; ALPN h2; TLS TLS 1.3", render_text(snapshot))

if __name__ == "__main__":
    unittest.main()

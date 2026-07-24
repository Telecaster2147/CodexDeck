"""Passive TLS ClientHello metadata collection for Linux packet sockets.

The module deliberately retains only handshake metadata needed to explain a
TCP connection: server name, ALPN offers, TLS versions, and observation time.
It never stores application payloads, request bodies, or response contents.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from models import ProcessIdentity, SocketInfo
from network.sockets import parse_endpoint


ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
ETH_P_IPV6 = 0x86DD
_VLAN_ETHERTYPES = {0x8100, 0x88A8, 0x9100}
_TCP = 6
_TLS_HANDSHAKE = 22
_CLIENT_HELLO = 1
_MAX_REASSEMBLY_BYTES = 64 * 1024
_FLOW_TTL_SECONDS = 15.0
_OBSERVATION_TTL_SECONDS = 300.0
_MAX_OBSERVATIONS = 512
_ALLOWLIST_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class FlowKey:
    """A directional TCP flow, from the TLS client to its peer."""

    client_host: str
    client_port: int
    server_host: str
    server_port: int


@dataclass(frozen=True)
class TlsMetadata:
    """The bounded, non-content metadata extracted from a ClientHello."""

    server_name: str = ""
    alpn_protocols: tuple[str, ...] = ()
    tls_versions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PacketObservation:
    """A TLS ClientHello observation associated with a TCP flow."""

    flow: FlowKey
    metadata: TlsMetadata
    observed_at: float


@dataclass(frozen=True)
class TcpSegment:
    """Decoded TCP payload from a single Ethernet frame."""

    flow: FlowKey
    sequence: int
    payload: bytes


def _read_u16(data: bytes | bytearray, offset: int) -> int | None:
    if offset + 2 > len(data):
        return None
    return struct.unpack_from("!H", data, offset)[0]


def _normalise_host(host: str) -> str:
    """Produce the same address spelling for ``ss`` and packet frames."""

    value = host.strip().strip("[]").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


def _parse_tcp(data: bytes, offset: int, source: str, destination: str) -> TcpSegment | None:
    if offset + 20 > len(data):
        return None
    source_port, destination_port = struct.unpack_from("!HH", data, offset)
    sequence = struct.unpack_from("!I", data, offset + 4)[0]
    header_length = (data[offset + 12] >> 4) * 4
    if header_length < 20 or offset + header_length > len(data):
        return None
    return TcpSegment(
        flow=FlowKey(source, source_port, destination, destination_port),
        sequence=sequence,
        payload=data[offset + header_length :],
    )


def _parse_ipv4(data: bytes, offset: int) -> TcpSegment | None:
    if offset + 20 > len(data):
        return None
    version_and_length = data[offset]
    if version_and_length >> 4 != 4:
        return None
    header_length = (version_and_length & 0x0F) * 4
    if header_length < 20 or offset + header_length > len(data):
        return None
    total_length = _read_u16(data, offset + 2)
    if total_length is None or total_length < header_length:
        return None
    end = min(len(data), offset + total_length)
    fragment = _read_u16(data, offset + 6)
    if fragment is None or fragment & 0x3FFF:
        return None
    if data[offset + 9] != _TCP:
        return None
    source = socket.inet_ntop(socket.AF_INET, data[offset + 12 : offset + 16])
    destination = socket.inet_ntop(socket.AF_INET, data[offset + 16 : offset + 20])
    return _parse_tcp(data[offset:end], header_length, source, destination)


def _parse_ipv6(data: bytes, offset: int) -> TcpSegment | None:
    if offset + 40 > len(data) or data[offset] >> 4 != 6:
        return None
    payload_length = _read_u16(data, offset + 4)
    if payload_length is None:
        return None
    end = min(len(data), offset + 40 + payload_length)
    next_header = data[offset + 6]
    cursor = offset + 40
    # Skip common extension headers while retaining the direct TCP fast path.
    while next_header in {0, 43, 44, 51, 60}:
        if next_header == 44:
            if cursor + 8 > end:
                return None
            fragment = _read_u16(data, cursor + 2)
            if fragment is None or fragment & (0xFFF8 | 0x0001):
                return None
            next_header = data[cursor]
            cursor += 8
        elif next_header == 51:
            if cursor + 2 > end:
                return None
            header_size = (data[cursor + 1] + 2) * 4
            if cursor + header_size > end:
                return None
            next_header = data[cursor]
            cursor += header_size
        else:
            if cursor + 2 > end:
                return None
            header_size = (data[cursor + 1] + 1) * 8
            if cursor + header_size > end:
                return None
            next_header = data[cursor]
            cursor += header_size
    if next_header != _TCP:
        return None
    source = socket.inet_ntop(socket.AF_INET6, data[offset + 8 : offset + 24])
    destination = socket.inet_ntop(socket.AF_INET6, data[offset + 24 : offset + 40])
    return _parse_tcp(data[offset:end], cursor - offset, source, destination)


def parse_ethernet_frame(frame: bytes) -> TcpSegment | None:
    """Decode a TCP segment from Ethernet II, including common VLAN tags."""

    if len(frame) < 14:
        return None
    ethertype = _read_u16(frame, 12)
    if ethertype is None:
        return None
    offset = 14
    while ethertype in _VLAN_ETHERTYPES:
        if offset + 4 > len(frame):
            return None
        ethertype = _read_u16(frame, offset + 2)
        if ethertype is None:
            return None
        offset += 4
    if ethertype == ETH_P_IP:
        return _parse_ipv4(frame, offset)
    if ethertype == ETH_P_IPV6:
        return _parse_ipv6(frame, offset)
    return None


def _tls_version_name(value: int) -> str:
    return {
        0x0301: "TLS 1.0",
        0x0302: "TLS 1.1",
        0x0303: "TLS 1.2",
        0x0304: "TLS 1.3",
    }.get(value, f"0x{value:04x}")


def _parse_extensions(data: bytes) -> TlsMetadata | None:
    cursor = 0
    server_name = ""
    alpn_protocols: list[str] = []
    tls_versions: list[str] = []
    while cursor < len(data):
        extension_type = _read_u16(data, cursor)
        extension_size = _read_u16(data, cursor + 2)
        if extension_type is None or extension_size is None:
            return None
        cursor += 4
        end = cursor + extension_size
        if end > len(data):
            return None
        value = data[cursor:end]
        cursor = end
        if extension_type == 0 and len(value) >= 5:
            list_size = _read_u16(value, 0)
            name_size = _read_u16(value, 3)
            if (
                list_size is not None
                and name_size is not None
                and value[2] == 0
                and list_size >= 3 + name_size
                and list_size + 2 <= len(value)
                and 5 + name_size <= len(value)
            ):
                encoded_name = value[5 : 5 + name_size]
                try:
                    server_name = encoded_name.decode("idna")
                except UnicodeError:
                    server_name = encoded_name.decode("ascii", "replace")
        elif extension_type == 16 and len(value) >= 2:
            list_size = _read_u16(value, 0)
            if list_size is None or list_size + 2 > len(value):
                continue
            item_cursor = 2
            item_end = 2 + list_size
            while item_cursor < item_end:
                size = value[item_cursor]
                item_cursor += 1
                if item_cursor + size > item_end:
                    break
                alpn_protocols.append(
                    value[item_cursor : item_cursor + size].decode("ascii", "replace")
                )
                item_cursor += size
        elif extension_type == 43 and value:
            list_size = value[0]
            if list_size + 1 > len(value) or list_size % 2:
                continue
            tls_versions.extend(
                _tls_version_name(struct.unpack_from("!H", value, item)[0])
                for item in range(1, 1 + list_size, 2)
            )
    return TlsMetadata(server_name, tuple(alpn_protocols), tuple(tls_versions))


def _client_hello_handshake(data: bytes | bytearray) -> tuple[bytes, int] | None:
    """Return ClientHello handshake bytes and consumed TLS stream length.

    TLS permits a handshake message to span records.  This parser keeps the
    record boundaries only long enough to concatenate handshake fragments.
    """

    cursor = 0
    handshake = bytearray()
    required_size: int | None = None
    while True:
        if cursor + 5 > len(data) or data[cursor] != _TLS_HANDSHAKE:
            return None
        record_size = _read_u16(data, cursor + 3)
        if record_size is None:
            return None
        record_end = cursor + 5 + record_size
        if record_end > len(data):
            return None
        handshake.extend(data[cursor + 5 : record_end])
        if required_size is None and len(handshake) >= 4:
            if handshake[0] != _CLIENT_HELLO:
                return None
            required_size = 4 + int.from_bytes(handshake[1:4], "big")
            if required_size > _MAX_REASSEMBLY_BYTES:
                return None
        if required_size is not None and len(handshake) >= required_size:
            return bytes(handshake[:required_size]), record_end
        cursor = record_end


def parse_tls_client_hello(data: bytes) -> TlsMetadata | None:
    """Extract metadata from one complete TLS ClientHello handshake.

    ``None`` means the data is not a complete, valid ClientHello record.  The
    reassembler keeps TCP fragments until the record's advertised length is
    available before invoking this parser.
    """

    parsed = _client_hello_handshake(data)
    if parsed is None:
        return None
    handshake, _ = parsed
    hello_size = int.from_bytes(handshake[1:4], "big")
    if hello_size < 34:
        return None
    hello = handshake[4 : 4 + hello_size]
    cursor = 34
    if cursor >= len(hello):
        return None
    session_size = hello[cursor]
    cursor += 1 + session_size
    cipher_size = _read_u16(hello, cursor)
    if cipher_size is None:
        return None
    cursor += 2 + cipher_size
    if cursor >= len(hello):
        return None
    compression_size = hello[cursor]
    cursor += 1 + compression_size
    extension_size = _read_u16(hello, cursor)
    if extension_size is None:
        return None
    cursor += 2
    if cursor + extension_size > len(hello):
        return None
    metadata = _parse_extensions(hello[cursor : cursor + extension_size])
    if metadata is None:
        return None
    return (
        metadata
        if metadata.tls_versions
        else TlsMetadata(
            metadata.server_name,
            metadata.alpn_protocols,
            (_tls_version_name(struct.unpack_from("!H", hello, 0)[0]),),
        )
    )


def _client_hello_stream_size(data: bytearray) -> int | None:
    """Return bytes required by a complete ClientHello, or ``-1`` if invalid."""

    if not data or data[0] != _TLS_HANDSHAKE:
        return -1
    parsed = _client_hello_handshake(data)
    if parsed is not None:
        return parsed[1]
    if len(data) < 5:
        return None
    # The first record can be complete while the handshake continues in the
    # next record. Retain it unless the first record is definitively invalid.
    record_size = _read_u16(data, 3)
    if record_size is None:
        return None
    if len(data) < 5 + record_size:
        return None
    first_record = data[5 : 5 + record_size]
    if len(first_record) >= 4 and first_record[0] != _CLIENT_HELLO:
        return -1
    return None


class TlsHelloReassembler:
    """Bounded in-order TCP reassembly just for one ClientHello per flow."""

    def __init__(
        self,
        *,
        max_bytes: int = _MAX_REASSEMBLY_BYTES,
        flow_ttl_seconds: float = _FLOW_TTL_SECONDS,
    ) -> None:
        self.max_bytes = max_bytes
        self.flow_ttl_seconds = flow_ttl_seconds
        self._flows: dict[FlowKey, tuple[int, bytearray, float]] = {}

    def feed(self, segment: TcpSegment, now: float | None = None) -> TlsMetadata | None:
        """Add a segment and return metadata once its ClientHello is complete."""

        current_time = time.monotonic() if now is None else now
        self._prune(current_time)
        if not segment.payload:
            return None
        existing = self._flows.get(segment.flow)
        if existing is None:
            if segment.payload[0] != _TLS_HANDSHAKE:
                return None
            data = bytearray(segment.payload)
            expected = (segment.sequence + len(segment.payload)) & 0xFFFFFFFF
        else:
            expected, data, _ = existing
            difference = (segment.sequence - expected) & 0xFFFFFFFF
            if difference == 0:
                data.extend(segment.payload)
                expected = (expected + len(segment.payload)) & 0xFFFFFFFF
            elif difference > 0x80000000:
                overlap = (expected - segment.sequence) & 0xFFFFFFFF
                if overlap < len(segment.payload):
                    data.extend(segment.payload[overlap:])
                    expected = (expected + len(segment.payload) - overlap) & 0xFFFFFFFF
            else:
                if segment.payload[0] != _TLS_HANDSHAKE:
                    self._flows.pop(segment.flow, None)
                    return None
                data = bytearray(segment.payload)
                expected = (segment.sequence + len(segment.payload)) & 0xFFFFFFFF
        if len(data) > self.max_bytes:
            self._flows.pop(segment.flow, None)
            return None
        expected_stream_size = _client_hello_stream_size(data)
        if expected_stream_size == -1:
            self._flows.pop(segment.flow, None)
            return None
        if expected_stream_size is None or len(data) < expected_stream_size:
            self._flows[segment.flow] = (expected, data, current_time)
            return None
        self._flows.pop(segment.flow, None)
        return parse_tls_client_hello(bytes(data[:expected_stream_size]))

    def _prune(self, now: float) -> None:
        self._flows = {
            flow: state
            for flow, state in self._flows.items()
            if now - state[2] <= self.flow_ttl_seconds
        }

    def retain_flows(self, allowed: set[FlowKey]) -> None:
        """Discard partial handshakes outside the current capture boundary."""

        self._flows = {flow: state for flow, state in self._flows.items() if flow in allowed}


def _socket_flow(socket_info: SocketInfo) -> FlowKey | None:
    local_host, local_port = parse_endpoint(socket_info.local)
    peer_host, peer_port = parse_endpoint(socket_info.peer)
    if local_port is None or peer_port is None:
        return None
    return FlowKey(
        _normalise_host(local_host),
        local_port,
        _normalise_host(peer_host),
        peer_port,
    )


class PacketInspector:
    """Background Linux AF_PACKET collector with explicit, bounded retention."""

    def __init__(
        self,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        allowlist_ttl_seconds: float = _ALLOWLIST_TTL_SECONDS,
    ) -> None:
        self._socket_factory = socket_factory
        self._clock = clock
        self._monotonic = monotonic
        self._packet_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reassembler = TlsHelloReassembler()
        self._observations: OrderedDict[FlowKey, PacketObservation] = OrderedDict()
        self._allowlist: dict[FlowKey, ProcessIdentity] = {}
        self._allowlist_updated_at: float | None = None
        self._allowlist_ttl_seconds = allowlist_ttl_seconds
        self.error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start capture, returning ``False`` with ``error`` on local failure."""

        if self.running:
            return True
        try:
            packet_socket = self._socket_factory(
                socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
            )
            packet_socket.settimeout(0.5)
        except (AttributeError, OSError) as exc:
            self.error = f"AF_PACKET 原始套接字不可用：{exc}"
            return False
        self.error = ""
        self._stop.clear()
        self._packet_socket = packet_socket
        self._thread = threading.Thread(target=self._run, name="codexdeck-packets", daemon=True)
        self._thread.start()
        return True

    def observe_frame(self, frame: bytes, now: float | None = None) -> PacketObservation | None:
        """Decode one frame. Public for deterministic module and integration tests."""

        segment = parse_ethernet_frame(frame)
        if segment is None:
            return None
        monotonic_now = self._monotonic()
        with self._lock:
            self._expire_allowlist_locked(monotonic_now)
            if segment.flow not in self._allowlist:
                return None
            metadata = self._reassembler.feed(segment, monotonic_now)
            if metadata is None:
                return None
            observed_at = self._clock() if now is None else now
            observation = PacketObservation(segment.flow, metadata, observed_at)
            self._observations[segment.flow] = observation
            self._observations.move_to_end(segment.flow)
            self._prune_observations_locked(observed_at)
        return observation

    def update_allowlist(
        self,
        sockets_by_pid: dict[int, list[SocketInfo]],
        process_identities: dict[int, ProcessIdentity],
        *,
        now: float | None = None,
    ) -> None:
        """Replace the current Codex flow boundary from one coherent snapshot."""

        allowed: dict[FlowKey, ProcessIdentity] = {}
        for pid, socket_list in sockets_by_pid.items():
            identity = process_identities.get(pid)
            if identity is None:
                continue
            for socket_info in socket_list:
                flow = _socket_flow(socket_info)
                if flow is not None:
                    allowed[flow] = identity

        updated_at = self._monotonic() if now is None else now
        with self._lock:
            unchanged = {
                flow for flow, identity in allowed.items() if self._allowlist.get(flow) == identity
            }
            self._allowlist = allowed
            self._allowlist_updated_at = updated_at
            self._reassembler.retain_flows(unchanged)
            self._observations = OrderedDict(
                (flow, observation)
                for flow, observation in self._observations.items()
                if flow in unchanged
            )

    def invalidate_allowlist(self) -> None:
        """Fail closed when process or socket ownership is no longer current."""

        with self._lock:
            self._clear_allowlist_locked()

    def annotate(self, sockets_by_pid: dict[int, list[SocketInfo]]) -> None:
        """Attach retained TLS metadata to the matching current TCP sockets."""

        now = self._clock()
        with self._lock:
            self._expire_allowlist_locked(self._monotonic())
            self._prune_observations_locked(now)
            observations = dict(self._observations)
        for socket_list in sockets_by_pid.values():
            for socket_info in socket_list:
                flow = _socket_flow(socket_info)
                observation = observations.get(flow) if flow else None
                if observation is None:
                    continue
                socket_info.tls_server_name = observation.metadata.server_name
                socket_info.tls_alpn_protocols = observation.metadata.alpn_protocols
                socket_info.tls_versions = observation.metadata.tls_versions
                socket_info.tls_observed_at = observation.observed_at

    def close(self) -> None:
        self._stop.set()
        packet_socket = self._packet_socket
        self._packet_socket = None
        if packet_socket is not None:
            try:
                packet_socket.close()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            packet_socket = self._packet_socket
            if packet_socket is None:
                return
            try:
                frame = packet_socket.recv(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = f"AF_PACKET 采集已停止：{exc}"
                return
            self.observe_frame(frame)

    def _prune_observations_locked(self, now: float) -> None:
        expired = [
            flow
            for flow, observation in self._observations.items()
            if now - observation.observed_at > _OBSERVATION_TTL_SECONDS
        ]
        for flow in expired:
            self._observations.pop(flow, None)
        while len(self._observations) > _MAX_OBSERVATIONS:
            self._observations.popitem(last=False)

    def _expire_allowlist_locked(self, now: float) -> None:
        updated_at = self._allowlist_updated_at
        if updated_at is None or now - updated_at > self._allowlist_ttl_seconds:
            self._clear_allowlist_locked()

    def _clear_allowlist_locked(self) -> None:
        self._allowlist.clear()
        self._allowlist_updated_at = None
        self._reassembler.retain_flows(set())
        self._observations.clear()

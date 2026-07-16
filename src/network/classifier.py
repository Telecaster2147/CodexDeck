"""Turn TCP counters into supporting evidence without overriding protocol state."""

from __future__ import annotations

from models import ConnectionAssessment, NetworkEvidence, NetworkState, SocketInfo


def idle_seconds(socket: SocketInfo) -> float | None:
    values = [value for value in (socket.lastsnd_ms, socket.lastrcv_ms) if value is not None]
    return min(values) / 1000.0 if values else None


def assess_connection(
    before: SocketInfo | None,
    after: SocketInfo,
    idle_threshold: float,
) -> ConnectionAssessment:
    sent_delta = max(0, after.bytes_sent - (before.bytes_sent if before else after.bytes_sent))
    previous_received = before.bytes_received if before else after.bytes_received
    received_delta = max(0, after.bytes_received - previous_received)
    acked_delta = max(0, after.bytes_acked - (before.bytes_acked if before else after.bytes_acked))
    previous_retrans = before.retrans_total if before else after.retrans_total
    retrans_delta = max(0, after.retrans_total - previous_retrans)
    progress = sent_delta + received_delta + acked_delta
    idle = idle_seconds(after)
    state = after.state.upper()

    if progress > 0:
        health = NetworkState.ACTIVE
        reason = f"发送 +{sent_delta} B、接收 +{received_delta} B、ACK +{acked_delta} B"
    elif state in {"SYN-SENT", "SYN-RECV"}:
        health = NetworkState.SUSPECT
        reason = "TCP 握手尚未完成且采样窗口无进展"
    elif after.send_q > 0:
        health = NetworkState.SUSPECT
        reason = f"发送队列积压 {after.send_q} 字节且没有 ACK 进展"
    elif retrans_delta > 0:
        health = NetworkState.SUSPECT
        reason = f"重传增加 {retrans_delta} 次且没有有效进展"
    elif state not in {"ESTAB", "ESTABLISHED"}:
        health = NetworkState.CLOSED
        reason = f"连接状态为 {state}"
    elif idle is not None and idle >= idle_threshold:
        health = NetworkState.IDLE
        reason = f"TCP 队列为空，最近业务流量约在 {idle:.1f} 秒前"
    else:
        health = NetworkState.IDLE
        reason = "TCP 已建立且队列为空"
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
        tls_server_name=after.tls_server_name,
        tls_alpn_protocols=after.tls_alpn_protocols,
        tls_versions=after.tls_versions,
        tls_observed_at=after.tls_observed_at,
    )


def assess_process_network(
    before: list[SocketInfo],
    after: list[SocketInfo],
    idle_threshold: float,
) -> NetworkEvidence:
    before_by_key = {socket.key: socket for socket in before}
    routed_after = [socket for socket in after if socket.route in {"external", "proxy", "lan"}]
    routed_before = [socket for socket in before if socket.route in {"external", "proxy", "lan"}]
    connections = [
        assess_connection(before_by_key.get(socket.key), socket, idle_threshold)
        for socket in routed_after
    ]
    active = [item for item in connections if item.health == NetworkState.ACTIVE]
    suspect = [item for item in connections if item.health == NetworkState.SUSPECT]
    established = [item for item in connections if item.health == NetworkState.IDLE]
    if active:
        state = NetworkState.ACTIVE
        reason = "; ".join(item.reason for item in active)
    elif suspect:
        state = NetworkState.SUSPECT
        reason = "; ".join(item.reason for item in suspect)
    elif established:
        state = NetworkState.IDLE
        reason = "; ".join(item.reason for item in established)
    elif routed_before and not routed_after:
        state = NetworkState.CLOSED
        reason = "采样期间连接正常关闭"
    elif connections:
        state = NetworkState.CLOSED
        reason = "; ".join(item.reason for item in connections)
    else:
        state = NetworkState.UNKNOWN
        reason = "未发现归属于该进程的活动外联"
    return NetworkEvidence(state=state, reason=reason, connections=connections)

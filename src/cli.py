"""Command-line parsing and top-level exception normalization."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable

from app import AppOptions, run_application
from codex.hook_events import receive_hook_event
from config import DEFAULT_EVENT_LOOKBACK, DEFAULT_IDLE_THRESHOLD, DEFAULT_INTERVAL, VERSION


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是数字") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def pid_value(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("PID 必须是整数") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("PID 必须大于 0")
    return pid


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexnet",
        description="按 Codex 实例观察会话生命周期、重连恢复与 TCP 证据。",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("doctor", "export", "metrics", "hook-event"),
        help="doctor：诊断；export：导出；metrics：指标；hook-event：接收 compact hook",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_INTERVAL,
        help=f"刷新间隔秒数，默认 {DEFAULT_INTERVAL:g}",
    )
    parser.add_argument(
        "--idle-threshold",
        type=positive_float,
        default=DEFAULT_IDLE_THRESHOLD,
        help=f"连接空闲显示阈值秒数，默认 {DEFAULT_IDLE_THRESHOLD:g}",
    )
    parser.add_argument(
        "--event-lookback",
        type=positive_float,
        default=float(DEFAULT_EVENT_LOOKBACK),
        help=f"时间线可见窗口秒数，默认 {DEFAULT_EVENT_LOOKBACK:g}",
    )
    parser.add_argument("--pid", type=pid_value, action="append", default=[], help="只观察指定 PID，可重复")
    parser.add_argument(
        "--codex-home",
        type=Path,
        action="append",
        default=[],
        help="只观察指定 CODEX_HOME，可重复",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="持续观察（默认）")
    mode.add_argument("--once", action="store_true", help="完成一个采样窗口后退出")
    parser.add_argument("--flat", action="store_true", help="启动时使用扁平会话视图")
    parser.add_argument("--all", action="store_true", help="显示启动器、app-server 和辅助进程")
    parser.add_argument(
        "--packet-inspection",
        action="store_true",
        help="被动解析 TLS ClientHello 元数据（需要 CAP_NET_RAW 或 root）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON，持续监控模式使用 NDJSON")
    parser.add_argument("--session", help="export：按会话 ID 或完整会话 key 导出复盘")
    parser.add_argument(
        "--current-incidents",
        action="store_true",
        help="export：导出当前未解决事件清单",
    )
    parser.add_argument(
        "--format",
        choices=("json",),
        default="json",
        help="export 输出格式，当前仅支持 json",
    )
    parser.add_argument(
        "--history",
        type=Path,
        help="把关键事件和聚合指标写入独立 SQLite 历史库",
    )
    parser.add_argument(
        "--hook-events",
        type=Path,
        help="读取 compact hook NDJSON；hook-event 命令将最小事件写入此路径",
    )
    parser.add_argument(
        "--history-days",
        type=positive_int,
        default=30,
        help="历史保留天数，默认 30",
    )
    parser.add_argument(
        "--history-max-mib",
        type=positive_float,
        default=128.0,
        help="历史库空间上限 MiB，默认 128",
    )
    parser.add_argument("--no-color", action="store_true", help="关闭终端颜色")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def required_commands_available() -> None:
    missing = [command for command in ("ps", "ss") if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"缺少系统命令：{', '.join(missing)}")


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "hook-event":
        if args.hook_events is None:
            raise RuntimeError("hook-event 必须指定 --hook-events PATH")
        receive_hook_event(args.hook_events, sys.stdin)
        return 0
    if args.command == "export" and bool(args.session) == bool(args.current_incidents):
        raise RuntimeError("export 必须且只能指定 --session 或 --current-incidents")
    if args.command != "export" and (args.session or args.current_incidents):
        raise RuntimeError("--session 和 --current-incidents 仅用于 export")
    required_commands_available()
    options = AppOptions(
        interval=args.interval,
        idle_threshold=args.idle_threshold,
        event_lookback=int(args.event_lookback),
        selected_pids=set(args.pid) or None,
        selected_homes=set(args.codex_home) or None,
        once=args.once,
        json=args.json,
        no_color=args.no_color,
        show_auxiliary=args.all,
        flat=args.flat,
        packet_inspection=args.packet_inspection,
        doctor=args.command == "doctor",
        command=args.command or "monitor",
        export_session=args.session,
        current_incidents=args.current_incidents,
        history_path=args.history,
        history_days=args.history_days,
        history_max_bytes=int(args.history_max_mib * 1024 * 1024),
        hook_events_path=args.hook_events,
    )
    return run_application(options)


def run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\n检测已停止。", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

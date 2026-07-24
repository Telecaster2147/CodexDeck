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


def _default(value: object, *, suppress: bool) -> object:
    return argparse.SUPPRESS if suppress else value


def _add_source_arguments(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool,
) -> None:
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=_default(DEFAULT_INTERVAL, suppress=suppress_defaults),
        help=f"刷新间隔秒数，默认 {DEFAULT_INTERVAL:g}",
    )
    parser.add_argument(
        "--idle-threshold",
        type=positive_float,
        default=_default(DEFAULT_IDLE_THRESHOLD, suppress=suppress_defaults),
        help=f"连接空闲显示阈值秒数，默认 {DEFAULT_IDLE_THRESHOLD:g}",
    )
    parser.add_argument(
        "--event-lookback",
        type=positive_float,
        default=_default(float(DEFAULT_EVENT_LOOKBACK), suppress=suppress_defaults),
        help=f"时间线可见窗口秒数，默认 {DEFAULT_EVENT_LOOKBACK:g}",
    )
    parser.add_argument(
        "--pid",
        type=pid_value,
        action="append",
        default=_default([], suppress=suppress_defaults),
        help="只观察指定 PID，可重复",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        action="append",
        default=_default([], suppress=suppress_defaults),
        help="只观察指定 CODEX_HOME，可重复",
    )
    parser.add_argument(
        "--packet-inspection",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="被动解析 TLS ClientHello 元数据（需要 CAP_NET_RAW 或 root）",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=_default(None, suppress=suppress_defaults),
        help="把关键事件和聚合指标写入独立 SQLite 历史库",
    )
    parser.add_argument(
        "--hook-events",
        type=Path,
        default=_default(None, suppress=suppress_defaults),
        help="读取 compact hook NDJSON；hook-event 命令将最小事件写入此路径",
    )
    parser.add_argument(
        "--history-days",
        type=positive_int,
        default=_default(7, suppress=suppress_defaults),
        help="历史保留天数，默认 7",
    )
    parser.add_argument(
        "--history-max-mib",
        type=positive_float,
        default=_default(128.0, suppress=suppress_defaults),
        help="历史库空间上限 MiB，默认 128",
    )


def _add_monitor_arguments(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool,
) -> None:
    parser.add_argument(
        "--once",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="完成一个采样窗口后退出",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="启动时使用扁平会话视图",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="显示启动器、app-server 和辅助进程",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="输出 JSON，持续监控模式使用 NDJSON",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="关闭终端颜色",
    )
    parser.add_argument(
        "--strict-observation",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="observer blind/stale/unknown/budget/conflict 时使用独立退出码 5",
    )


def _add_export_arguments(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool,
) -> None:
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument(
        "--session",
        default=_default(None, suppress=suppress_defaults),
        metavar="SESSION_ID",
        help="按会话 ID 或完整会话 key 导出复盘",
    )
    selectors.add_argument(
        "--current-incidents",
        action="store_true",
        default=_default(False, suppress=suppress_defaults),
        help="导出当前未解决事件清单",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexdeck",
        description="按 Codex 实例观察会话生命周期、重连恢复与 TCP 证据。",
        epilog="不指定子命令时启动监控；交互终端进入 TUI，管道环境输出文本。",
    )
    _add_source_arguments(parser, suppress_defaults=False)
    _add_monitor_arguments(parser, suppress_defaults=False)
    _add_export_arguments(parser, suppress_defaults=False)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = subparsers.add_parser(
        "doctor",
        help="立即检查 discovery、数据源与采集能力",
        description=(
            "立即执行一次只读完整采样并报告 discovery、路径、SQLite、rollout、socket "
            "和可选采集器状态；不等待普通监控的基线窗口。"
        ),
        epilog="默认输出人类可读诊断；--json 输出 doctor_schema_version 1。退出码反映采集健康度。",
    )
    _add_source_arguments(doctor, suppress_defaults=True)
    doctor.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="输出 versioned doctor JSON",
    )

    export = subparsers.add_parser(
        "export",
        help="导出当前事件或单个会话复盘",
        description=(
            "立即执行一次只读完整采样并输出 JSON。必须且只能指定 --session SESSION_ID "
            "或 --current-incidents。"
        ),
        epilog=(
            "会话导出包含保留的 normalized events、工具/turn 摘要和网络证据；"
            "terminal transcript 正文不会进入导出。"
        ),
    )
    _add_source_arguments(export, suppress_defaults=True)
    _add_export_arguments(export, suppress_defaults=True)

    metrics = subparsers.add_parser(
        "metrics",
        help="输出一次 Prometheus text format 快照",
        description=(
            "立即执行一次只读完整采样并输出 Prometheus text format；该命令不会启动 HTTP server。"
        ),
        epilog="指标只使用低基数标签，不包含 session ID、PID、errmsg 或网络 endpoint。",
    )
    _add_source_arguments(metrics, suppress_defaults=True)

    hook_event = subparsers.add_parser(
        "hook-event",
        help="从 stdin 接收最小 compact hook 事件",
        description=(
            "读取 stdin 中的 PreCompact/PostCompact payload，筛选白名单字段后追加到指定 NDJSON。"
        ),
        epilog="必须指定 --hook-events PATH；目标文件以 0600 权限创建。",
    )
    hook_event.add_argument(
        "--hook-events",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="写入最小 compact hook NDJSON 的路径（必需）",
    )

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
        command=args.command or "monitor",
        export_session=args.session,
        current_incidents=args.current_incidents,
        history_path=args.history,
        history_days=args.history_days,
        history_max_bytes=int(args.history_max_mib * 1024 * 1024),
        hook_events_path=args.hook_events,
        strict_observation=args.strict_observation,
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

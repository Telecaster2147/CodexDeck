"""Command-line parsing and top-level exception normalization."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable

from app import AppOptions, run_application
from config import DEFAULT_EVENT_LOOKBACK, DEFAULT_IDLE_THRESHOLD, VERSION


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


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pid", type=pid_value, action="append", default=[], help="只观察指定 PID，可重复")
    parser.add_argument(
        "--codex-home",
        type=Path,
        action="append",
        default=[],
        help="只观察指定 CODEX_HOME，可重复",
    )


def _add_advanced_monitor_options(parser: argparse.ArgumentParser) -> None:
    advanced = parser.add_argument_group("advanced")
    advanced.add_argument(
        "--idle-threshold",
        type=positive_float,
        default=DEFAULT_IDLE_THRESHOLD,
        help=f"连接空闲显示阈值秒数，默认 {DEFAULT_IDLE_THRESHOLD:g}",
    )
    advanced.add_argument(
        "--event-lookback",
        type=positive_float,
        default=float(DEFAULT_EVENT_LOOKBACK),
        help=f"时间线可见窗口秒数，默认 {DEFAULT_EVENT_LOOKBACK:g}",
    )


def _add_monitor_arguments(parser: argparse.ArgumentParser) -> None:
    _add_filters(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="采样一次后退出")
    mode.add_argument("--watch", action="store_true", help="持续输出 NDJSON")
    parser.add_argument(
        "--format",
        choices=("text", "json", "ndjson"),
        default="text",
        dest="output_format",
        help="非交互输出格式，默认 text",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_alias",
        help="兼容别名，等同 --format json",
    )
    parser.add_argument("--flat", action="store_true", help="TUI 启动时使用扁平会话视图")
    parser.add_argument("--all", action="store_true", help="显示已结束会话与辅助进程")
    parser.add_argument("--no-color", action="store_true", help="关闭 TUI 与文本颜色")
    parser.add_argument(
        "--strict-observation",
        action="store_true",
        help="one-shot observer 降级时使用退出码 5",
    )
    _add_advanced_monitor_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexdeck",
        description="只读观察当前用户的 Codex 会话。",
        epilog="直接运行进入 TUI；脚本请使用 monitor --once --format json。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--json", action="store_true", dest="legacy_json_alias", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    monitor = subparsers.add_parser(
        "monitor",
        help="交互观察或输出一次当前快照",
        description="TTY 默认进入 TUI；非 TTY 默认 one-shot。持续机器输出仅支持 --watch --format ndjson。",
    )
    _add_monitor_arguments(monitor)

    doctor = subparsers.add_parser(
        "doctor",
        help="立即检查 discovery、数据源与采集能力",
        description="立即执行一次只读完整采样；不等待普通监控的基线窗口。",
        epilog="text 为默认格式；json 输出 doctor_schema_version 2。",
    )
    _add_filters(doctor)
    doctor.add_argument("--format", choices=("text", "json"), default="text", dest="output_format")
    doctor.add_argument("--json", action="store_true", dest="json_alias", help="等同 --format json")

    export = subparsers.add_parser(
        "export",
        help="导出单会话的有界当前报告",
        description="立即采样并输出 versioned JSON；必须指定 --session SESSION_ID。",
        epilog="包含 rollout lookback 与最多 500 条 retained events；terminal transcript 正文不会进入导出。",
    )
    _add_filters(export)
    export.add_argument("--session", required=True, metavar="SESSION_ID", help="会话 ID 或完整会话 key")
    return parser


def required_commands_available() -> None:
    if shutil.which("ps") is None:
        raise RuntimeError("缺少系统命令：ps")


def _normalize_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    command = args.command or "monitor"
    if args.command is None:
        # Bare invocation intentionally means the quiet interactive monitor.
        legacy_json_alias = args.legacy_json_alias
        defaults = build_parser().parse_args(["monitor"])
        defaults.command = "monitor"
        defaults.json_alias = legacy_json_alias
        args = defaults
    if command in {"monitor", "doctor"} and args.json_alias:
        if args.output_format not in {"text", "json"}:
            parser.error("--json 与非 JSON 的 --format 不能同时使用")
        args.output_format = "json"
    if command == "monitor":
        if args.watch and args.output_format != "ndjson":
            parser.error("--watch 只与 --format ndjson 一起使用")
        if args.output_format == "ndjson" and not args.watch:
            parser.error("--format ndjson 必须同时指定 --watch")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = _normalize_args(parser, parser.parse_args(list(argv) if argv is not None else None))
    required_commands_available()
    command = args.command or "monitor"
    options = AppOptions(
        idle_threshold=getattr(args, "idle_threshold", DEFAULT_IDLE_THRESHOLD),
        event_lookback=int(getattr(args, "event_lookback", DEFAULT_EVENT_LOOKBACK)),
        selected_pids=set(args.pid) or None,
        selected_homes=set(args.codex_home) or None,
        once=getattr(args, "once", False),
        watch=getattr(args, "watch", False),
        output_format=getattr(args, "output_format", "json" if command == "export" else "text"),
        no_color=getattr(args, "no_color", False),
        show_auxiliary=getattr(args, "all", False),
        flat=getattr(args, "flat", False),
        command=command,
        export_session=getattr(args, "session", None),
        strict_observation=getattr(args, "strict_observation", False),
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

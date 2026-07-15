"""Command-line parsing and top-level application orchestration."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from .activity import ActivityTracker, SseHealthTracker
from .config import (
    DEFAULT_EVENT_LOOKBACK,
    DEFAULT_IDLE_THRESHOLD,
    DEFAULT_INTERVAL,
    DEFAULT_SSE_LOOKBACK,
    STATE_NETWORK_STALL,
    VERSION,
)
from .models import ConversationActivity, ProcessAssessment, SseHealth
from .monitoring import collect_assessments
from .ui import (
    assessment_to_dict,
    compact_banner,
    render_text,
    run_interactive,
    visible_cli_assessments,
)
from .utils import format_duration


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-net-health",
        description="自动发现 Codex 进程并检测对外连接、流量进展和网络卡死迹象。",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_INTERVAL,
        help=f"两次采样之间等待秒数，默认 {DEFAULT_INTERVAL:g}",
    )
    parser.add_argument(
        "--idle-threshold",
        type=positive_float,
        default=DEFAULT_IDLE_THRESHOLD,
        help=f"超过多少秒无业务流量时标记等待上游，默认 {DEFAULT_IDLE_THRESHOLD:g}",
    )
    parser.add_argument(
        "--sse-lookback",
        type=positive_float,
        default=float(DEFAULT_SSE_LOOKBACK),
        help=(
            "检查最近多少秒内的远程压缩 SSE 超时，"
            f"默认 {DEFAULT_SSE_LOOKBACK:g}"
        ),
    )
    parser.add_argument(
        "--event-lookback",
        type=positive_float,
        default=float(DEFAULT_EVENT_LOOKBACK),
        help=f"关键事件日志窗口秒数，默认 {DEFAULT_EVENT_LOOKBACK:g}",
    )
    parser.add_argument(
        "--pid",
        type=pid_value,
        action="append",
        default=[],
        help="只检测指定 PID；可以重复使用",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--watch",
        action="store_true",
        help="持续检测（默认行为，保留此参数用于兼容）",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="只采样并显示一次",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="同时显示启动器和辅助进程；VS Code app-server 始终隐藏",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def required_commands_available() -> None:
    missing = [command for command in ("ps", "ss") if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"缺少系统命令：{', '.join(missing)}")


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    required_commands_available()
    selected_pids = set(args.pid) or None
    use_color = sys.stdout.isatty() and not args.no_color and not args.json
    sse_tracker = SseHealthTracker(args.sse_lookback)

    watch_mode = not args.once
    panel_mode = (
        watch_mode and sys.stdin.isatty() and sys.stdout.isatty() and not args.json
    )
    assessments: list[ProcessAssessment] = []
    sse_health = SseHealth(True, int(args.sse_lookback))
    activities: dict[str, ConversationActivity] = {}
    if panel_mode:
        assessments, sse_health, activities = run_interactive(
            args, use_color, selected_pids, sse_tracker
        )
    else:
        activity_tracker = ActivityTracker(args.event_lookback)
        while True:
            assessments, sse_health = collect_assessments(
                interval=args.interval,
                idle_threshold=args.idle_threshold,
                selected_pids=selected_pids,
                sse_tracker=sse_tracker,
            )
            visible = visible_cli_assessments(assessments, args.all)
            activities = activity_tracker.update(
                [item.process for item in visible if item.process.role == "session"]
            )
            if args.json:
                payload = {
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "interval_seconds": args.interval,
                    "process_count": len(visible),
                    "processes": [assessment_to_dict(item) for item in visible],
                    "activities": {
                        session_id: asdict(activity)
                        for session_id, activity in activities.items()
                    },
                    "sse_health": asdict(sse_health),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
            else:
                render_text(visible, sse_health, args.interval, use_color, args.all)
                for item in visible:
                    activity = activities.get(item.process.session_id)
                    if not activity:
                        continue
                    if activity.alert:
                        print(
                            f"[{activity.alert}] PID {item.process.pid} "
                            f"{format_duration(activity.alert_age_seconds)}：{activity.alert_reason}"
                        )
                    elif activity.compacting:
                        print(compact_banner(item.process, activity))
                    else:
                        print(f"[上游] PID {item.process.pid}：{activity.phase}")
            if not watch_mode:
                break
            if not assessments:
                time.sleep(args.interval)
    if sse_health.has_sse_timeout:
        return 3
    if any(activity.alert for activity in activities.values()):
        return 4
    return 2 if any(item.health == STATE_NETWORK_STALL for item in assessments) else 0


def run() -> int:
    """Run the command-line application and normalize top-level failures."""
    try:
        return main()
    except KeyboardInterrupt:
        print("\n检测已停止。", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

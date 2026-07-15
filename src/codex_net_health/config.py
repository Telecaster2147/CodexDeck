"""Application paths, thresholds, states, labels, and terminal constants."""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__


VERSION = __version__
DEFAULT_INTERVAL = 2.0
DEFAULT_IDLE_THRESHOLD = 30.0
DEFAULT_SSE_LOOKBACK = 15 * 60
DEFAULT_EVENT_LOOKBACK = 15 * 60
ACTIVITY_BOOTSTRAP_LOOKBACK = 6 * 60 * 60
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
STATE_DB = CODEX_HOME / "state_5.sqlite"
LOG_DB = CODEX_HOME / "logs_2.sqlite"
SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
MAX_SESSION_TAIL = 4 * 1024 * 1024

STATE_ACTIVE = "ACTIVE"
STATE_HEALTHY_IDLE = "HEALTHY_IDLE"
STATE_UPSTREAM_WAIT = "UPSTREAM_WAIT"
STATE_NETWORK_STALL = "NETWORK_STALL"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_NO_OUTBOUND = "NO_OUTBOUND"
STATE_AUXILIARY = "AUXILIARY"

ALERT_PRE_REQUEST = "PRE_REQUEST_STALL"
ALERT_HTTP_RESPONSE = "HTTP_RESPONSE_STALL"
ALERT_POST_TOOL = "POST_TOOL_STALL"
ALERT_KEEPALIVE_ONLY = "KEEPALIVE_ONLY"

ALERT_THRESHOLDS = {
    ALERT_PRE_REQUEST: (60, 180),
    ALERT_HTTP_RESPONSE: (30, 90),
    ALERT_POST_TOOL: (90, 240),
    ALERT_KEEPALIVE_ONLY: (120, 300),
}

ACTIVITY_LABELS = {
    "TASK_START": "正在准备本地请求",
    "HTTP_POST": "请求已发送，等待响应",
    "RESPONSE_STARTED": "上游已接收请求",
    "REASONING": "模型正在推理",
    "MESSAGE": "模型正在生成回复",
    "TOOL_BUILD": "模型正在组织工具调用",
    "TOOL_CALL": "工具调用已生成",
    "TOOL_DONE": "工具已返回，等待模型继续",
    "KEEPALIVE": "仅收到上游 keepalive",
    "RESPONSE_DONE": "模型响应完成",
    "RESPONSE_FAIL": "模型响应失败",
    "TOKEN_USAGE": "上下文用量更新",
    "TASK_DONE": "当前 turn 已完成",
    "COMPACT_START": "正在压缩上下文",
    "COMPACT_DONE": "上下文压缩完成",
    "COMPACT_FAIL": "上下文压缩失败",
    "SSE_TIMEOUT": "SSE 空闲超时",
    "INTERRUPT": "当前 turn 已中断",
}

STATUS_TEXT = {
    STATE_ACTIVE: "活跃传输",
    STATE_HEALTHY_IDLE: "连接正常，暂时空闲",
    STATE_UPSTREAM_WAIT: "连接正常，正在等待上游",
    STATE_NETWORK_STALL: "疑似网络阻塞",
    STATE_DISCONNECTED: "采样期间连接断开",
    STATE_NO_OUTBOUND: "未发现活动外联",
    STATE_AUXILIARY: "辅助进程，无独立外联",
}

STATUS_PRIORITY = {
    STATE_NETWORK_STALL: 60,
    STATE_DISCONNECTED: 50,
    STATE_ACTIVE: 40,
    STATE_UPSTREAM_WAIT: 30,
    STATE_HEALTHY_IDLE: 20,
    STATE_NO_OUTBOUND: 10,
    STATE_AUXILIARY: 0,
}

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
}

ALT_SCREEN_ENTER = "\033[?1049h"
ALT_SCREEN_LEAVE = "\033[?1049l"
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"
SCREEN_HOME_CLEAR = "\033[H\033[2J"
ERASE_LINE = "\033[2K"
INVERSE = "\033[7m"


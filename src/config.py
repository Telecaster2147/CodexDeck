"""Application defaults and human-readable status labels."""

from __future__ import annotations

__version__ = "0.1.1"


VERSION = __version__
DEFAULT_INTERVAL = 2.0
TUI_EVENT_POLL_INTERVAL = 0.1
TUI_CLOCK_INTERVAL = 1.0
DEFAULT_IDLE_THRESHOLD = 30.0
DEFAULT_EVENT_LOOKBACK = 15 * 60
ACTIVITY_BOOTSTRAP_LOOKBACK = 6 * 60 * 60
MAX_SESSION_TAIL = 4 * 1024 * 1024
MAX_EVENTS_PER_SESSION = 500
COMMAND_TIMEOUT = 1.5
SQLITE_TIMEOUT = 0.10

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

EVENT_LABELS = {
    "TURN_STARTED": "Turn 已开始",
    "MODEL_CONFIG": "模型配置更新",
    "REQUEST_SENT": "请求已发送",
    "RESPONSE_STARTED": "上游已接收请求",
    "MODEL_PROGRESS": "模型正在生成",
    "REASONING_SUMMARY": "推理摘要",
    "PLAN_UPDATED": "执行计划已更新",
    "TOOL_RUNNING": "工具正在运行",
    "TOOL_COMPLETED": "工具已返回",
    "FILE_CHANGE_APPLIED": "文件变更已应用",
    "FILE_CHANGE_FAILED": "文件变更失败",
    "RECONNECTING": "响应流正在重连",
    "TRANSPORT_FALLBACK": "正在切换传输方式",
    "RECOVERED": "连接已恢复",
    "COMPACTING": "正在压缩上下文",
    "COMPACT_REQUESTED": "已请求压缩上下文",
    "COMPACT_PROGRESS": "上下文压缩仍有活动",
    "COMPACT_COMPLETED": "上下文压缩完成",
    "COMPACT_FAILED": "上下文压缩失败",
    "COMPACT_ABORTED": "上下文压缩中止",
    "TURN_COMPLETED": "Turn 已完成",
    "TURN_FAILED": "模型调用失败",
    "TURN_ABORTED": "Turn 已中断",
    "TOKEN_USAGE": "上下文用量更新",
    "KEEPALIVE": "收到 keepalive",
    "WARNING": "Codex 警告",
    "OPERATION_ERROR": "操作错误",
    "ACTION_REQUIRED": "等待用户操作",
    "ACTION_RESOLVED": "用户操作已处理",
    "USER_INPUT_RECEIVED": "用户已回复",
    "UNPARSED_PAYLOAD": "未识别协议数据",
    "PROCESS_EXITED": "进程已退出",
    "PROCESS_RESUMED": "进程已重新启动",
}

LIFECYCLE_LABELS = {
    "IDLE": "空闲",
    "STARTING": "正在准备请求",
    "WAITING_RESPONSE": "正在等待上游",
    "GENERATING": "模型正在生成",
    "RUNNING_TOOL": "工具正在运行",
    "COMPACTING": "正在压缩上下文",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "ABORTED": "已中断",
}

RECOVERY_LABELS = {
    "NONE": "",
    "SUSPECT": "疑似异常",
    "RECONNECTING": "正在重连",
    "TRANSPORT_FALLBACK": "切换传输",
    "RECOVERED": "已恢复",
}

NETWORK_LABELS = {
    "UNKNOWN": "网络信息未知",
    "IDLE": "连接空闲",
    "ACTIVE": "活跃传输",
    "SUSPECT": "疑似网络异常",
    "STALLED": "网络阻塞",
    "CLOSED": "连接已关闭",
}

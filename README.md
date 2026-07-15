# Codex Net Health

`codex-net-health` 是面向 Linux 终端的 Codex 会话与网络状态监视器。它以只读方式组合进程、TCP 和 Codex 本地事件数据，帮助区分正常传输、健康空闲、等待上游、连接断开与疑似网络阻塞。

## 功能

- 自动发现 Codex 会话、启动器和辅助进程
- 通过 `ss -tinp` 比较 TCP 队列、收发字节、ACK 与重传进展
- 读取 Codex rollout 与日志数据库，展示请求、推理、工具调用和 compact 阶段
- 检测请求前、HTTP 响应、工具返回后和仅 keepalive 四类停顿
- 提供交互式 TUI、单次文本检查和 JSON 输出
- 输出事件详情前遮盖常见令牌、密码和 Authorization 字段

## 环境要求

- Linux
- Python 3.10 或更高版本
- `ps` 与 `ss`（`ss` 通常由 `iproute2` 提供）
- 本机 Codex 数据目录，默认是 `~/.codex`；可通过 `CODEX_HOME` 覆盖

本项目仅使用 Python 标准库。

## 安装

推荐使用 `pipx` 从仓库安装：

```bash
pipx install .
```

开发环境可使用可编辑安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

安装后会提供 `codexnet` 和 `codex-net-health` 两个等价命令。直接从源码目录运行时，也可以使用根目录的兼容启动器或模块入口。

## 使用

```bash
codexnet
codexnet --once
codexnet --pid PID --interval 10
codexnet --event-lookback 900 --sse-lookback 900
codexnet --once --json
python3 -m codex_net_health --version
```

交互模式支持方向键或 `j`/`k` 选择、Enter 或 `l` 查看日志、`/` 搜索、`?` 查看帮助、`q` 退出。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 未检测到告警 |
| `1` | 运行环境或命令错误 |
| `2` | 检测到网络阻塞 |
| `3` | 检测到 SSE 空闲超时 |
| `4` | 检测到会话阶段停顿 |
| `130` | 用户中断 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 项目结构

```text
codexnet/
├── src/codex_net_health/
│   ├── __init__.py       # 包版本与公共元数据
│   ├── __main__.py       # python -m 入口
│   ├── cli.py            # 参数解析、主循环与退出码
│   ├── config.py         # 路径、阈值、状态和终端常量
│   ├── activity.py       # rollout、结构化日志与 SSE 事件追踪
│   ├── collectors.py     # 进程、会话、TCP 采集与网络判定
│   ├── monitoring.py     # 单次、持续与交互模式的采样编排
│   ├── ui.py             # 文本输出与交互式 TUI
│   ├── models.py         # 进程、连接、事件与评估数据模型
│   └── utils.py          # 跨层共享的小型工具函数
├── tests/                # 单元测试
├── codex-net-health      # 源码工作区兼容启动器
├── pyproject.toml        # 构建、安装与工具配置
└── README.md
```

模块依赖保持单向：`cli` 负责应用编排，`ui` 使用活动追踪和采集服务，底层模块共享 `models`、`config` 与 `utils`。采集和判定代码不依赖终端界面，因此后续可以单独测试或接入其他输出方式。

## 工作原理

监视器先用 `ps` 发现 Codex 进程，并读取 `/proc` 补充工作目录和打开的 rollout 文件。随后对 `ss -tinpH` 做间隔采样，以连接状态、队列积压、字节与 ACK 增量及重传变化评估 TCP 健康度。同时，它以只读 SQLite 连接查询 `$CODEX_HOME/logs_2.sqlite`，并增量解析会话 JSONL，从而将网络现象与 Codex 当前阶段关联起来。

运行时不会修改 Codex 数据。JSON 或终端输出仍可能包含会话标题、任务摘要、路径和网络端点，公开日志前应先检查内容。

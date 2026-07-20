# Codex Net Health

`codexnet` 是面向 Linux 终端的多会话 Codex 运维与异常解释器。它以只读方式组合 rollout 协议、结构化日志、进程、SQLite、TCP 和配置证据，集中显示所有工作区当前在做什么、哪些会话等待用户操作，以及异常结论的来源和新鲜度。

当前版本：`0.1.0`。

## 功能

- 自动发现当前用户运行的 Codex 会话、启动器、app-server 和辅助组件
- 同时观察不同 `CODEX_HOME` 和 `CODEX_SQLITE_HOME`，默认按真实 workspace 分组并显示 home 元数据
- 优先解析 Codex 官方 rollout 事件，识别 turn、推理摘要、工具、文件变更、compact、重连和失败
- Overview 直接显示当前工具、命令摘要、文件影响、subagent 与 action required
- Activity 展示语义里程碑；Diagnosis 展示当前结论、关键证据、数据质量和瓶颈
- Terminal 以只读终端视图展示当前运行中的后台 exec，并关联后续 poll 输出、PID、命令和 cwd
- 子进程 stdout/stderr 指向工作区或 `/tmp` 普通文件时，每 2 秒增量 tail；不读取 PTY、pipe 或 socket
- 单次 SSE idle timeout 显示为 `RECONNECTING`，恢复后记录 `RECOVERED`
- 每个终态模型失败显示结构化错误类型和完整脱敏 errmsg
- 汇总 Turn 耗时、TTFT、工具执行时间，以及 reconnect/fallback 恢复次数
- 展示单 Turn/累计 token、上下文占用、rate-limit 窗口和 credits
- 根据官方 thread 与 agent path 展示 subagent 层级和运行状态
- 比较 TCP 队列、收发字节、ACK 和重传；连续两个异常窗口后才确认阻塞
- 一条异常连接不会覆盖同进程中仍在传输的活跃连接
- 可选的高级实验诊断可被动解析 TLS ClientHello，提供连接归属辅助证据
- 提供基于 Textual 的响应式 TUI、文本输出、单次 JSON 和连续 NDJSON
- TUI 每 100ms 增量读取活动 rollout 事件，完整进程与网络采样仍保持默认 2 秒
- rollout 即使只增长 partial/ignored record，也会更新只读活动证据而不追加 Activity
- 每 2 秒读取 `/proc` CPU、I/O、context switch 和递归 child tree 差值
- 区分 `QUIET_ACTIVE`、`WAITING_UPSTREAM`、`QUIET_UNKNOWN`、`STALL_SUSPECT` 与 `OBSERVER_BLIND`
- 可选读取官方 TUI session log 的 outbound typed `Compact`，从提交时标注 requested
- 可选接收最小 PreCompact/PostCompact hook 事件，完整展示 requested/running/terminal
- 提供 `doctor` 数据源诊断、会话复盘导出和低基数 Prometheus 指标
- 可选独立 SQLite 历史库，保存关键事件与 10 秒/60 秒聚合桶
- 告警保留打开、升级、确认和恢复生命周期，供导出和内部状态追踪
- 每个会话在内存中保留最近 500 条标准化事件

## 环境要求

- Linux
- Python 3.10 或更高版本
- `ps`
- `ss`，通常由 `iproute2` 提供
- 对当前用户 Codex 进程的 `/proc` 和本地 Codex 数据具有读取权限

交互界面使用 Textual，采集与状态判断仍只依赖 Python 标准库和系统命令。项目不修改 Codex 配置、rollout 或 SQLite 数据库。

## 安装

使用 `pipx` 从工作区安装：

```bash
pipx install .
```

开发环境：

```bash
uv sync
uv run codexnet
```

安装后提供两个等价命令：

```text
codexnet
codex-net-health
```

文档统一使用 `codexnet`。源码工作区也可以直接运行 `./codex-net-health`。

## 使用

启动交互式 TUI：

```bash
codexnet
```

完成一个两秒采样窗口并输出文本：

```bash
codexnet --once
```

输出一个 JSON 快照：

```bash
codexnet --once --json
```

持续输出 NDJSON，每行都是独立的 schema 1 快照：

```bash
codexnet --json
```

检查数据源、采集器健康度和协议字段能力：

```bash
codexnet doctor
codexnet doctor --json
```

`doctor` 立即执行一次只读采样，不等待两秒基线窗口，也不会修改 Codex 数据或配置。

需要额外确认 TLS 连接目标时，可显式启用高级实验性的 Linux 原始套接字采集：

```bash
codexnet --packet-inspection
codexnet doctor --packet-inspection --json
```

该能力的数据面冻结且默认关闭，运行账户需要 `CAP_NET_RAW` 或 root。采集器只解析 ClientHello 的 SNI、ALPN、
TLS 版本和时间，并按当前 TCP 五元组短暂关联；不保留应用请求、响应或 TLS 密文。若系统不允许
打开原始套接字，`doctor` 和快照的 collector 诊断会显示原因，其他采集器继续工作。

如需从官方 TUI 提交 `/compact` 的时刻开始观察，可在启动 Codex 时显式设置：

```bash
CODEX_TUI_RECORD_SESSION=1
CODEX_TUI_SESSION_LOG_PATH=SESSION_LOG.jsonl
```

CodexNet 从对应进程环境发现该文件，只保留方向、typed op、session/turn 和时间戳；其他 prompt、
工具参数与输出在解析入口即丢弃。`doctor` 会报告是否启用、路径、可读性和 freshness。

PreCompact/PostCompact hook 可把 stdin 中的官方 hook payload 交给最小接收命令：

```bash
codexnet hook-event --hook-events CODEXNET_HOOKS.jsonl
codexnet --hook-events CODEXNET_HOOKS.jsonl
```

接收文件权限为 `0600`，仅保存 event、session、turn、trigger、timestamp 和 outcome。CodexNet
不会自行修改 Codex hook 或 `config.toml`。

### 网络解包范围

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `tls_server_name` | TLS SNI | 说明该连接请求的服务名 |
| `tls_alpn_protocols` | TLS ALPN | 显示客户端提供的应用协议 |
| `tls_versions` | TLS supported_versions | 显示客户端提供的 TLS 版本 |
| `tls_observed_at` | 本地采集时间 | 说明握手元数据的观察时间 |

解析器支持 Ethernet/VLAN、IPv4/IPv6、TCP 分段和跨 TLS record 的 ClientHello。它跳过 IP
分片，单流重组上限为 `64 KiB`、流状态保留 `15` 秒，已关联元数据最多保留 `512` 条、每条最长
`5` 分钟。网络证据只用于解释连接状态；会话生命周期仍以 rollout、日志和 SQLite 数据为准。

导出当前未解决事件，或按会话 ID/完整会话 key 导出复盘：

```bash
codexnet export --current-incidents
codexnet export --session SESSION_ID
```

会话复盘使用独立 `export_schema_version: 1`，包含状态机中最多 500 条完整保留事件、
Turn/工具摘要、恢复链、告警、失败 errmsg 和 TCP 证据。导出会脱敏凭据值，但仍可能包含
任务摘要和路径。

输出一次 Prometheus text format 指标：

```bash
codexnet metrics
```

指标 family 已冻结，仅使用实例、状态、类别和事件类型等低基数标签，不使用 session ID、PID、
errmsg 或网络端点；该命令是一次性输出，不包含内置 HTTP exporter。显式开启独立历史库：

```bash
codexnet --history HISTORY.sqlite
codexnet --once --history HISTORY.sqlite --history-days 14 --history-max-mib 64
```

历史功能默认关闭，不创建数据库、不写入 Codex 自有 SQLite，也不增加 TUI 表格；关键事件按
原始记录保存，会话采样按 10 秒、实例采样按 60 秒聚合，并按天数和空间上限淘汰。

筛选进程或 Codex home：

```bash
codexnet --pid 12345
codexnet --codex-home CODEX_HOME_A
codexnet --codex-home CODEX_HOME_A --codex-home CODEX_HOME_B
```

显示辅助进程或使用扁平视图：

```bash
codexnet --all
codexnet --flat
```

主要参数：

| 参数 | 含义 |
| --- | --- |
| `--interval SECONDS` | 刷新间隔，默认 `2` 秒 |
| `--idle-threshold SECONDS` | 已建立连接的空闲显示阈值，默认 `30` 秒 |
| `--event-lookback SECONDS` | TUI 和输出中的事件可见窗口，默认 `900` 秒 |
| `--pid PID` | 只观察指定 Codex PID，可重复 |
| `--codex-home PATH` | 只观察指定 home，可重复 |
| `--once` | 完成一个采样窗口后退出 |
| `--flat` | 启动时使用扁平会话视图 |
| `--all` | 在 text/JSON 输出中包含启动器、app-server 和辅助组件 |
| `--packet-inspection` | 被动解析 TLS ClientHello 元数据，要求 `CAP_NET_RAW` 或 root |
| `--json` | 单次模式输出 JSON，持续模式输出 NDJSON |
| `--no-color` | 关闭终端颜色 |
| `--history PATH` | 开启独立 SQLite 历史库 |
| `--hook-events PATH` | 读取 CodexNet 最小 compact hook NDJSON |
| `--history-days DAYS` | 历史保留天数，默认 `30` |
| `--history-max-mib MIB` | 历史库空间上限，默认 `128` MiB |

## TUI

TUI 的首屏是跨会话 Overview，目标是直接回答哪个工作区正在执行什么、哪个会话需要用户操作，以及哪里发生失败、恢复或阻塞：

```text
 CODEXNET   SESSIONS 3   ISSUES 1

 ▼ workspace-a
   CODEX_HOME CODEX_HOME_A · 2 sessions · 1 action required
 ? Review changes  ATTENTION · Allow command? · 18s
   context 98%
 ● Test session  SHELL · pytest tests/test_core.py · 42s
   1 tool running · 2 files
```

会话行的优先级是 attention、失败、恢复/阻塞、工具/文件/subagent、模型请求和空闲。审批、权限确认、用户问答、MCP elicitation 与登录操作使用独立 `AttentionState`，不会覆盖生命周期、恢复或网络状态。`Tab` 会在 action required、失败、严重停顿和网络阻塞会话之间跳转。

交互键：

| 按键 | 操作 |
| --- | --- |
| `↑` / `↓`、`j` / `k` | 移动选择 |
| `Enter` | 折叠/展开工作区，或在窄屏进入会话详情 |
| `1` / `2` / `3` | 切换 Activity / Diagnosis / Terminal |
| `/` | 搜索会话；Terminal tab 中搜索当前后台进程输出 |
| `]` | 跳到下一个需要关注的会话 |
| `Tab` / `Shift+Tab` | 切换焦点 |
| `f` | 切换 Activity 与 Terminal 的自动跟随，不持久化 |
| `n` / `Shift+N` | Terminal 搜索中跳到下一个或上一个匹配 |
| `g` | 切换分组和扁平视图 |
| `r` | 立即执行完整采样 |
| `?` | 打开快捷键与运行参数帮助 |
| `Esc` | 返回上一级 |
| `q` | 退出 |

Inspector 有三个页面：

- **Activity**：展示请求、工具、文件、action required、失败/恢复、compact 和 subagent 等语义里程碑。默认隐藏 keepalive、token/rate-limit 快照、普通 `MODEL_PROGRESS`、reasoning 和成功工具原文；已完成 compact 与工具边界折叠为摘要，领域事件和导出仍保留完整状态。
- **Diagnosis**：只展示当前结论、原因、最多三条关键证据、数据质量和最近 Turn 瓶颈。历史趋势、完整 collector/capacity、协议预览、agent tree 和逐连接详情由 doctor、export、metrics、history 或 JSON 承接。
- **Terminal**：只显示当前仍在运行且可关联 process ID 的后台进程，完成或 stale 的进程不占用界面。顶部使用 shell prompt 形式展示 cwd 与命令，正文按真实终端行排列，仅保留窄的 `OUT`、`ERR`、`TTY`、`SYS` 来源 gutter。数组型工具输出会先展开 content parts 并移除 Codex 的 `Script completed`/`Wall time` 外壳；控制序列被清理，截断和 dropped bytes 显式显示。普通 Codex TUI 不公开进程内逐字节输出，因此内容只在 Codex 写入新的 poll 结果后更新。

Overview 与固定 health strip 会显示 phase age、semantic silence、最近 evidence 和 observation
结论。1 秒时钟只重算显示 age，不调用进程发现、SQLite、`ss`、packet 或 history，也不向
Activity 追加 heartbeat。静默判断不会把 process alive、spinner 或 TCP established 描述成模型进展。

Compact 使用独立 operation lifecycle：`requested`、`candidate`、`running`、`completed`、
`failed`、`aborted`。运行中的 requested/running 始终可见；成功 terminal 后折叠成包含开始时间、
duration、trigger、source 和 confidence 的摘要；completion-only 不会虚构 start。

工作区默认分组，`g` 只切换当前运行中的分组状态，`--flat` 控制启动默认值；TUI 不再保存显示
偏好。每个 `CODEX_HOME` 的 `config.toml` 中 `model_auto_compact_token_limit` 仍作为采集配置，
实际 compact 只由 rollout/log 协议确认。启用 history 后，长期趋势继续通过 metrics、export 和
机器输出消费，不进入实时 Diagnosis。

宽度至少 96 列时保持 Overview 与 Inspector 双面板；更窄时使用列表/详情钻取；低于 50×20 显示尺寸提示。稳定刷新原地更新导航、Activity 与 Terminal，保留焦点、选择和滚动位置。

## 状态语义

界面分别维护四类状态，而不是用一条 TCP 连接或交互等待覆盖整个会话：

- **生命周期**：`IDLE`、`STARTING`、`WAITING_RESPONSE`、`GENERATING`、`RUNNING_TOOL`、`COMPACTING`、`COMPLETED`、`FAILED`、`ABORTED`
- **恢复状态**：`SUSPECT`、`RECONNECTING`、`TRANSPORT_FALLBACK`、`RECOVERED`
- **用户操作**：`APPROVAL`、`PERMISSIONS`、`USER_INPUT`、`MCP_ELICITATION`、`AUTH_ELICITATION`
- **网络证据**：`UNKNOWN`、`IDLE`、`ACTIVE`、`SUSPECT`、`STALLED`、`CLOSED`
- **静默判断**：`NORMAL`、`QUIET_ACTIVE`、`WAITING_UPSTREAM`、`QUIET_UNKNOWN`、`STALL_SUSPECT`、`OBSERVER_BLIND`

单次 timeout、历史 timeout、正常连接关闭和已经恢复的重试不会被标记为当前失败。`FAILED` 只来自 Codex 终态错误或明确的失败事件。

## 退出码

交互式 TUI 使用 `q` 正常退出时返回 `0`。单次和非交互模式只根据最后一个快照中仍然存在的问题返回：

| 退出码 | 含义 |
| --- | --- |
| `0` | 没有当前告警，或此前问题已经恢复 |
| `1` | 参数、依赖或运行环境导致监视器不能工作 |
| `2` | 当前存在已确认的网络阻塞 |
| `3` | 当前存在终态模型 turn 失败 |
| `4` | 当前存在严重阶段停顿，但尚无终态失败 |
| `130` | 用户中断 |

## 多实例发现

内部实例身份由 `(CODEX_HOME, CODEX_SQLITE_HOME)` 组成。监视器按以下顺序定位数据：

1. 从进程打开的 rollout 与 SQLite 文件确认实际路径。
2. 读取对应 `CODEX_HOME/config.toml`、进程环境变量和 cwd。
3. 通过进程父子关系关联启动器和辅助组件。
4. 使用 Codex 默认 home，同时在路径证据不足时显示数据不完整提示。

相对 `sqlite_home` 与 `CODEX_SQLITE_HOME` 按对应 Codex 进程的 cwd 解析。已打开 SQLite
文件是最强证据；文件尚未打开时，配置值优先于环境变量。SQLite 文件会先进行只读 schema
能力检查，缺少可选数据源不会中断其他实例。

## JSON

JSON 顶层包含：

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-15T19:00:00+08:00",
  "interval_seconds": 2.0,
  "collection_duration_seconds": 0.12,
  "summary": {},
  "diagnostics": [],
  "instances": []
}
```

实例中包含完整路径、发现方式、schema/协议能力、采集器健康度、未映射协议事件计数、进程和会话。会话除生命周期、恢复状态、网络证据、失败和标准化事件外，还包含 `turns`、`tool_executions`、`terminal_sessions`、`token_usage`、`cumulative_token_usage`、`rate_limits`、`agents` 与 `protocol_capabilities`。`terminal_sessions` 只输出 command、状态、capability、retained/dropped bytes 等摘要，不输出 transcript chunks。正文也不进入 export、history 或 Prometheus。以上字段是 schema 1 的向后兼容增量；未知标量使用 JSON `null`，未知集合使用空数组。

普通 `--once --json` 的 `schema_version` 与 `doctor --json` 的 `doctor_schema_version` 独立维护，当前都为 `1`。

默认 JSON 只列出会话进程；添加 `--all` 后同时列出启动器和辅助组件。诊断信息写入结构化字段，stdout 不混入横幅。

## 项目结构

```text
src/
├── cli.py                     # 参数和顶层异常处理
├── app.py                     # 运行模式与退出码
├── engine.py                  # 完整采集与快速事件刷新编排
├── snapshot_publisher.py      # 快照发布与可选 history 持久化
├── state_machine.py           # 生命周期、恢复和停顿状态机
├── models.py                  # 领域模型和 schema 值
├── diagnostics.py             # 采集器耗时、错误与 stale 状态
├── history.py                 # 可选独立 SQLite 历史库
├── codex/
│   ├── paths.py               # CODEX_HOME / SQLite home 与 /proc
│   ├── processes.py           # 进程发现和家族关联
│   ├── state_store.py         # 只读 SQLite 能力和批量查询
│   ├── rollout.py             # 增量 JSONL 与半行处理
│   ├── process_activity.py    # /proc process tree CPU/I/O 差值
│   ├── compact_evidence.py    # 可选 compact 旁路统一适配层
│   ├── tui_session_log.py     # typed Compact 白名单解析
│   ├── hook_events.py         # 最小 compact hook receiver/reader
│   └── events.py              # 官方协议与诊断日志标准化
├── network/
│   ├── sockets.py             # ss 采集和解析
│   ├── classifier.py          # TCP 证据判断
│   └── packet.py              # AF_PACKET 与 TLS ClientHello 元数据解析
└── presentation/
    ├── text.py
    ├── json_output.py
    ├── doctor.py
    ├── export.py
    ├── metrics.py
    ├── projection.py          # collector 与数据质量共享投影
    └── tui/
        ├── activity.py
        ├── controller.py
        ├── diagnosis.py
        ├── terminal_panel.py
        ├── textual_app.py
        ├── theme.py
        └── codexnet.tcss
```

依赖方向为：`cli/presentation -> app -> engine -> codex/network -> models`。底层采集器不依赖终端界面。

## 测试

```bash
uvx ruff check src tests
uv run python -m unittest discover -s tests -v
```

测试覆盖多 home、相对 SQLite 路径、schema 能力、rollout 半行与无语义增长、`/proc` CPU/I/O/child
差值、五类静默判断、Turn/工具/TTFT、typed Compact、Pre/PostCompact、manual/auto/remote、
retry/failure/abort、completion-only、500 条保留、网络聚合、两窗口阻塞、TLS ClientHello、JSON
schema，以及 Textual Pilot 下的宽屏、窄屏、稳定时钟、滚动/focus 和终端尺寸下限。

## 数据说明

监视器不会写入 Codex 数据。终端和 JSON 可能包含会话标题、任务摘要、文件路径、网络端点、TLS 服务名以及脱敏后的错误信息。向外分享输出前应检查这些内容。

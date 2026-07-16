# Codex Net Health

`codexnet` 是面向 Linux 终端的 Codex 会话、恢复过程和网络状态监视器。它以只读方式组合 Codex rollout 协议事件、结构化日志、进程信息与 TCP 指标，帮助判断会话是在正常生成、运行工具、等待上游、重连恢复，还是已经发生终态失败。

当前版本：`0.1.0`。

## 功能

- 自动发现当前用户运行的 Codex 会话、启动器、app-server 和辅助组件
- 同时观察不同 `CODEX_HOME` 和 `CODEX_SQLITE_HOME`，默认按实例分组
- 优先解析 Codex 官方 rollout 事件，识别 turn、模型输出、工具、compact、重连和失败
- 单次 SSE idle timeout 显示为 `RECONNECTING`，恢复后记录 `RECOVERED`
- 每个终态模型失败显示结构化错误类型和完整脱敏 errmsg
- 汇总 Turn 耗时、TTFT、工具执行时间，以及 reconnect/fallback 恢复次数
- 展示单 Turn/累计 token、上下文占用、rate-limit 窗口和 credits
- 根据官方 thread 与 agent path 展示 subagent 层级和运行状态
- 比较 TCP 队列、收发字节、ACK 和重传；连续两个异常窗口后才确认阻塞
- 一条异常连接不会覆盖同进程中仍在传输的活跃连接
- 提供分组 TUI、文本输出、单次 JSON 和连续 NDJSON
- 提供 `doctor` 数据源诊断、会话复盘导出和低基数 Prometheus 指标
- 可选独立 SQLite 历史库，保存关键事件与 10 秒/60 秒聚合桶
- 告警具有打开、升级、确认和恢复生命周期，并可在 TUI 中确认
- 每个会话在内存中保留最近 500 条标准化事件

## 环境要求

- Linux
- Python 3.10 或更高版本
- `ps`
- `ss`，通常由 `iproute2` 提供
- 对当前用户 Codex 进程的 `/proc` 和本地 Codex 数据具有读取权限

本项目运行时只使用 Python 标准库，不修改 Codex 配置、rollout 或 SQLite 数据库。

## 安装

使用 `pipx` 从工作区安装：

```bash
pipx install .
```

开发环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
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

指标仅使用实例、状态、类别和事件类型等低基数标签，不使用 session ID、PID、errmsg 或
网络端点。显式开启独立历史库：

```bash
codexnet --history ~/.local/state/codexnet/history.sqlite
codexnet --once --history /tmp/codexnet-history.sqlite --history-days 14 --history-max-mib 64
```

历史功能默认关闭，不写入 Codex 自有 SQLite；关键事件按原始记录保存，会话采样按 10 秒、
实例采样按 60 秒聚合，并按天数和空间上限淘汰。

筛选进程或 Codex home：

```bash
codexnet --pid 12345
codexnet --codex-home ~/.codex
codexnet --codex-home ~/work/codex-a --codex-home ~/work/codex-b
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
| `--all` | 包含启动器、app-server 和辅助组件 |
| `--json` | 单次模式输出 JSON，持续模式输出 NDJSON |
| `--no-color` | 关闭终端颜色 |
| `--history PATH` | 开启独立 SQLite 历史库 |
| `--history-days DAYS` | 历史保留天数，默认 `30` |
| `--history-max-mib MIB` | 历史库空间上限，默认 `128` MiB |

## TUI

TUI 默认按 Codex 实例展开：

```text
 CODEX NET HEALTH  v0.1.0                              LIVE  20:25:21
  实例 2  会话 3  失败 0  告警 0  阻塞 0  采集 18ms
────────────────────────────────────────────────────────────────────
  ▼ ~/.codex  2 会话
▶ │ 当前会话 · 8s                          [已选中 · 模型正在生成]
  │ 另一个会话 · 24s                               [正在等待上游]
  ▼ ~/work/codex-b  1 会话  DB ~/runtime/codex-b
  │ 数据处理 · 3s                                       [正在重连]
```

交互键：

| 按键 | 操作 |
| --- | --- |
| `↑` / `↓`、`j` / `k` | 移动选择 |
| `PgUp` / `PgDn`、`Ctrl-U` / `Ctrl-D` | 按页移动 |
| `Home` / `End` | 跳到当前视图首尾 |
| `Enter` | 进入 Home 或会话详情 |
| `Space` | 在 Overview 折叠或展开实例 |
| `1` / `2` / `3` | 切换 Timeline / Turns / Evidence |
| `c` | 在 Home Detail 打开多 Home 对比 |
| `x` | 在会话详情确认最新活动告警 |
| `/` | 按标题、任务、模型、会话 ID 或错误信息搜索 |
| `Tab` | 跳到下一个失败、严重停顿或网络阻塞会话 |
| `f` | 切换事件时间线自动跟随 |
| `g` | 切换分组和扁平视图 |
| `a` | 显示或隐藏辅助进程 |
| `?` | 打开快捷键帮助 |
| `Esc` | 返回上一级 |
| `q` | 退出 |

导航分为 `Overview → Home Detail → Session Detail` 三层。Home Detail 可打开多 Home 对比，比较活跃会话、失败、重连、阻塞、数据完整度、TTFT 和 rate limit。Session Detail 顶部常驻网络和生命周期状态，并提供 Timeline、Turns、Evidence 三种内容模式。Timeline 同时显示告警生命周期；Turns 汇总耗时、TTFT、工具与 token；Evidence 展示告警、错误、rate limit、TCP、数据源诊断和 subagent 树。列表仅用整行反色标记当前选择，事件摘要按失败、警告、恢复、工具和请求阶段使用不同颜色。

## 状态语义

界面分别维护三类状态，而不是用一条 TCP 连接覆盖整个会话：

- **生命周期**：`IDLE`、`STARTING`、`WAITING_RESPONSE`、`GENERATING`、`RUNNING_TOOL`、`COMPACTING`、`COMPLETED`、`FAILED`、`ABORTED`
- **恢复状态**：`SUSPECT`、`RECONNECTING`、`TRANSPORT_FALLBACK`、`RECOVERED`
- **网络证据**：`UNKNOWN`、`IDLE`、`ACTIVE`、`SUSPECT`、`STALLED`、`CLOSED`

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

1. 读取每个 Codex 进程的环境变量和 cwd。
2. 从进程打开的 rollout 与 SQLite 文件反推路径。
3. 通过进程父子关系关联启动器和辅助组件。
4. 使用默认 `~/.codex`，同时在路径证据不足时显示数据不完整提示。

相对 `CODEX_SQLITE_HOME` 按对应 Codex 进程的 cwd 解析，与 Codex 自身行为一致。SQLite 文件会先进行只读 schema 能力检查，缺少可选数据源不会中断其他实例。

## JSON

JSON 顶层包含：

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-15T19:00:00+08:00",
  "interval_seconds": 2.0,
  "summary": {},
  "diagnostics": [],
  "instances": []
}
```

实例中包含完整路径、发现方式、schema/协议能力、采集器健康度、未映射协议事件计数、进程和会话。会话除生命周期、恢复状态、网络证据、失败和标准化事件外，还包含 `turns`、`tool_executions`、`token_usage`、`cumulative_token_usage`、`rate_limits`、`agents` 与 `protocol_capabilities`。这些字段是 schema 1 的向后兼容增量；未知标量使用 JSON `null`，未知集合使用空数组。

普通 `--once --json` 的 `schema_version` 与 `doctor --json` 的 `doctor_schema_version` 独立维护，当前都为 `1`。

默认 JSON 只列出会话进程；添加 `--all` 后同时列出启动器和辅助组件。诊断信息写入结构化字段，stdout 不混入横幅。

## 项目结构

```text
src/
├── cli.py                     # 参数和顶层异常处理
├── app.py                     # 运行模式与退出码
├── engine.py                  # 多实例增量采样引擎
├── state_machine.py           # 生命周期、恢复和停顿状态机
├── models.py                  # 领域模型和 schema 值
├── diagnostics.py             # 采集器耗时、错误与 stale 状态
├── history.py                 # 可选独立 SQLite 历史库
├── codex/
│   ├── paths.py               # CODEX_HOME / SQLite home 与 /proc
│   ├── processes.py           # 进程发现和家族关联
│   ├── state_store.py         # 只读 SQLite 能力和批量查询
│   ├── rollout.py             # 增量 JSONL 与半行处理
│   └── events.py              # 官方协议与诊断日志标准化
├── network/
│   ├── sockets.py             # ss 采集和解析
│   └── classifier.py          # TCP 证据判断
└── presentation/
    ├── text.py
    ├── json_output.py
    ├── doctor.py
    ├── export.py
    ├── metrics.py
    └── tui/
        ├── controller.py
        ├── views.py
        └── terminal.py
```

依赖方向为：`cli/presentation -> app -> engine -> codex/network -> models`。底层采集器不依赖终端界面。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

测试覆盖多 home、相对 SQLite 路径、schema 能力、rollout 半行、Turn/工具/TTFT、token/rate limit、subagent、doctor、重连恢复、终态 errmsg、500 条保留、网络聚合、两窗口阻塞、JSON schema 和三级 TUI。

## 数据说明

监视器不会写入 Codex 数据。终端和 JSON 可能包含会话标题、任务摘要、文件路径、网络端点以及脱敏后的错误信息。向外分享输出前应检查这些内容。

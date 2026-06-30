# OpenClaw — 飞书控制 Claude Code CLI

一个**飞书机器人后端**：通过飞书 WebSocket 长连接接收用户消息，调用本地 `claude` CLI（Claude Code）作为 AI 代理，结果实时流式回写到飞书卡片。

```
飞书用户消息 → WebSocket → FastAPI(_dispatch 路由) → asyncio 子进程(claude CLI) → JSONL 流 → 飞书卡片更新
                                                          ↓
                                                  PreToolUse hook → 审批卡片（按需）
```

## 一、快速开始（配置步骤）

### 1. 安装 Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version    # 验证
```

OpenClaw 通过 `shutil.which()` 解析可执行文件，**Windows 上会自动找到 `claude.CMD`**，不需要手动配置完整路径。

### 2. 安装 Python 依赖

```bash
uv sync
# 或 pip install -r requirements.txt
```

### 3. 配置飞书应用

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建应用并启用**机器人**能力
2. 添加事件与回调：均选择**长连接（WebSocket）**模式
   - 事件订阅：`im.message.receive_v1`（收消息）
   - 回调订阅：`card.action.trigger`（卡片交互）
3. 权限管理：
   - `im:message` — 接收消息
   - `im:message:send_as_bot` — 发送消息
   - `im:resource` — 上传文件/图片

### 4. 配置 `.env`

```bash
cp .env.example .env
```

关键字段：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | `""` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | `""` | 飞书应用密钥 |
| `FEISHU_VERIFICATION_TOKEN` | `""` | 事件验证 token |
| `CLAUDE_CLI_PATH` | `"claude"` | Claude CLI 名称或绝对路径（用 `shutil.which` 解析） |
| `CLAUDE_DEFAULT_MODEL` | `"sonnet"` | 默认模型 |
| `DEFAULT_WORKSPACE` | `"D:\projects"` | 默认工作目录 |
| `ALLOWED_USERS` | `""` | 允许的 open_id 列表（逗号分隔，空=全部允许） |
| `APPROVAL_MODE` | `"m"` | 默认审批模式（h/m/l） |
| `OPENCLAW_HOST` / `OPENCLAW_PORT` | `localhost` / `8080` | hook 脚本回调 OpenClaw 的地址 |

### 5. 创建供应商 Profile

OpenClaw 通过 `~/.claude/settings_<name>.json` 管理多个 API 供应商（GLM、Kimi、DeepSeek 等）。每个文件是完整的 claude 配置，**核心是 `env` 字段**，会被注入到子进程环境变量。

`~/.claude/settings_glm.json` 示例：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "your-glm-key",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4-flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4-air",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-4-plus"
  }
}
```

在飞书里用 `/switch` 命令切换。切换后只影响**新启动**的 openclaw 子进程，**不污染 `~/.claude/settings.json`**（不再拷贝文件）。

### 6. 启动

```bash
uv run python -m app.main
```

启动后日志会打印：默认 workspace、允许的用户列表、Claude CLI 路径、WS 连接状态。

---

## 二、Dispatch 路由表（`app/feishu/events.py::_dispatch`）

`on_message_receive` 解析飞书消息后，`_dispatch` 按内容路由。**所有命令通过 `asyncio.get_running_loop().create_task(_dispatch(...))` 异步派发**，3s 内返回飞书避免重推。

| 命令 | 功能 | 实现要点 |
|---|---|---|
| `/start` `/new` | 重置会话 | `reset_user_session` + kill 当前 CLI 进程；下次消息开**新会话**（不传 `--continue`） |
| `/stop` | 中断当前子进程 | `cancel_and_wait`；pending future 标记为 `cancelled` |
| `/status` | 查看会话状态 | 显示 session_id、claude_session_id、workspace、模型、profile、**消息数（读 claude jsonl）**、上下文用量 |
| `/switch` | 切换 API 供应商 | 写 `~/.openclaw_active_profile` 标记文件 → kill 进程 → 测连通 |
| `/model haiku\|sonnet\|opus` | 切换模型 | 写 `session.last_model` → kill 进程 → 自动重跑上一条 user 消息 |
| `/mode h\|m\|l` | 切换审批模式 | `h` 模式启动 claude 时加 `--permission-mode bypassPermissions` |
| `/pwd <path\|shortcut>` | 切换工作区 | 清空 `claude_session_id`（换 workspace 不能 continue） |
| `/resume <session_id>` | 用原生 `--resume <id>` 恢复 | kill 进程 → 一次性 resume（不绑定 session） |
| `/continue [prompt]` | 用原生 `--continue` 恢复 | 写哨兵值 `__continue__` 强制走 `--continue` 分支 |
| `/compact` | 触发 CLI 内置上下文压缩 | 把 `/compact` 作为 prompt 发给 claude |
| `/mem <text>` | 追加到工作区 `CLAUDE.md` | claude 原生加载，下次对话自动生效；首次写入时迁移老 `AGENT.md` |
| `/sh <command>` | 工作区执行 shell（30s 超时） | 直接 `subprocess`，不走 claude、不审批 |
| `/clean` | 清理旧会话文件 | 只保留当前 session |
| 普通文本 | 调用 Claude CLI 执行 | 走 `_run_claude` |

### `_run_claude` 内部模型选择流程

1. **有 `last_model`**（`/model` 设过或之前选过）→ 直接用，跳过选择卡片
2. **首次消息**（无 last_model 且进程未启动）→ 弹模型选择卡片 → 用户选 → 执行
3. **失败** → 清空 `session.claude_session_id`，下次开新会话（避免 `--continue` 到坏 session）
4. **进程被取消**（`/switch`/`/stop`/`/start`）→ 返回 `cancelled`，静默退出

---

## 三、架构总览

### FastAPI lifespan 启动（`app/main.py`）

- 基于 **FastAPI**，启动时通过 `lifespan` 建立 Feishu WebSocket 长连接
- 核心方法：`ws_client.start_async()` → `_connect()` → 启动 `_receive_message_loop()` + `_ping_loop()`
- `_receive_message_loop` 是主循环，从 ws 拉取消息并分发处理
- `_ping_loop` 每 10s 发送一次心跳包保持连接
- `_select()` 阻塞保活

```
_running_loop().create_task(self._ping_loop())   # 1. 心跳任务（瞬间完成，不等待）
_running_loop().create_task(self._recv_loop())   # 2. 接收任务（瞬间完成，不等待）
await self._select()                             # 3. 主流程在这里挂起，保活
```

### WebSocket 后端 → 事件路由 → CLI 子进程

```
飞书 WebSocket 帧
     │
     ▼
WsClient._handle_data_frame()             # app/feishu/ws.py
     │  (重写以支持 card.action.trigger)
     ▼
on_message_receive / on_card_action       # app/feishu/events.py
     │  (lark_oapi 注册的事件处理器)
     ▼
asyncio.get_running_loop().create_task(
    _dispatch(text, open_id, chat_id)     # 异步派发，3s 内返回飞书
)
     │
     ▼
_dispatch 内部 if/elif 链匹配命令         # 命令路由表
     │
     ├─ 系统命令 (/start, /status, /model...) → 直接处理，return
     │
     └─ 普通文本 / /continue / /resume → _run_claude()
                                              │
                                              ▼
                                    claude_cli_loop.send_and_wait()
                                              │  (app/agent/cli_loop.py)
                                              ▼
                                    _start_process() 首次启动子进程
                                              │
                                              ▼
                                    asyncio.create_subprocess_exec(...)
                                              │
                                              ▼
                                    后台 task: _read_loop 读 stdout JSONL
```

### asyncio 子进程调用 Claude CLI（`ClaudeCLILoop`）

1. 检查 per-user Lock（同一用户同时只能跑一个 claude）
2. `shutil.which(cli_path)` 解析可执行文件路径（Windows 找到 `claude.CMD`）
3. `_build_env(workspace)` 复制环境变量 + 注入 active profile 的 env vars
4. `_ensure_hook_config(workspace)` 注入 PreToolUse hook 到 `workspace/.claude/settings.local.json`
5. `create_subprocess_exec` 启动 claude 进程
6. 启动 stderr 后台读取 task
7. `while True: readline() → json.loads → 按事件类型分发`
8. 进程退出后清理

**启动参数**：

```python
args = [
    resolved_cli_path,                  # shutil.which("claude") → claude.CMD
    "--print",
    "--output-format", "stream-json",   # JSONL 流式输出
    "--input-format", "stream-json",    # stdin 也走 JSONL
    "--verbose",
    "--include-partial-messages",       # 包含部分消息（实时流）
]
# 会话恢复（原生）
if resume_session_id:
    args.extend(["--resume", resume_session_id])   # /resume <id>
elif claude_session_id:
    args.append("--continue")                       # /continue
# 审批模式（原生）
if approval_mode == "h":
    args.extend(["--permission-mode", "bypassPermissions"])
# 模型
if chosen_model:
    args.extend(["--model", chosen_model])
```

---

## 三、日志位置与错误排查

OpenClaw 运行时会写两类日志，出问题时按下面位置取：

### 1. 运行日志 `openclaw.log`

- **位置**：项目根目录 `openclaw.log`（启动时的工作目录下）
- **配置**：`app/main.py:17-22` 的 `logging.basicConfig`，同时输出到 `StreamHandler`（控制台）和 `FileHandler("openclaw.log")`
- **级别**：`INFO` 及以上
- **内容**：FastAPI lifespan、WS 连接状态、消息派发、Claude CLI 启动/退出、JSONL 事件分发异常等
- **排查命令**：

  ```bash
  # 实时跟踪
  tail -f openclaw.log

  # 看最近的错误
  grep -i "error\|exception\|traceback" openclaw.log | tail -50

  # 看某个用户的所有活动
  grep "ou_xxx" openclaw.log
  ```

### 2. 审计日志 `audit.log`

- **位置**：`.env` 的 `AUDIT_LOG_PATH` 配置，默认 `./audit.log`
- **格式**：JSON-lines，每行一条 `{"timestamp", "user_id", "action", "command", "status", ...}`
- **写入**：`app/audit/logger.py`，由 `events.py` 在命令接收、agent 完成时调用
- **排查命令**：

  ```bash
  # 看某用户的命令历史
  cat audit.log | python -c "import sys,json;[print(json.loads(l).get('command','')) for l in sys.stdin if 'ou_xxx' in l]"

  # 看失败的任务
  grep '"status": "failed"' audit.log | tail -20
  ```

### 3. Claude CLI 子进程 stderr

claude 子进程的 stderr 不直接落盘，而是由 `cli_loop.py` 的 `_drain_stderr` 后台 task 捕获：

- 每行 stderr 实时打到 `openclaw.log`，前缀 `claude stderr: ...`
- 进程退出后最近 10 行存到 `_last_error`，作为 `AgentResult.error` 返回给 `_run_claude`
- `_run_claude` 失败时会通过飞书消息把错误反馈给用户

### 4. 会话文件 `.sessions/`

- **位置**：项目根目录 `.sessions/{open_id}.json`
- **用途**：每个用户的 openclaw session 状态（不是 claude 的对话历史）
- **内容**：session_id、claude_session_id、workspace、approval_mode、last_model、context_tokens
- **claude 真实对话历史**：在 `~/.claude/projects/<encoded-workspace>/<claude_session_id>.jsonl`，由 claude 自己管理

### 5. profile 切换标记 `~/.openclaw_active_profile`

- **位置**：`~/.openclaw_active_profile`（单行文本，内容是 active profile 名）
- **用途**：`_build_env` 读它来决定注入哪个 profile 的 env vars
- **错误表现**：内容为空或指向不存在的 profile → `get_active_profile()` 返回 `"unknown"`，子进程不注入 `ANTHROPIC_*`，claude 会回退到 `~/.claude/settings.json`

### 6. 飞书 API 日志

`httpx` 的请求日志（POST/GET 飞书 open-apis）也会进 `openclaw.log`，前缀 `[httpx]`：

```
2026-06-16 09:53:10,969 [httpx] INFO: HTTP Request: POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id "HTTP/1.1 200 OK"
```

排查飞书 API 报错（4xx/5xx）时直接 grep `[httpx]` 看响应码。

---

## 四、CLI stdin/stdout 通过 JSONL 流更新飞书卡片

### stdin：写入用户消息

`send_and_wait` 通过 `proc.stdin` 写入一行 JSONL，然后 `writer.close()` 触发 claude 处理：

```python
payload = json.dumps({
    "type": "user",
    "message": {"role": "user", "content": prompt},
}) + "\n"
writer.write(payload.encode())
await writer.drain()
writer.close()
```

### stdout：解析 JSONL 流事件

后台 `_read_loop` 逐行读取，按 `type` 字段分发：

| 事件类型 | 处理 |
|---|---|
| `system/init` | 提取 `session_id` → 注册到 `session_registry`（hook 路由用） |
| `stream_event` | 累积 `text_delta` → **每 500ms 更新一次飞书卡片**（流式展示）；超过 3000 字符自动开新卡 |
| `assistant` | 提取 `tool_use` 块记录工具调用；非流式模式补齐文本 |
| `result` | 解析最老的 pending future → 更新最终卡片（费用/耗时/轮次/token） |
| `system/api_retry` | 通知用户 API 重试中 |

### 流式卡片更新流程

```
claude stdout:
  {"type":"stream_event","event":{"delta":{"type":"text_delta","text":"你"}}}
  {"type":"stream_event","event":{"delta":{"type":"text_delta","text":"好"}}}
  {"type":"stream_event","event":{"delta":{"type":"text_delta","text":"！"}}}
  ...
  {"type":"result","result":"你好！","total_cost_usd":0.001,...}
       │
       ▼
_read_loop 累积 msg_state["text"]
       │
       ├─ now - last_update >= 0.5s → feishu_client.update_card(card_id, ...)
       │                              节流避免飞书限流
       │
       └─ len(text) >= 3000         → 把当前卡片标为"已满"
                                       开新卡片继续流式
       │
       ▼
result 事件 → feishu_client.update_card(card_id, build_tool_result_card(...))
                  最终卡片含：耗时、token 数、费用、工具调用数
```

**并发隔离**：每个 pending 消息有独立的 `msg_state`（text 累积器、tools 列表、card_id），多轮对话状态不会互相污染。

---

## 五、Profile 切换（`app/profiles.py`）

支持多个 API 供应商（GLM、Kimi、DeepSeek 等）。**完全不修改 `~/.claude/settings.json`**，机制如下：

| 文件/对象 | 作用 |
|---|---|
| `~/.claude/settings_<name>.json` | profile 定义（含 `env` 字段） |
| `~/.openclaw_active_profile` | 标记当前 active profile 名（单行文本） |
| `load_active_profile_env()` | 读 active profile 的 `env` 字段返回 dict |
| `_build_env(workspace)` | 调 `load_active_profile_env()` 注入子进程环境 |

**切换流程**：

```
用户点 /switch 卡片 → switch_profile(name)
                          │
                          ▼ 不拷贝文件，只写标记
                  ~/.openclaw_active_profile = "glm"
                          │
                          ▼ kill 当前 CLI 进程
                  claude_cli_loop.cancel_by_user(open_id)
                          │
                          ▼ 测连通
                  test_profile() → POST {base_url}/messages
                          │
                          ▼ 下次用户发消息时
                  _build_env() 注入 ANTHROPIC_BASE_URL 等
                  → 新 claude 子进程用新 profile
```

`test_profile()` 直接用将要注入的 env 测试，验证的就是 claude 实际看到的环境，与全局 `settings.json` 无关。

---

## 六、Hook 审批（`app/hooks/router.py`）

claude 的 `PreToolUse` hook 触发时，调用 `scripts/hooks/pre_tool_use.py`，该脚本把工具调用信息 HTTP POST 到 `/hooks/pre_tool_use`。

```
claude 工具调用 → PreToolUse hook → pre_tool_use.py → HTTP POST /hooks/pre_tool_use
                                                          │
                                                          ▼
                                            hooks/router.py 处理
                                                          │
                                            ┌─────────────┴─────────────┐
                                            │                           │
                                  h 模式（已 bypass）            m / l 模式
                                  claude 直接放行                       │
                                                            session_registry 查 open_id
                                                                       │
                                                            飞书审批卡片 → 用户点
                                                                       │
                                                            approval_manager 解除 await
                                                                       │
                                                            HTTP 响应回 hook 脚本
                                                                       │
                                                            hook stdout 返回决策给 claude
```

**Hook 注入位置**：`workspace/.claude/settings.local.json`（claude 原生的本地覆盖，**不覆盖用户的 `settings.json`**）。claude 合并 settings 时把 openclaw 的 hook 与用户自己的合并。

**三级审批模式**：

| 模式 | 代码 | 行为 |
|---|---|---|
| 高容忍 | `h` | 启动 claude 时加 `--permission-mode bypassPermissions`，**完全跳过 hook** |
| 中风险 | `m` | claude default mode + PreToolUse hook；Write/Edit/NotebookEdit 始终高风险；Bash 按命令内容分析 |
| 低容忍 | `l` | claude default mode + 所有工具都走 hook 审批 |

**Bash 风险分析（mode m）**：
- 安全命令（`ls`, `cat`, `grep`, `cd`, `echo`, `python -c` 等）自动放行
- 高风险关键字（`rm`, `git push`, `pip install`, `curl -X POST` 等）需审批
- 命令链 `&&`/`;`/`|` 只检查第一段命令
- 未知命令默认高风险

---

## 七、会话管理与上下文监控

### `SessionManager`

- 每个用户（`open_id`）一个 session，持久化在 `.sessions/{open_id}.json`
- session 存：openclaw session_id、claude_session_id、workspace、approval_mode、last_model、context_tokens
- **不再自存消息历史**：用户消息数、最近 user message 等都从 claude 原生 jsonl 读（`~/.claude/projects/*/<sid>.jsonl`）

### `session.claude_session_id` 的双重角色

1. **"有历史可继续"开关**：truthy → 下次启动 claude 加 `--continue`
2. **"最近一次会话 id"**：用于在 `~/.claude/projects/*/` 下定位 jsonl 文件，回读消息历史

### 上下文监控

长时间 `--continue` 后上下文会满。每次任务完成时从 `result.usage.input_tokens` 拿到当前上下文大小：

- ≥ 80%：完成通知追加警告，建议 `/compact`
- ≥ 95%：建议 `/start` 新会话
- `/status` 实时显示百分比

---

## 八、飞书 SDK 注意事项

### 常用类

1. **卡片回调**：`P2CardActionTriggerResponse`、`CardActionResponse`、`P2MessageCardUpdateResponse`、`CardFormSubmitResponse`
2. **消息/富媒体**：`MessageContent`（不能 `MessageContent(text="hi")`）、`TextMessageContent`、`ImageMessageContent`、`FileMessageContent`、`CardMessageContent`（不能直接传 `card=...`）
3. **事件回调**：`EventHeader`、`P2EventCallbackResponse`、`AppEventReceiveResponse`
4. **多维表格/文档**：`BitableRecord`（不能 `BitableRecord(fields={...})`）、`BitableField`、`DocumentContent`、`SheetData`

### loop 问题

`lark_oapi` SDK 在模块顶层导入时就抓取 loop，所有 loop 方法必须改成用当前 running loop：

```python
# ❌ 顶层 loop
loop.create_task(...)
# ✅ 当前 running loop
asyncio.get_running_loop().create_task(...)
```

### 卡片回调在 ws 中被丢弃

原 SDK 卡片回调走 HTTP，ws 不识别。本项目重写了 `_handle_data_frame` 来支持 card 事件。

### 接口提示

1. 无参数类不支持关键词参数传入
2. 文件上传必须传文件对象，不能传 `f.read()`：
   ```python
   # ❌ client.file.upload(file=f.read())
   # ✅ client.file.upload(file=f)
   ```
3. SDK 只自动管 `tenant_access_token`，用户态 Token 要自己刷新
4. 枚举类不能传字符串，必须用枚举值
5. 长连接事件 3s 超时必重推 → 异步处理（开线程/队列），3s 内返回空响应
6. 卡片模板参数写了字符串，但回调响应必须传对象：`resp.card` 必须是 dict/对象，不能是 JSON 字符串

---

## 九、Claude Code 支持的 Hook 类型

| Hook | 触发时机 |
|---|---|
| `SessionStart` | 会话刚启动 |
| `UserPromptSubmit` | 用户提交输入（回车） |
| `PreToolUse` | 调用工具前（写文件/执行命令等）← **OpenClaw 用这个做审批** |
| `PostToolUse` | 调用工具后 |
| `Notification` | 需要通知用户时 |
| `Stop` | 当前助手回合结束 |
| `SubagentStop` | 子代理结束 |

---

## 十、项目结构

```
openclaw/
├── config/settings.py        # 配置管理（pydantic-settings）
├── app/
│   ├── main.py               # FastAPI 入口 + WS 长连接
│   ├── profiles.py           # API 供应商 profile 切换（标记文件 + env 注入）
│   ├── feishu/               # 飞书集成
│   │   ├── client.py         # 飞书 API 客户端
│   │   ├── events.py         # 消息分发 + 会话管理 + 命令路由
│   │   ├── cards.py          # 卡片模板（流式/审批/模型选择/profile切换）
│   │   └── ws.py             # WebSocket 长连接（子类化 WsClient）
│   ├── agent/                # Agent 执行层
│   │   └── cli_loop.py       # Claude CLI 子进程管理 + JSONL 事件解析
│   ├── approval/             # 审批流程
│   │   └── manager.py        # 审批状态机（Future + 超时）
│   ├── audit/                # 审计日志
│   │   └── logger.py         # JSON-lines 日志
│   ├── hooks/                # Hook 回调
│   │   └── router.py         # PreToolUse HTTP 端点 + Bash 风险分析
│   └── models/schemas.py     # 数据模型（Session, AgentResult 等）
├── scripts/hooks/            # Hook 脚本
│   └── pre_tool_use.py       # PreToolUse hook 脚本（stdin→HTTP→stdout）
├── .env.example              # 环境变量模板
├── flow/                     # 架构图
│   ├── architecture.html     # HTML 架构图（ws→events→cli_loop + JSONL 流）
│   ├── ASYNC_FLOW.drawio     # Draw.io 架构图
│   └── ASYNC_FLOW_MERMAID.md # Mermaid 流程图
└── TROUBLESHOOTING.md        # 踩坑记录
```

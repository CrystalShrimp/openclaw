# OpenClaw 踩坑记录与排查指南

> 本文档按**架构分块**组织所有踩坑记录。每块开头列出该层的**脆弱点**和**排查入口**，便于从现象快速定位。
>
> 六个分块对应数据流的六个阶段：
>
> ```
> 飞书WS → 事件路由 → 子进程管理 → Claude CLI → 飞书卡片
>                                            ↑
>                                       Hook 审批（旁路）
> ```

---

## 目录

- [一、飞书 WebSocket 层](#一飞书-websocket-层)
- [二、事件路由层（events.py）](#二事件路由层eventspy)
- [三、子进程管理层（cli_loop.py）](#三子进程管理层cli_looppy)
- [四、Claude CLI 层](#四claude-cli-层)
- [五、飞书卡片层](#五飞书卡片层)
- [六、Hook 审批层](#六hook-审批层)
- [快速排查清单](#快速排查清单)

---

# 一、飞书 WebSocket 层

**位置**：`app/feishu/ws.py`（`FeishuWsClient` 子类化 SDK 的 `WsClient`）

**核心脆弱点**：

1. **Event loop 绑定**：SDK 在模块顶层捕获 `asyncio.get_event_loop()`，而 uvicorn 用自己的 loop → SDK 的 `loop.create_task` 指向错误 loop，任务永远不执行
2. **CARD 类型帧被丢弃**：SDK 的 `_handle_data_frame` 对 CARD 直接 `return`，所有审批按钮回调丢失
3. **挂起 Future 阻止退出**：审批 Future 等待卡片点击，卡片因其他 bug 没送达 → Ctrl+C 无法停服务

**排查入口**：
- 日志里有 `connected to wss://...` 但没 `receive pong` / 没任何消息分发 → loop 错误
- 点审批按钮报 `code 200340` → CARD 帧被丢
- Ctrl+C 无效 → `taskkill /F /PID`，检查 hook handler 是否有 Future 卡住

---

## 1.1 WS 连接正常但收不到消息（核心问题）

**现象**：WS 连接成功（`connected to wss://...`），ping 也正常，但发送消息给机器人后日志里没有任何消息接收记录。

**排查过程**：

1. 写了最小化测试脚本 `test_ws.py`，直接用 SDK 原生 `WsClient`（不走 FastAPI），结果能收到消息
2. 对比测试脚本和集成代码的差异，发现根因：`lark_oapi/ws/client.py` 模块顶层捕获了 `loop = asyncio.get_event_loop()`，而 uvicorn 运行时用的是自己的 event loop

**根因代码（SDK 内部）：**

```python
# lark_oapi/ws/client.py 模块顶层
try:
    loop = asyncio.get_event_loop()  # ← 导入时捕获的 loop
except RuntimeError:
    loop = asyncio.new_event_loop()

# _connect() 方法内
loop.create_task(self._receive_message_loop())  # ← 用的是错误的 loop

# _receive_message_loop() 方法内
loop.create_task(self._handle_message(msg))  # ← 同样的问题
```

**解决方案**：子类化 `WsClient`，覆盖 `_connect()` 和 `_receive_message_loop()` 方法，将所有 `loop.create_task()` 替换为 `asyncio.get_running_loop().create_task()`：

```python
class FeishuWsClient(_BaseClient):
    async def _connect(self) -> None:
        asyncio.get_running_loop().create_task(self._receive_message_loop())

    async def _receive_message_loop(self):
        while True:
            msg = await self._conn.recv()
            asyncio.get_running_loop().create_task(self._handle_message(msg))
```

**验证方法**：修复后日志中出现 `receive pong`（之前没有），说明 receive loop 在正确的 loop 上运行了。

**教训**：在 FastAPI/uvicorn 环境中使用任何通过模块级变量缓存 event loop 的第三方库时，都必须注意 loop 一致性问题。

---

## 1.2 SDK 原生 WS 客户端丢弃 CARD 类型消息

**现象**：审批卡片的 Approve/Reject 按钮点击后报错 `code 200340`。

**原因**：SDK 的 `_handle_data_frame()` 方法中对 `MessageType.CARD` 直接 `return`，不做任何处理：

```python
if message_type == MessageType.EVENT:
    result = self._event_handler.do_without_validation(pl)
elif message_type == MessageType.CARD:
    return  # ← 卡片回调被直接丢弃
```

**解决方案**：子类覆盖 `_handle_data_frame`，同时处理 EVENT 和 CARD：

```python
async def _handle_data_frame(self, frame):
    if message_type in (MessageType.EVENT, MessageType.CARD):
        result = self._event_handler.do_without_validation(pl)
```

**仅覆盖代码还不够**，还需要在飞书开发者后台配置：
1. 进入应用 → **事件与回调** → 事件配置
2. 添加 `card.action.trigger` 事件订阅
3. 确保推送方式选 **长连接（WebSocket）**
4. 发布新版本

---

## 1.3 服务无法用 Ctrl+C 停止

**现象**：按 Ctrl+C 一次或两次都无法停止服务，进程卡住。

**原因**：有挂起的 asyncio Future（hook handler 等待审批卡片点击，但卡片因其他 bug 没发出），asyncio 事件循环无法正常关闭。

**解决方案**：

- 短期：用 `taskkill /F /PID <pid>` 强制杀进程
- 长期：所有 Future 必须有超时兜底（`_expire_after`），避免无限挂起

**关键点**：任何 `await future` 都必须有超时保护，否则整个进程无法优雅退出。

---

# 二、事件路由层（events.py）

**位置**：`app/feishu/events.py`（`on_message_receive` / `on_card_action` / `_dispatch` / `_run_claude`）

**核心脆弱点**：

1. **静默吞错**：`_dispatch` 通过 `create_task` 异步派发，未捕获异常被 asyncio 静默吞掉，用户端无任何反馈
2. **命令前缀冲突**：`startswith` 匹配时短前缀吞长前缀（`/mode` 吃 `/model`）
3. **配置/状态错位**：用户实际 open_id 与 `ALLOWED_USERS` 不一致；`/mem` 用 `default_workspace` 而非 session workspace
4. **同步异步混用**：`on_card_action` 是 SDK 要求的 sync 函数，内部不能 `await`，否则 SyntaxError
5. **审计时序**：只在任务结束时记日志，运行中任务在 audit.log 不可见

**排查入口**：
- 用户发消息无响应 → grep `_dispatch` 是否有 try/except；查 stderr
- `/model opus` 返回审批帮助 → startswith 前缀冲突
- 权限被拒 "not authorized" → 实际 open_id 与 ALLOWED_USERS 对比
- `await outside async function` SyntaxError → on_card_action 调了 async 函数

---

## 2.1 _dispatch() 缺全局异常捕获导致静默失败

**现象**：用户发消息后完全无响应，服务日志也无错误输出。

**原因**：`_dispatch()` 通过 `asyncio.create_task()` 调度，内部异常被 asyncio 静默吞掉。如果分类或 Agent Loop 抛出未捕获的异常，用户看不到任何反馈。

**解决方案**：在 `_dispatch()` 的核心逻辑外包裹 try/except，将错误信息通过飞书消息反馈给用户：

```python
async def _dispatch(open_id, chat_id, message_id, text):
    try:
        difficulty, model = await classify_with_deepseek(prompt)
        # ... 审批 + AgentLoop ...
    except Exception as e:
        logger.exception("Dispatch error for user %s", open_id)
        try:
            await feishu_client.send_text(open_id, f"处理指令时出错：{type(e).__name__}: {e}")
        except Exception:
            pass
```

**关键点**：所有通过 `create_task` 调度的异步函数都应有顶层异常处理，否则错误会被静默吞掉。

---

## 2.2 /model 被 /mode 的 startswith 抢先匹配

**现象**：输入 `/model opus` 返回的是审批模式帮助信息，而非模型切换。

**原因**：`_dispatch()` 中 `/mode` 用 `text_lower.startswith("/mode")` 匹配。由于 `/model` 以 `/mode` 开头，`/model opus` 被 `/mode` 分支拦截。`"opus"` 不在 `("h", "m", "l")` 中，走进了显示帮助的子分支。

**解决方案**：将 `/model` 的匹配放在 `/mode` **前面**：

```python
# --- /model ---  (must be before /mode to avoid prefix clash)
if text_lower.startswith("/model"):
    ...
    return

# --- /mode ---
if text_lower.startswith("/mode"):
    ...
    return
```

**关键点**：使用 `startswith` 做命令匹配时，较长的命令前缀必须排在前面，否则会被短前缀吞掉。

---

## 2.3 飞书用户 ID 与 ALLOWED_USERS 不匹配

**现象**：用户发消息收到 "Sorry, you are not authorized to use this bot."

**原因**：`.env` 中的 `ALLOWED_USERS` 配的是 `ou_421c22...`，但实际发消息的用户 open_id 是 `ou_cae433...`。

**排查**：在 `_is_user_allowed` 函数中加日志打印实际的 `user` 和 `allowed` 列表，对比发现不一致。

**解决方案**：将实际用户的 open_id 加入 `ALLOWED_USERS`。

**关键点**：飞书用户的 open_id 不是固定的，它和应用的 app_id 绑定。换一个飞书应用，同一个用户的 open_id 会变。获取用户 open_id 的方法：
1. 在代码中加日志，让用户发一条消息即可看到
2. 或通过飞书管理后台的用户管理查看

---

## 2.4 中文命令的风险分类失效

**现象**：发送"删除 main.py"、"修改 config.py"等中文命令被分类为低风险，直接执行而不走审批。

**原因**：风险分类的正则只匹配英文关键词（`delete`、`edit`、`modify`），不匹配中文。

**解决方案**：在 `HIGH_RISK_PATTERNS` 中添加中文关键词正则：

```python
HIGH_RISK_PATTERNS = [
    r"(修改|新建|创建|写入|编辑|删除|移除|重命名)",
    r"(推送|提交|部署)",
    # ... 英文关键词 ...
]
```

**关键点**：中文正则不需要 `\b`（单词边界），因为 `\b` 对 Unicode 字符无效。

---

## 2.5 pydantic-settings 解析 list[str] 失败 / .env 旧字段校验失败

**现象 A**：启动报 `SettingsError: error parsing value for field "allowed_users"`

**原因 A**：`pydantic-settings` 读 `.env` 时对 `list[str]` 字段用 `json.loads()` 解析。直接写 `ALLOWED_USERS=ou_aaa,ou_bbb` 不是合法 JSON。

**解决 A**：字段类型改为 `str`，代码中手动拆分：

```python
allowed_users: str = ""  # 逗号分隔

def get_allowed_users(self) -> list[str]:
    return [u.strip() for u in self.allowed_users.split(",") if u.strip()]
```

---

**现象 B**：重构后（移除某些字段）服务启动报 `Extra inputs are not permitted`

**原因 B**：`.env` 中仍包含旧配置项，pydantic-settings 默认拒绝未知字段。

**解决 B**：在 `model_config` 中添加 `extra="ignore"`：

```python
model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
```

**关键点**：配置字段增删时，旧的 `.env` 文件可能残留过时字段。`extra="ignore"` 让迁移更平滑。

---

## 2.6 /mem 写入路径用了 default_workspace 而非 session 工作区

**现象**：用户执行 `/mem CSMAR弹窗需要先关闭`，但文件出现在 `D:\projects`（default_workspace），而不是用户当前工作区 `D:\ForRunning\ForDev\CSMAR`。

**原因**：`/mem` 实现中用了 `settings.default_workspace` 而非 session 的 workspace：

```python
# 错误 — 用了固定配置路径
agent_md = Path(settings.default_workspace) / "AGENT.md"

# 正确 — 用 session workspace
session = session_manager.get_user_session(open_id)
workspace = session.workspace if session else settings.default_workspace
```

**关键点**：所有涉及文件操作的命令都应该使用 `session.workspace`，而非 `settings.default_workspace`。用户的实际工作区可能已经通过 `/pwd` 切换到了其他路径。

---

## 2.7 audit.log 不记录正在运行的任务

**现象**：用户发消息后，飞书端能看到流式卡片正常更新，但 `tail audit.log` 看不到任何新条目。诊断时误判为"WS 连接断了"。

**原因**：`log_command_received` 和 `log_agent_result` 都在 `_run_claude` 的**最末尾**调用——任务完成后才记录。任务还在运行时 audit.log 里什么都不会出现。

**解决方案**：在 `_run_claude` **开头**加一条 `started` 日志：

```python
async def _run_claude(...):
    audit_logger.log_command_received(open_id, prompt, "started")  # ← 立即记录
    try:
        ...
```

audit.log 现在显示：
- `started` — 消息已收到，开始处理
- `completed` / `failed` — 任务结束（原有逻辑）

**关键点**：审计日志应该在任务生命周期的关键节点都记录（开始、结束），否则长时间运行的任务在日志中完全不可见。

---

## 2.8 await 出现在 sync 函数中导致 SyntaxError

**现象**：启动直接报错，服务无法启动：

```
SyntaxError: 'await' outside async function
```

**原因链**：
1. `cancel_by_user` 被改成 `async def`（等待 reader task 完成清理）
2. `on_card_action` 是同步函数（飞书 SDK 的事件分发器以同步方式调用它）
3. profile switch 卡片回调分支调了 `await cancel_by_user()` → SyntaxError

**解决方案**：拆为两个版本：

```python
# cli_loop.py
def cancel_by_user(self, open_id: str) -> bool:
    """同步版 — 给 sync 回调函数用"""
    ...

async def cancel_and_wait(self, open_id: str) -> bool:
    """异步版 — 给 async 函数用，等待 reader cleanup"""
    cancelled = self.cancel_by_user(open_id)
    await asyncio.sleep(0.3)
    return cancelled
```

调用方适配：

```python
# on_card_action (sync) → 用 cancel_by_user
claude_cli_loop.cancel_by_user(open_id)

# _dispatch (async) → 用 cancel_and_wait
await claude_cli_loop.cancel_and_wait(open_id)
```

**关键点**：如果一个方法被 sync 函数和 async 函数共用，必须提供两个版本。SDK 要求的回调签名不能改。

---

## 2.9 连续对话丢失上下文 / 切换审批模式后无法继续

**现象**：用户发"继续"，GLM 回复"我没有上下文"，每次消息都要重新分类和审批。

**原因**：
1. 每条消息都走完整分类→审批→执行流程，没区分"首次消息"和"后续对话"
2. 没有强制继续的指令

**解决方案**：

```python
has_history = bool(session.agent_messages) and bool(session.last_model)
if has_history:
    model = session.last_model  # 直接继续
else:
    # 首次消息：分类 + 模型选择审批
    ...
```

并添加 `/continue` 命令强制跳过审批继续会话。

---

## 2.10 模型选择确认后无状态反馈

**现象**：用户点击"确认执行"后，没有任何反馈。等了很久才出现工具审批卡片，用户不确定是否生效。

**原因**：模型审批通过后直接进入 `agent_loop.run()`，没有发中间状态消息。Agent Loop 启动需要时间，用户在空白期不知道发生了什么。

**解决方案**：在 Agent Loop 启动前发送状态消息：

```python
await feishu_client.send_text(open_id, f"[{model}] 开始执行，请等待...")
```

**关键点**：每个异步等待点之前都应该给用户反馈，避免"黑盒等待"。

---

## 2.11 /new 后工作区跳回 default（已反转）

**现象**：用户用 `/pwd` 切到某个项目目录工作，发 `/new` 想开新会话——结果 workspace 跳回 `DEFAULT_WORKSPACE`，新会话跑在错的目录里。

**原因（旧设计）**：`reset_user_session()` 不传 workspace，回落到 `settings.default_workspace`。早先认为"`/new` 是全新开始，不该继承旧状态"，但实际用法上 `/new` 是"同一项目开新会话"，跳回 default 反而不符合预期。

**修复**：`reset_user_session()` 继承旧 session 的 workspace，仅在没有旧 session 时回落到 default：

```python
workspace = old.workspace if old else None
session = self.create_session(open_id, chat_id, workspace=workspace)
```

**关键点**：`/new` 语义 = 同一项目里开新 claude 会话；想换 workspace 用 `/pwd`。修改 `.env` 的 `DEFAULT_WORKSPACE` 后想生效，重启 openclaw 即可（首次 session 会用新 default）。

---

## 2.12 服务重启后 /continue 找不到之前的 Claude session

**现象**：服务重启后发 `/continue` 提示"当前会话没有 Claude session"。

**原因**：`SessionManager` 的 session 数据完全存储在**内存**中，服务重启后全部丢失。

**解决方案**：将 session 持久化到文件系统：

```python
class SessionManager:
    def __init__(self):
        self._state_dir = Path(".sessions")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()  # 启动时加载

    def _save(self, session: Session) -> None:
        path = self._state_dir / f"{session.user_open_id}.json"
        path.write_text(session.model_dump_json(indent=2), "utf-8")
```

持久化触发时机：任务完成后、`/model`/`/mode`/`/resume`/`/pwd` 命令执行后、`create_session` 创建时。

**关键点**：内存状态 + 文件持久化是经典 hybrid 方案。

---

# 三、子进程管理层（cli_loop.py）

**位置**：`app/agent/cli_loop.py`（`ClaudeCLILoop` / `_start_process` / `send_and_wait` / `_read_loop`）

**核心脆弱点**：

1. **Windows 可执行文件后缀**：`create_subprocess_exec` 不走 shell，不会自动补 `.cmd`/`.bat`，必须用 `shutil.which` 解析
2. **stdin EOF + 单次进程模型**：`-p --input-format stream-json` 模式下，stdin close 就是"本条消息结束"的信号，每条消息一个进程，不能复用 writer
3. **缓冲区溢出**：默认 64KB 单行限制，`tool_result` 可能含整文件内容
4. **环境变量泄漏**：父进程的 `CLAUDECODE` 会让子进程拒绝启动（嵌套检测）
5. **Windows git-bash 依赖**：CLI 在 Windows 上必须有 git-bash 才能跑 shell 命令
6. **进程拆除竞态**：旧 reader task 的 `_cleanup` 按 open_id 索引清状态，与新 `_start_process` 并发会清错

**排查入口**：
- `FileNotFoundError: [WinError 2]` → `shutil.which` 没找到 claude.CMD
- CLI 1.5s 内退出 + `text_len=0` → 看 stderr，通常是 CLAUDECODE/git-bash/session 坏
- `Separator not found, chunk exceed the limit` → subprocess limit 不够
- 第二条消息 `ConnectionResetError` → 进程已退出但代码假设长驻，看 `_consumed`/`_teardown_previous`

---

## 3.1 Claude CLI 在 WSL/Windows 找不到（FileNotFoundError）

**现象**：`Claude CLI not found at: claude` 或 `FileNotFoundError: [WinError 2] 系统找不到指定的文件`

**原因**：`asyncio.create_subprocess_exec` 在子进程环境中查找可执行文件，`claude` 不在 PATH 中，或 Windows 上找不到 `claude.cmd`（CreateProcess 不会自动补 `.cmd` 后缀）。

**早期方案**（WSL → Windows）：
- `which claude` 找到符号链接，但 `create_subprocess_exec` 无法执行 `.js` 文件
- 改用 `powershell.exe` + `Set-Location` 设置工作目录

**最终方案**（当前架构）：用 `shutil.which()` 解析可执行文件，**自动找到 `claude.CMD`**：

```python
resolved = shutil.which(cli_path)  # claude → C:\Users\...\npm\claude.CMD
if not resolved:
    raise FileNotFoundError(
        f"Claude CLI not found: {cli_path!r}. "
        f"Install it (`npm i -g @anthropic-ai/claude-code`) or set "
        f"CLAUDE_CLI_PATH to an absolute path."
    )
args = [resolved, "--print", ...]
```

**关键点**：Windows 上 npm shim 是 `claude.CMD`，CreateProcess 不会自动补 `.cmd` 后缀（只补 `.exe`），必须用 `shutil.which` 显式解析。

---

## 3.2 Claude CLI 嵌套检测拒绝启动（CLAUDECODE 环境变量）

**现象**：Claude CLI 启动后 2 秒内结束，stdout 无任何输出（`text_len=0 tools=0`）。飞书卡片停在"思考中..."不更新。

**原因**：Claude Code CLI 检测到 `CLAUDECODE` 环境变量（由父 Claude Code 进程设置），拒绝在嵌套模式下启动：

```
Error: Claude Code cannot be launched inside another Claude Code session.
To bypass this check, unset the CLAUDECODE environment variable.
```

**解决方案**：在 `_build_env` 中创建子进程前移除该变量：

```python
def _build_env(workspace: str) -> dict:
    env = os.environ.copy()
    for key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]:
        env.pop(key, None)
    ...
```

**关键点**：当 OpenClaw 本身运行在 Claude Code 终端内时（如开发调试），必须清除这个变量。

---

## 3.3 Windows 找不到 git-bash

**现象**：CLI 立即退出，`text_len=0 tools=0`。stderr 显示：

```
Claude Code on Windows requires git-bash (https://git-scm.com/downloads/win).
If installed but not in PATH, set CLAUDE_CODE_GIT_BASH_PATH=C:\Program Files\Git\bin\bash.exe
```

**原因**：Claude CLI 在 Windows 上需要 git-bash 来执行 shell 命令。

**解决方案**：在 `_build_env` 中自动检测并设置 git-bash 路径：

```python
if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
    for candidate in [
        r"D:\Git\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Git\bin\bash.exe",
    ]:
        if os.path.isfile(candidate):
            env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
            break
```

**关键点**：这个问题只在 Windows 上出现。Linux/Mac 不需要此配置。

---

## 3.4 CLI 2.1.71 升级后 stdin 不关闭导致所有请求 crash

**现象**：发任何消息，CLI 进程在 1.5 秒内退出（`code=1`），`text_len=0 tools=0`。`~/.claude/debug/` 显示初始化后直接 `SessionEnd`，没有任何 `API:request` 记录。

**根因链（3 个问题叠加）：**

### 问题 1：asyncio subprocess stdin 不关闭导致 CLI 不处理

Claude CLI 在 `--print --input-format stream-json` 模式下通过 asyncio subprocess 启动时，stdin pipe 保持打开。CLI 2.1.71 在等更多输入（或 EOF），永远不会处理已写入的消息。

```
# PowerShell (工作)              asyncio subprocess (不工作)
echo → pipe → EOF → CLI          write() → drain() → pipe OPEN → CLI 永远等
```

**修复**：`send_and_wait` 写完 stdin 后立即 `writer.close()` 发出 EOF：

```python
writer.write(payload.encode())
await writer.drain()
writer.close()  # ← 关键：让 CLI 知道输入已完整
```

### 问题 2：`subtype == "error"` 遗漏 `error_during_execution`

CLI 崩溃时的 result 事件 subtype 是 `error_during_execution`，但代码只匹配精确的 `"error"`。导致崩溃被标记为 `status=completed`，用户收到空白完成消息。

**修复**：用 `startswith` 匹配所有 error 变体：

```python
status = "failed" if subtype.startswith("error") else "completed"
```

### 问题 3：失败 session 的 session_id 被保存，导致下次 --resume 坏 session

CLI 崩溃时 result 事件仍包含 `session_id`。`events.py` 无条件保存这个 ID 到 `session.claude_session_id`。下次消息启动 CLI 时带上 `--resume <broken_session_id>`，CLI 尝试恢复从未成功初始化的 session，立即再次崩溃。形成死循环。

**修复**：失败时不保存 session_id：

```python
if agent_result.status == "failed":
    session.claude_session_id = ""
elif agent_result.session_id:
    session.claude_session_id = agent_result.session_id
```

**排查过程**：
1. 直接 curl DeepSeek API → 200 OK，排除 API key/网络问题
2. PowerShell 中 `echo | claude --print ...` → 正常输出，排除 CLI 本体问题
3. 查看 `~/.claude/debug/` 日志 → 发现初始化后直接 `SessionEnd`
4. 添加诊断日志打印 subprocess env 和事件流 → 唯一 stdout 事件是 `result sub=error_during_execution`
5. 清除 session 缓存后发消息 → 立即正常工作

---

## 3.5 连续对话报 ConnectionResetError（#3.4 修复的副作用）

**现象**：单条消息正常回复，但用户连续发**第二条**消息时报：

```
ConnectionResetError: Connection lost
```

飞书端：流式卡片建好（POST 卡片 200 OK）后毫秒级立刻报错。

**原因**：#3.4 为了让 CLI 2.1.71 开始处理消息，在 `send_and_wait` 写完 stdin 后加了 `writer.close()`。这等于把每条消息变成"一次性"——stdin EOF 触发处理，进程随后退出。但代码仍按"进程长驻可复用"的旧假设工作：

1. `is_running()` 只看 `proc.returncode is None`。进程在 stdin 关闭后到完全退出前的 draining 期间，returncode 仍是 None → `is_running()` 返回 True
2. 第二条消息进来时复用了**已关闭的 writer**
3. `writer.write()` 可能成功（缓冲写入），但 `writer.drain()` 检测到底层 transport 已 `connection_lost`，抛 `ConnectionResetError`

**解决方案**：显式跟踪"已消费"进程，启动新进程前彻底拆除旧的：

```python
# 1. 新增 _consumed: set[str]，标记 stdin 已关闭的用户
self._consumed: set[str] = set()

# 2. is_running() 对 _consumed 中的用户返回 False
def is_running(self, open_id: str) -> bool:
    if open_id in self._consumed:
        return False
    proc = self._processes.get(open_id)
    return proc is not None and proc.returncode is None

# 3. send_and_wait 检测到不可复用时调 _teardown_previous
async def _teardown_previous(self, open_id: str) -> None:
    """旧 reader 的 _cleanup 按 open_id 索引清状态，若与新 _start_process
    并发会把新进程的状态清掉。必须先 await 旧 reader 完全退出再起新进程。"""
    old_task = self._reader_tasks.pop(open_id, None)
    old_proc = self._processes.pop(open_id, None)
    self._stdin_writers.pop(open_id, None)
    self._consumed.discard(open_id)

    if old_proc is not None and old_proc.returncode is None:
        try:
            old_proc.terminate()
        except ProcessLookupError:
            pass

    if old_task is not None and not old_task.done():
        try:
            await asyncio.wait_for(old_task, timeout=3.0)
        except asyncio.TimeoutError:
            if old_proc is not None and old_proc.returncode is None:
                try:
                    old_proc.kill()
                except ProcessLookupError:
                    pass
            old_task.cancel()
```

**关键点**：
- `--print --input-format stream-json` 模式下，stdin EOF 就是"本条消息结束"的信号，每条消息一个进程是固有行为，不是 bug——bug 是没为这个过渡做清理
- 拆除旧进程时**必须** `await` 旧 reader task 完全退出，否则它的 `_cleanup` 会按 open_id 清掉新进程的状态（竞态）
- `is_running()` 的语义应是"进程还能接受新输入吗"，不只是"还活着吗"
- 副作用：每条消息有 ~1-2s 的进程启动开销，这是 `-p stream-json` 模式的固有成本

---

## 3.6 Claude CLI 的运行模式（避免误改架构）

**背景**：排查 #3.5 时曾考虑"去掉 `--print` 改用交互 REPL 模式做长驻进程"。查证官方文档后确认此路不通。

**事实**：
- 官方 CLI 文档对 `--input-format` 的原文描述是："Specify input format for **print mode**"。`--input-format stream-json` **必须**配 `-p`/`--print` + `--output-format stream-json`
- 不加 `-p` 的 `claude` 进 TUI（终端 UI），是给人敲键盘用的，**不接受程序化 stdin 喂 JSONL**
- 所以"交互 REPL + stream-json 多轮 stdin"这个组合在 CLI 层面**根本不存在**

**当前项目用的模式：**

```
claude -p --input-format stream-json --output-format stream-json --resume <sid>
```

- 每条消息 spawn 一个 `-p` 子进程
- stdin 写一条 user 消息 → close → CLI 处理 → 输出 → 退出
- 多轮上下文靠 `--resume <sid>` / `--continue` 从 `~/.claude/projects/*/<sid>.jsonl` 恢复

**真正能做"进程长驻多轮"的三种途径：**

| 途径 | 机制 | 代价 |
|---|---|---|
| 每条消息 `-p` + `--resume`/`--continue` | 进程退出，jsonl 恢复历史 | **当前项目用的**；有 ~1-2s/条启动开销 |
| Claude Agent SDK（Python `ClaudeSDKClient` / TS `query()`） | 库嵌入进程，真长驻 | 要重写 `cli_loop.py` 整层 |
| `--bg` 后台会话 + `claude attach`/`logs`（v2.1.144+） | daemon socket 托管 | 新机制，集成成本高 |

**关键点**：不要再尝试"去掉 `-p` 改 stream-json 长驻"——CLI 不支持这个组合。若启动开销成了瓶颈，唯一干净的路是迁移到 Claude Agent SDK。

---

## 3.7 asyncio subprocess readline 缓冲区溢出

**现象**：Claude CLI 执行若干工具后突然中断，日志报 `ValueError: Separator is not found, and chunk exceed the limit`。

**原因**：`asyncio.create_subprocess_exec` 的 StreamReader 默认缓冲区限制为 **64KB**（`_DEFAULT_LIMIT = 65536`）。Claude CLI 的 JSONL 输出中，`tool_result` 事件可能包含完整文件内容（200KB 文件），单行超过 64KB 后 `readline()` 直接抛出 ValueError。

**解决方案**：将 `limit` 参数提升到 10MB：

```python
proc = await asyncio.create_subprocess_exec(
    *args,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=workspace,
    env=env,
    limit=10 * 1024 * 1024,  # 10MB — Claude JSONL lines can be huge
)
```

**关键点**：所有使用 `asyncio.subprocess` 读取外部程序输出的场景，都要考虑单行超长的可能性。Claude Code 的 JSONL 输出尤其容易出这个问题，因为 `tool_result` 会包含完整的文件内容或命令输出。

---

## 3.8 /stop 命令不生效（task_id vs open_id）

**现象**：执行 `/stop` 后返回"没有正在运行的任务"，但 CLI 进程明明在跑。

**原因**：`cli_loop.py` 中的 `task_id` 只在 `_run_inner` 里作为局部变量创建，从未设置到 `session.current_task_id`。`/stop` 靠 `session.current_task_id` 查找进程，永远为 `None`。

**解决方案**：不再依赖 `session.current_task_id`，改为按 `open_id` 查找进程：

```python
def cancel_by_user(self, open_id: str) -> bool:
    proc = self._processes.pop(open_id, None)
    ...
    if proc and proc.returncode is None:
        proc.terminate()
        return True
    return False
```

**关键点**：进程管理应该用直接的映射（open_id → process），而不是依赖 session 对象上的字段。

---

## 3.9 /switch 切换 provider 时连续两条"任务失败"

**现象**：用户用 `/switch` 切换 GLM/Kimi provider 后，飞书连续收到两条 "❌ 任务失败 | 原因: process terminated"。

**原因**：交互模式下 `send_and_wait` 的锁只保护 stdin 写入，不保护 `await future`。用户快速发了两条消息，`_response_futures[open_id]` 中会有**两个** pending future。`/switch` 卡片回调调用 `cancel_by_user` 时，两个 future 全部 resolve 为 `_error_result("", "process terminated", status="failed")`，两个 `_run_claude` 协程各自恢复，各自发送"任务失败"。

同时，主动取消用 `status="failed"` 是**语义错误**。

**解决方案**：

1. `_error_result` 增加 `status` 参数（默认 `"failed"`）
2. `cancel_by_user` 使用 `status="cancelled"`：

```python
def cancel_by_user(self, open_id: str) -> bool:
    ...
    for future, _ in entries:
        if not future.done():
            future.set_result(self._error_result("", "process terminated", "cancelled"))
```

3. `_run_claude` 对 cancelled 结果直接 return，不更新 session、不发通知：

```python
if agent_result.status == "cancelled":
    return  # 调用方（/switch, /stop, /new）已通知用户
```

**关键点**：主动取消和任务失败是不同语义。所有 `cancel_by_user` 触发的 future resolve 应该用 `cancelled` 而非 `failed`。

---

## 3.10 uvicorn 热重载中断正在执行的命令 / start.bat 用错 Python

**现象 A**：Claude 命令正在执行中，`watchfiles` 检测到文件变化触发 uvicorn 重载，导致进程被杀、命令中断。

**解决 A**：生产环境关闭热重载；开发模式用 `--dev` 显式开启，并排除非代码文件：

```python
if dev_mode:
    uvicorn.run(
        "app.main:app",
        reload=True,
        reload_includes=["*.py"],
        reload_excludes=["audit.log", ".env", "*.log"],
    )
else:
    uvicorn.run("app.main:app", reload=False)
```

---

**现象 B**：桌面快捷方式启动的服务用的是 `Anaconda\pytorch\python.exe`，不是项目的 `.venv\Scripts\python.exe`。

**原因 B**：`scripts/start.bat` 硬编码了 Anaconda 的 uv 路径。

**解决 B**：直接用项目的 `.venv` Python：

```bat
if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found. Run: uv sync
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m app.main
```

**关键点**：桌面启动脚本应该用确定性的路径，不要依赖 `uv` 的 Python 解析逻辑。

---

# 四、Claude CLI 层

**位置**：CLI 子进程本身（npm 全局安装的 `claude` / `claude.CMD`）

**核心脆弱点**：

1. **session 与 workspace 绑定**：claude session 在创建时与 cwd 绑定，跨 workspace resume 必失败
2. **stderr 才是真错误**：CLI 启动失败的错误在 stderr 而非 stdout JSONL
3. **SSE 流无超时**：GLM 等第三方 API 的 SSE 流可能不发 `[DONE]`，无限等待卡死
4. **Windows 编码**：默认可能 GBK，但 claude 输出 UTF-8

**排查入口**：
- CLI 1.5s 退出 + 空白卡片 → 看 `claude stderr:` 前缀的日志
- 手动测试 → PowerShell `echo '你好' | claude -p --output-format stream-json`
- `~/.claude/debug/` → CLI 自己的 debug 日志
- `~/.claude/projects/<workspace-hash>/<sid>.jsonl` → 真实对话历史

---

## 4.1 CLI 无输出时飞书卡片空白（stderr 才是真错误）

**现象**：CLI 启动失败（如 `--resume` session 不存在、workspace 不匹配），飞书只显示一张空白的"思考中..."卡片，没有任何错误提示。

**原因**：`cli_loop.py` 在 CLI 进程退出后，如果没收到任何 JSONL 事件，`accumulated_text` 为空，卡片保持初始状态不更新。stderr 有错误信息但未被展示给用户。

**解决方案**：在进程退出后检查是否无输出，并将 stderr 内容显示到飞书卡片：

```python
await proc.wait()
exit_code = proc.returncode

if not accumulated_text and stderr_lines:
    err_detail = "\n".join(stderr_lines[:5])
    result.error = f"Claude CLI 无输出 (exit={exit_code}):\n{err_detail}"
    result.status = "failed"
    err_card = build_error_card("Claude CLI 启动失败", err_detail[:3500])
    await feishu_client.update_card(card_message_id, err_card)
```

同时用后台 asyncio task 收集 stderr：

```python
async def _drain_stderr():
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").rstrip()
        stderr_lines.append(decoded)
        logger.warning("claude stderr: %s", decoded)
```

**关键点**：CLI 启动失败的错误在 stderr 而非 stdout（JSONL 流）。必须读取并展示 stderr，否则用户完全无法排查问题。

---

## 4.2 跨 workspace 的 --resume / --continue crash

**现象**：用户在 workspace A 创建了 session，之后切到 workspace B（通过 `/pwd` 或修改 `.env`），下次消息 `--resume <old_session_id>` 时 CLI 立即 crash。

**原因**：Claude Code 的 session 与创建时的工作区目录（cwd）绑定。在新工作区找不到该 session。

**解决方案**（两处改动）：

1. `/pwd` 切换工作区时清空 `claude_session_id`：

```python
session.workspace = str(p.resolve())
session.claude_session_id = ""  # 工作区变了，旧 session 无法 resume
```

2. 任务失败时**清空**（不是跳过保存）`claude_session_id`：

```python
if agent_result.status == "failed":
    session.claude_session_id = ""
elif agent_result.session_id:
    session.claude_session_id = agent_result.session_id
```

**关键点**：
- Claude session 与 workspace 目录绑定，跨目录 resume 必然失败
- 失败时只跳过保存不够——旧的 session_id 已持久化到 `.sessions/` 文件，必须主动清空
- 工作区变更（`/pwd`）是清空 session_id 的另一个触发点

**临时排查**：在终端手动验证 `claude --resume <session_id> -p "你好" --output-format stream-json` 是否正常返回。

---

## 4.3 GLM SSE 流无超时导致 Agent Loop 永久卡死

**现象**：Agent 执行了几个工具后突然卡住，不再有任何输出，也不再弹审批卡片。等了一小时也没有响应。

**排查**：查看 audit.log，最后一个工具执行时间戳到下一条用户消息之间有超过 1 小时的空白。

**根因**：GLM API 的 SSE 流在发送完 tool_call 结果后，可能因为服务端异常没有发送 `finish_reason=stop` 或 `[DONE]` 信号。代码中 `resp.aiter_lines()` 没有逐行超时机制，会无限等待下一行数据。

**解决方案**：

1. 细化 httpx 超时配置：

```python
httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0, read=120.0, write=30.0, pool=30.0))
```

2. 处理流异常断开——如果 SSE 流没有正常结束，把已累积的文本当结果返回：

```python
if not tool_calls_pending:
    if not stream_done and accumulated_text:
        logger.warning("SSE stream ended without finish_reason")
    break
```

**关键点**：所有 SSE/流式 HTTP 响应都必须有超时保护。不能假设服务端一定会发送 `[DONE]` 或 `finish_reason`。

---

## 4.4 中文乱码（GBK vs UTF-8）

**现象**：Claude 返回的中文变成 `褰撳墠宸ヤ綔璺ㄦ褰」寰勬槸锛` 之类的乱码。

**原因**：executor 中错误地判断 Windows 输出用 GBK 编码解码，但 claude.exe 实际输出 UTF-8。

**解决方案**：统一用 UTF-8 解码：

```python
result.stdout = stdout.decode("utf-8", errors="replace")
```

---

## 4.5 DeepSeek API Key 格式问题导致 401

**现象**：DeepSeek 分类返回 401 Authorization Required。

**原因**：key 格式不正确（应为 `sk-xxx`）。

**影响**：不影响功能，分类器会自动 fallback 到关键词分类器。但用户无感知（参考 #6.5）。

---

# 五、飞书卡片层

**位置**：`app/feishu/cards.py`（模板）/ `app/feishu/client.py`（API 调用）/ SDK 响应类

**核心脆弱点**：

1. **SDK 响应类构造限制**：`P2CardActionTriggerResponse` / `CallBackToast` 只接受 dict（`d`）参数，不支持关键字参数；用关键字构造会 TypeError 被吞，飞书收到格式异常响应（`code 200671`）
2. **API 调用静默失败**：`send_card` 只打日志不抛异常，调用方 `await future` 永远等一个未送达的卡片
3. **内置权限拦截**：claude 有自己的 permission 系统，在 hook 之前；没配 `permissions.allow` 时 Write/Edit 默认拒绝，hook 不触发
4. **hook 配置位置**：必须在 CLI 工作目录（cwd）的 `.claude/settings.json` 才生效

**排查入口**：
- 点审批按钮报 `code 200671` → 响应类构造方式（不能用关键字参数）
- 卡片不出现但代码看似正常 → `send_card` 是否静默失败
- claude 说"需要授权"但 hook 没触发 → `~/.claude/settings.json` 缺 `permissions.allow`

---

## 5.1 SDK 响应类构造方式错误（code 200671）

**现象**：点击卡片审批按钮后飞书显示错误 200671，但审批逻辑实际已生效。

**原因**：`P2CardActionTriggerResponse` 和 `CallBackToast` 的 `__init__()` 不接受关键字参数，但实例有 `toast`/`card`/`type`/`content` 属性。直接传参会触发飞书 API 响应格式异常。

```python
# 错误写法 — TypeError 在 SDK 内部被吞掉，飞书收到格式异常的响应
return P2CardActionTriggerResponse(toast=toast)
CallBackToast(content="Approved")  # TypeError

# 正确写法 — 先创建空对象再赋值
resp = P2CardActionTriggerResponse()
toast = CallBackToast()
toast.type = "info"
toast.content = "已确认"
resp.toast = toast
return resp
```

**排查**：通过 `uv run python -c "P2CardActionTriggerResponse(toast=t)"` 复现，确认 `TypeError: __init__() got an unexpected keyword argument 'toast'`。

**关键点**：所有 SDK 生成的响应类都不能用关键字参数构造，必须先创建再赋值。

---

## 5.2 feishu_client.send_card 静默吞掉错误

**现象**：工具审批卡片永远不会出现，也没有任何错误提示。hook handler 永远挂起等待用户点击一个从未送达的卡片。

**原因**：`feishu/client.py` 的 `send_card` 方法在 API 返回错误时只打日志不抛异常：

```python
# 修复前 — 静默吞错
data = resp.json()
if data.get("code") != 0:
    logger.error("send_card failed: %s", data)
return data  # 不管成功失败都返回，调用方以为成功了
```

调用方 `request_tool_approval` 认为 `send_card` 成功（没抛异常），就一直 `await future` 等用户点击——但卡片根本没发出去。

**解决方案**：加 token 刷新重试 + 抛异常：

```python
data = resp.json()
if data.get("code") != 0:
    logger.error("send_card failed: %s", data)
    if data.get("code") == 99991663:  # token 过期
        await self._refresh_token()
        return await self.send_card(receive_id, card, is_chat=is_chat)
    raise RuntimeError(f"send_card failed: code={data.get('code')} msg={data.get('msg')}")
```

**关键点**：所有飞书 API 调用都应该在失败时抛异常，否则调用方无法知道操作是否成功。这是工具审批卡片不出现的直接原因之一。

---

## 5.3 Claude Code 内置权限系统拒绝工具，PreToolUse hook 不触发

**现象**：工具审批卡片永远不出现。hook_log 显示 Claude CLI 的 Write 调用根本没记录，但 Claude 回复"需要你授权写入权限"。

**原因**：Claude Code 有自己的内置权限系统，在 PreToolUse hook **之前**运行。如果内置权限拒绝，hook 根本不会触发。

用户级配置 `~/.claude/settings.json` 中缺少 `permissions` 配置，Claude Code 的 `-p` 模式对 Write/Edit/Bash 等工具默认需要交互授权。非交互模式下无人响应，直接拒绝。

```json
// 修复前 — 没有 permissions
{
  "env": { ... },
  "model": "opus[1m]",
  "skipDangerousModePermissionPrompt": true
}

// 修复后 — 添加 permissions 允许所有工具
{
  "env": { ... },
  "permissions": {
    "allow": ["Bash(*)", "Write(*)", "Edit(*)", "NotebookEdit(*)", "Read(*)", "Glob(*)", "Grep(*)", "WebSearch", "WebFetch"],
    "deny": []
  }
}
```

**关键点**：
- Claude Code 的权限层级：**内置权限 → PreToolUse hook → 工具执行**
- 内置权限在 hook 之前，如果拒绝了，hook 没机会触发
- 需要在 `~/.claude/settings.json` 中配置 `permissions.allow` 让工具自动放行，审批逻辑交给 hook
- `skipDangerousModePermissionPrompt` 不等于允许工具自动执行

---

## 5.4 PreToolUse Hook 不触发（hook 配置在错误目录）

**现象**：Claude CLI 正常运行，工具被执行，但没有任何审批卡片弹出，所有工具都被自动放行。服务日志显示 `No session registry for xxx, allowing`。

**原因**：`.claude/settings.json`（Hook 配置文件）只在 openclaw 项目目录下存在，但用户通过飞书启动的 Claude CLI 工作目录是 `DEFAULT_WORKSPACE`，该目录下没有 Hook 配置。

```
openclaw/.claude/settings.json  ← Hook 配置在这里
用户工作区/.claude/             ← 这里没有 settings.json，hook 不触发！
```

**解决方案**：在 `cli_loop.py` 启动子进程前，自动将 Hook 配置注入到工作区（**当前架构进一步改为 `settings.local.json`，避免覆盖用户 settings.json**）：

```python
async def _ensure_hook_config(workspace: str) -> None:
    claude_dir = Path(workspace) / ".claude"
    settings_file = claude_dir / "settings.local.json"  # 用 local 覆盖

    # 合并而非覆盖：只在不存在等价 hook 时追加
    pre_use = existing.get("hooks", {}).get("PreToolUse", [])
    already = any(...)
    if not already:
        pre_use.append(openclaw_hook_entry)
        ...
```

**关键点**：
- Hook 配置必须存在于 Claude CLI 的工作目录（cwd）中
- 不同用户工作区都需要注入
- hook 脚本路径要写成绝对路径
- 用 `settings.local.json` 而非 `settings.json`，避免覆盖用户已有配置

---

# 六、Hook 审批层

**位置**：`app/hooks/router.py`（HTTP 端点）/ `app/approval/manager.py`（Future 状态机）/ `scripts/hooks/pre_tool_use.py`（CLI 端脚本）

**核心脆弱点**：

1. **跨进程 HTTP 往返**：claude → hook 脚本 → HTTP POST → FastAPI → 飞书卡片 → 用户点击 → HTTP 响应 → hook 脚本 stdout → claude；任一环挂掉整链卡死
2. **Future 必须超时兜底**：审批卡片未送达 / 用户不点 → Future 永久挂起 → 服务无法退出
3. **分支语义不一致**：审批通过/拒绝/跳过三分支，工具 record 必须在统一位置创建，否则 IndexError
4. **类型跨界不一致**：router 传字符串 `"high"`，manager 调 `.value`（以为枚举）→ AttributeError
5. **错误必须用户可见**：外部 API 错误只写日志用户无感

**排查入口**：
- 审批后 IndexError → `tools_used` 在所有分支都创建 record
- 卡片发送前抛异常 → risk_level 类型不匹配
- 审批卡片不出现 → 三步：① `permissions.allow` ② `send_card` 抛错 ③ hook_log 有记录
- 服务无法 Ctrl+C → Future 没超时兜底

---

## 6.1 Agent Loop 审批通过后 IndexError

**现象**：工具执行审批点"允许执行"后流式输出中断，日志报 `IndexError: list index out of range`。

**原因**：工具审批逻辑的 `if needs_approval` 和 `else` 分支结构有误——`ToolCallRecord` 只在 `else`（跳过审批）分支创建，审批通过分支没有创建 record。执行工具后用 `result.tools_used[-1]` 取最后一条 record 时列表为空。

```python
# 修复前（错误）
if needs_approval:
    approved = await approval_manager.request_tool_approval(...)
    if not approved:
        result.tools_used.append(ToolCallRecord(...))  # 只在拒绝时添加
        break
    # ← 审批通过后没有添加 record！
else:
    result.tools_used.append(ToolCallRecord(...))  # 只在跳过审批时添加

tool_output = await execute_tool(...)
record = result.tools_used[-1]  # ← IndexError! 列表为空

# 修复后（正确）
if needs_approval:
    approved = await approval_manager.request_tool_approval(...)
    if not approved:
        result.tools_used.append(ToolCallRecord(..., approved=False))
        break
else:
    pass  # 无需审批，继续往下执行

# 统一在这里创建 record
record = ToolCallRecord(..., approved=True)
result.tools_used.append(record)
```

**关键点**：审批通过和跳过审批应走同一个"创建 record → 执行工具"路径，避免分支逻辑不一致。

---

## 6.2 risk_level 类型不匹配导致 AttributeError

**现象**：工具审批卡片发送失败，错误 `AttributeError: 'str' object has no attribute 'value'`。

**原因**：`hooks/router.py` 传 `risk_level="high"`（字符串），但 `approval/manager.py` 调了 `risk_level.value`（以为它是枚举）：

```python
# hooks/router.py
approved = await approval_manager.request_tool_approval(
    risk_level="high" if tool_name in HIGH_RISK_TOOLS else "low",  # 字符串
)

# manager.py
audit_logger.log_tool_approval_requested(risk_level=risk_level.value)  # 💥 'str' has no .value
```

这个异常在 `send_card_fn` 调用**之前**就抛出了，所以卡片根本没机会发。

**解决方案**：兼容字符串和枚举两种类型：

```python
risk_level=risk_level.value if hasattr(risk_level, "value") else str(risk_level)
```

**关键点**：跨模块传参时类型要一致。用 `hasattr` 检查比强制类型转换更安全。

---

## 6.3 run_command 风险分类过于粗粒度

**现象**：`ls -la`、`find . -name "*.py"`、`git status` 等只读命令也要逐个审批，体验很差。

**原因**：初始实现把 `run_command` 工具整体标记为 HIGH 风险，没有分析具体命令内容。`ls` 和 `rm -rf` 走同一个审批流程。

**解决方案**：对 `run_command` 实现命令级风险分析：

```python
def classify_tool_risk(name: str, arguments: dict | None = None) -> RiskLevel:
    if name in _HIGH_RISK_TOOLS:
        return RiskLevel.HIGH

    if name == "run_command":
        cmd = arguments.get("command", "").strip()
        cmd_first = cmd.split(";")[0].split("&&")[0].split("|")[0].strip()

        for kw in _HIGH_RISK_COMMAND_KEYWORDS:
            if kw in cmd_lower:
                return RiskLevel.HIGH

        if cmd_lower in _SAFE_COMMAND_EXACT or cmd_lower.startswith(...):
            return RiskLevel.LOW

        return RiskLevel.HIGH  # 未知命令默认 HIGH
```

分类逻辑：
- **LOW（免审批）**：`ls`, `find`, `cat`, `grep`, `git status`, `pip list`, `python --version` 等
- **HIGH（需审批）**：`rm`, `pip install`, `npm install`, `git push`, `chmod` 等
- **未知命令 → HIGH**（安全优先）

**关键点**：`classify_tool_risk()` 签名需要接收 `arguments` 参数。

---

## 6.4 DeepSeek API 错误被静默容错，用户无感知

**现象**：DeepSeek API 调用失败后，自动降级到信号分类器，用户完全不知道出了问题。

**原因**：`classifier.py` 的 `except Exception` 只写了 `logger.warning`，没有通知用户。

**解决方案**：`classify_prompt` 返回值新增 `error_message` 字段，`events.py` 调用后检查并通知用户：

```python
difficulty, model, used_deepseek, ds_error = await classify_prompt(prompt)
if ds_error:
    await feishu_client.send_text(open_id, f"⚠️ {ds_error}\n已自动回退到信号分类器。")
```

**关键点**：外部 API 调用的错误应该通过用户可感知的渠道（飞书消息）报告，不能只写日志。

---

## 6.5 任务完成后进程立即退出，用户不知道可以继续

**现象**：Claude CLI 完成任务后显示"CLI 进程已退出 (code=0)"和冷冰冰的统计数据，用户以为崩了。

**原因**：
1. 正常完成时发送了"CLI 进程已退出 (code=0)"——看起来像崩溃
2. 完成后没有提示用户可以继续
3. `claude -p` 是单次执行模式，完成即退出是正常行为

**解决方案**：
1. 删除正常完成时的"CLI 进程已退出"消息，仅在异常退出时显示
2. 完成后发送明确的提示：

```python
await feishu_client.send_text(
    open_id,
    f"✅ 任务完成 | 耗时: {duration:.1f}s | ...\n\n"
    f"直接发消息即可继续对话，或用 `/continue` 恢复上次会话。",
)
```

**关键点**：`claude -p` 是单次执行模式，进程退出是正常的。需要在用户端明确说明"可以继续"。

---

# 快速排查清单

## 一、飞书 WebSocket 层

| 问题 | 检查方式 |
|------|----------|
| WS 连不上 | 检查 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 是否正确 |
| WS 连上但收不到消息 | 确认 `_connect()` 和 `_receive_message_loop()` 使用 `asyncio.get_running_loop()` |
| 审批按钮报错 200340 | 子类化覆盖 `_handle_data_frame` 处理 CARD；后台开 `card.action.trigger` 长连接 |
| 服务无法 Ctrl+C 停止 | `taskkill /F /PID`；所有 Future 必须有超时兜底 |

## 二、事件路由层

| 问题 | 检查方式 |
|------|----------|
| 发消息无响应 | `_dispatch` 是否有 try/except 全局异常捕获 |
| `/model opus` 返回审批帮助 | `/model` 必须排在 `/mode` 前面匹配 |
| 权限被拒 | 实际 open_id 与 `.env` 的 `ALLOWED_USERS` 对比 |
| 中文命令绕过审批 | `HIGH_RISK_PATTERNS` 加中文正则（不需 `\b`） |
| `/mem` 写错位置 | 用 `session.workspace` 而非 `default_workspace` |
| audit.log 看不到运行中任务 | `_run_claude` 开头加 `log_command_received(..., "started")` |
| 启动报 `await outside async function` | `on_card_action` 是 sync，拆 `cancel_by_user`(sync) + `cancel_and_wait`(async) |
| 配置迁移后启动报错 | `model_config` 添加 `extra="ignore"` |
| `/new` 后工作区跳回 default | `reset_user_session` 要继承旧 workspace（仅首次回落 default） |

## 三、子进程管理层

| 问题 | 检查方式 |
|------|----------|
| `FileNotFoundError: [WinError 2]` | `shutil.which("claude")` 解析；Windows 上找 `claude.CMD` |
| CLI 1.5s 退出 + 空白卡片 | 看 `claude stderr:`；通常是 CLAUDECODE/git-bash/session 坏 |
| 第二条消息 `ConnectionResetError` | `_consumed` 跟踪 + `_teardown_previous` 拆旧 reader 再起新 |
| `Separator/chunk exceed the limit` | `asyncio.subprocess` 加 `limit=10*1024*1024` |
| `/stop` 不生效 | 用 `cancel_by_user(open_id)`，不依赖 `session.current_task_id` |
| `/switch` 双重"任务失败" | `cancel_by_user` 用 `status="cancelled"`；`_run_claude` cancelled 静默 return |
| 命令被中断 | 关闭 uvicorn `reload=True`，开发用 `--dev` |
| 想去掉 `-p` 改长驻进程 | CLI 不支持；要长驻只能迁移到 Claude Agent SDK |

## 四、Claude CLI 层

| 问题 | 检查方式 |
|------|----------|
| CLI 无输出卡片空白 | 读 stderr 并展示（#4.1），通常是 CLAUDECODE/git-bash/session 坏 |
| `--resume` 后没恢复上下文 | workspace 必须与原 session 一致；手动 `claude --resume <sid> -p "你好"` 验证 |
| Agent 卡死不响应 | SSE 流加 read timeout（120s），处理无 `finish_reason` 的断开 |
| 中文乱码 | 统一 UTF-8 解码（`decode("utf-8", errors="replace")`） |
| DeepSeek 401 | key 格式 `sk-xxx`，自动降级到信号分类器 |

## 五、飞书卡片层

| 问题 | 检查方式 |
|------|----------|
| 卡片回调报 200671 | `P2CardActionTriggerResponse` / `CallBackToast` 不能用关键字构造，先创建再赋值 |
| 卡片不出现 | `send_card` 失败时必须抛异常，不能只打日志 |
| claude 说"需要授权"但 hook 没触发 | `~/.claude/settings.json` 加 `permissions.allow`（内置权限先于 hook） |
| hook 不触发 | hook 配置必须在 CLI 工作目录的 `.claude/settings.local.json` |

## 六、Hook 审批层

| 问题 | 检查方式 |
|------|----------|
| 审批后 `IndexError` | `tools_used` 在所有分支（通过/拒绝/跳过）都创建 record |
| 卡片发送前 `AttributeError: 'str' has no attribute 'value'` | router 传字符串时 manager 用 `hasattr(risk_level, "value")` 兼容 |
| 只读命令也要审批 | `run_command` 命令级风险分析，未知默认 HIGH |
| DeepSeek 错误无感知 | `classify_prompt` 返回 error_message，events.py 通知用户 |
| 审批卡片不出现 | 三步排查：① `permissions.allow` ② `send_card` 是否报错 ③ hook_log 有记录 |

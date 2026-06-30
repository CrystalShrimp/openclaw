"""Claude Code CLI subprocess manager — interactive mode.

Spawns `claude` with --print --input-format stream-json and keeps
the process alive.  User messages are written to stdin as JSONL
and responses are read from stdout by a background reader task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime

from config.settings import settings
from app.feishu.cards import (
    build_streaming_card,
    build_tool_result_card,
    build_error_card,
)
from app.feishu.client import feishu_client
from app.models.schemas import AgentResult, ToolCallRecord
from app.audit.logger import audit_logger
from app.profiles import load_active_profile_env, OPENCLAW_ROOT, CONFIG_DIR

logger = logging.getLogger("openclaw.cli_loop")

# Map claude_session_id -> {open_id, approval_mode}
# Used by hooks router to look up user + approval mode for tool cards.
session_registry: dict[str, dict] = {}


# ---- helpers ----


def _build_env(workspace: str) -> dict:
    env = os.environ.copy()
    for key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]:
        env.pop(key, None)
    # Inject active profile env vars (ANTHROPIC_BASE_URL, AUTH_TOKEN,
    # model names) per-process so concurrent claude invocations under
    # different profiles don't fight over ~/.claude/settings.json.
    env.update(load_active_profile_env())
    if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
        for candidate in [
            r"D:\Git\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Git\bin\bash.exe",
        ]:
            if os.path.isfile(candidate):
                env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                break
    return env


def _write_openclaw_settings() -> None:
    """幂等覆写 config/claude_settings.json — openclaw 拥有这个文件，不合并。

    子进程通过 --settings 加载它，优先级高于所有 settings.json 层级。
    每次 spawn 前覆写，自愈：即使用户手改过，下次 spawn 恢复预期配置。
    """
    settings_file = CONFIG_DIR / "claude_settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    hook_script = OPENCLAW_ROOT / "scripts" / "hooks" / "pre_tool_use.py"
    payload = {
        "permissions": {
            "allow": [
                "Bash(*)", "Write(*)", "Edit(*)", "NotebookEdit(*)",
                "Read(*)", "Glob(*)", "Grep(*)", "WebSearch", "WebFetch",
            ],
            "deny": [],
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": f'python "{hook_script}"',
                    "timeout": 1800,
                }],
            }],
        },
        "skipDangerousModePermissionPrompt": True,
    }
    settings_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), "utf-8",
    )


def _strip_openclaw_hooks(workspace: str) -> None:
    """移除 workspace/.claude/settings.local.json 里 openclaw 注入过的 hook 条目。

    按 hook script 路径子串匹配；只清自己注入的，不动用户其他配置。
    文件清空到 {} 才删，否则保留（用户可能还有 permissions 等 key）。
    """
    from pathlib import Path

    f = Path(workspace) / ".claude" / "settings.local.json"
    if not f.exists():
        return
    try:
        data = json.loads(f.read_text("utf-8"))
    except Exception:
        return
    pre = data.get("hooks", {}).get("PreToolUse")
    if not pre:
        return
    needle = str(OPENCLAW_ROOT / "scripts" / "hooks" / "pre_tool_use.py")
    kept = [
        e for e in pre
        if not any(needle in h.get("command", "") for h in e.get("hooks", []))
    ]
    if kept == pre:
        return
    if kept:
        data.setdefault("hooks", {})["PreToolUse"] = kept
    else:
        data.get("hooks", {}).pop("PreToolUse", None)
        if not data.get("hooks"):
            data.pop("hooks", None)
    if data == {}:
        f.unlink()
    else:
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    logger.info("Stripped openclaw hooks from %s", f)


async def _ensure_hook_config(workspace: str) -> None:
    """每次 spawn 前调用：写 openclaw 自有 settings + 清 workspace 旧注入。

    第三步清理 openclaw 项目自己的 .claude/settings.local.json（旧版本代码
    注入 hook 的地方）。当前该文件只有 permissions 没 hook，是 no-op；保留
    这步以应对历史残留。按脚本路径匹配，不会误删开发者自配的 hook。
    """
    _write_openclaw_settings()
    _strip_openclaw_hooks(workspace)
    _strip_openclaw_hooks(str(OPENCLAW_ROOT))


# ---- main class ----


class ClaudeCLILoop:
    """Manage a long-lived Claude Code CLI subprocess per user.

    The process runs with --print --input-format stream-json so it stays
    alive between messages.  Each user message is written to stdin as a
    JSONL ``user`` event and the background reader resolves the matching
    future when a ``result`` event arrives.

    Each pending message has its own *msg_state* dict (text accumulator,
    tools list, streaming card id) stored alongside the future, so
    concurrent / multi-turn state never leaks between messages.
    """

    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._stdin_writers: dict[str, asyncio.StreamWriter] = {}
        # list of (future, msg_state) — one entry per pending message
        self._response_futures: dict[str, list[tuple[asyncio.Future, dict]]] = {}
        self._reader_tasks: dict[str, asyncio.Task] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._last_error: str = ""
        # Users whose process has had its stdin closed (--print mode is
        # one-shot: once stdin is closed the process can no longer accept
        # input, even though returncode may still be None while it drains).
        self._consumed: set[str] = set()

    # ---- public API ----

    def is_running(self, open_id: str) -> bool:
        """Check if there's a running CLI process that can still accept messages."""
        if open_id in self._consumed:
            return False
        proc = self._processes.get(open_id)
        return proc is not None and proc.returncode is None

    def get_session_id_for_user(self, open_id: str) -> str | None:
        """Get the Claude session_id for a currently running user process."""
        for claude_sid, info in session_registry.items():
            if info.get("open_id") == open_id:
                return claude_sid
        return None

    async def send_and_wait(
        self,
        prompt: str,
        open_id: str,
        workspace: str,
        model: str | None = None,
        approval_mode: str = "m",
        claude_session_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> AgentResult:
        """Send a prompt to the user's interactive Claude process.

        Starts the process on first call (or after a crash).  Session
        handling uses claude's native flags:

        - *resume_session_id*  → ``claude --resume <id>`` (specific session)
        - *claude_session_id*  → ``claude --continue``       (most recent in workspace)
        - neither              → fresh session
        """
        lock = self._start_locks.setdefault(open_id, asyncio.Lock())
        chosen = model or settings.claude_default_model

        async with lock:
            # Start process if needed. A previous process whose stdin was
            # closed (--print one-shot) must be torn down first, otherwise
            # the old reader task's _cleanup would race with the new
            # process's state.
            if not self.is_running(open_id):
                await self._teardown_previous(open_id)
                await self._start_process(
                    open_id, workspace, model, approval_mode, claude_session_id,
                    resume_session_id,
                )

            writer = self._stdin_writers.get(open_id)
            if not writer or writer.is_closing():
                return self._error_result(
                    prompt, "stdin writer not available (process may have crashed)",
                )

            # Per-message state: card + text accumulator (isolated from other messages)
            msg_state = {
                "text": "",
                "tools": [],
                "tool_count": 0,
                "last_update": 0.0,
                "card_id": "",
            }

            # Create a fresh streaming card for this message
            try:
                card = build_streaming_card(chosen, "")
                card_msg = await feishu_client.send_card(open_id, card)
                msg_state["card_id"] = card_msg.get("data", {}).get("message_id", "")
            except Exception as e:
                logger.warning("Failed to create streaming card: %s", e)

            # Queue future + msg_state BEFORE writing stdin so the reader
            # always finds msg_state for the very first stream_event.
            future: asyncio.Future[AgentResult] = asyncio.get_event_loop().create_future()
            self._response_futures.setdefault(open_id, []).append((future, msg_state))

            # Write JSONL user message to stdin
            payload = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }) + "\n"
            writer.write(payload.encode())
            await writer.drain()
            writer.close()  # 关闭 stdin 让 CLI 2.1.71 开始处理消息
            # stdin 已关闭，进程进入"已消费"状态：不能再接受新输入。
            # 下一条消息必须启动新进程（见 _teardown_previous）。
            self._consumed.add(open_id)

        return await future

    async def _teardown_previous(self, open_id: str) -> None:
        """Tear down any previous process for this user and wait for its
        reader task to finish cleanup.

        Must run before _start_process when starting a fresh process: the
        old reader's _cleanup pops state keyed by open_id, so if we let it
        run concurrently with a new _start_process it would wipe the new
        process's entries.
        """
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
            except asyncio.CancelledError:
                pass

    def cancel_by_user(self, open_id: str) -> bool:
        """Kill the running process for a user (synchronous)."""
        proc = self._processes.pop(open_id, None)
        # Resolve any pending futures as cancelled
        entries = self._response_futures.pop(open_id, [])
        for future, _ in entries:
            if not future.done():
                future.set_result(self._error_result("", "process terminated", "cancelled"))

        # Cancel reader task (it will run _cleanup in its finally block)
        task = self._reader_tasks.pop(open_id, None)
        if task and not task.done():
            task.cancel()

        self._stdin_writers.pop(open_id, None)
        self._consumed.discard(open_id)

        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False

    async def cancel_and_wait(self, open_id: str) -> bool:
        """Kill process and wait for reader task cleanup.

        Use this in async contexts where you need to start a new process
        immediately afterwards (e.g. /model, /stop auto-continue).
        """
        cancelled = self.cancel_by_user(open_id)
        # Small delay for reader task to process EOF and run _cleanup
        await asyncio.sleep(0.3)
        return cancelled

    def cancel(self, task_id: str) -> bool:
        """Not used (open_id based).  Kept for compatibility."""
        logger.warning("cancel(task_id) is deprecated; use cancel_by_user(open_id)")
        return False

    # ---- internal ----

    async def _start_process(
        self, open_id: str, workspace: str, model: str | None,
        approval_mode: str, claude_session_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        chosen = model or settings.claude_default_model
        cli_path = settings.claude_cli_path

        # shutil.which resolves bare names ("claude") to a real path,
        # picking up .cmd/.bat/.exe on Windows that CreateProcess alone
        # won't auto-append. Without this, asyncio.create_subprocess_exec
        # raises FileNotFoundError (WinError 2) for "claude".
        resolved = shutil.which(cli_path)
        if not resolved:
            raise FileNotFoundError(
                f"Claude CLI not found: {cli_path!r}. "
                f"Install it (`npm i -g @anthropic-ai/claude-code`) or set "
                f"CLAUDE_CLI_PATH to an absolute path."
            )

        args = [
            resolved,
            "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            # 隔离 desktop 端 ~/.claude/settings.json：只加载 project+local 级，
            # 再用 --settings 显式叠加 openclaw 自有配置（hook + permissions），
            # --settings 优先级最高，覆盖一切重叠 key。
            "--setting-sources", "project,local",
            "--settings", str(CONFIG_DIR / "claude_settings.json"),
        ]
        # Use claude's native session flags. Explicit --resume <id> wins
        # over --continue; if neither applies, start a fresh session.
        if resume_session_id:
            args.extend(["--resume", resume_session_id])
        elif claude_session_id:
            args.append("--continue")
        # High-tolerance approval mode: skip openclaw's hook entirely
        # and let claude's native bypassPermissions handle everything.
        if approval_mode == "h":
            args.extend(["--permission-mode", "bypassPermissions"])
        if chosen:
            args.extend(["--model", chosen])

        env = _build_env(workspace)
        await _ensure_hook_config(workspace)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=env,
            limit=10 * 1024 * 1024,
        )
        self._processes[open_id] = proc
        self._stdin_writers[open_id] = proc.stdin  # type: ignore[assignment]
        logger.warning(
            "Interactive Claude CLI started: open_id=%s workspace=%s model=%s pid=%s",
            open_id, workspace, chosen, proc.pid,
        )

        # Start background reader
        task = asyncio.create_task(
            self._read_loop(open_id, proc, approval_mode),
        )
        self._reader_tasks[open_id] = task

    # ---- helpers for _read_loop ----

    @staticmethod
    def _current_msg_state(entries: list) -> dict | None:
        """Return the msg_state of the oldest pending message, if any."""
        if entries:
            return entries[0][1]
        return None

    # ---- reader loop ----

    async def _read_loop(
        self, open_id: str, proc: asyncio.subprocess.Process, approval_mode: str,
    ) -> None:
        """Background task: read JSONL from stdout, dispatch events, resolve futures."""
        stderr_lines: list[str] = []
        model: str = "Claude Code"
        task_id = uuid.uuid4().hex[:12]

        # ---- stderr drainer ----
        async def _drain_stderr() -> None:
            pipe = proc.stderr
            if pipe is None:
                return
            while True:
                line = await pipe.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                stderr_lines.append(decoded)
                logger.warning("claude stderr: %s", decoded)
        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            while True:
                pipe = proc.stdout
                if pipe is None:
                    break
                raw_line = await pipe.readline()
                if not raw_line:
                    break  # process exited
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line: %s", line[:200])
                    continue

                etype = event.get("type", "")
                entries = self._response_futures.get(open_id, [])
                msg_state = self._current_msg_state(entries)

                # --- system/init ---
                if etype == "system" and event.get("subtype") == "init":
                    claude_sid = event.get("session_id", "")
                    if claude_sid:
                        session_registry[claude_sid] = {
                            "open_id": open_id,
                            "approval_mode": approval_mode,
                        }
                    model = event.get("model", model)

                # --- stream_event ---
                elif etype == "stream_event":
                    delta = event.get("event", {}).get("delta", {})
                    if msg_state and delta.get("type") == "text_delta":
                        msg_state["text"] += delta.get("text", "")
                        now = asyncio.get_event_loop().time()

                        # 超长换卡：内容超过阈值时开新卡
                        CARD_TEXT_LIMIT = 3000
                        if len(msg_state["text"]) >= CARD_TEXT_LIMIT and msg_state.get("card_id"):
                            try:
                                old_final = build_streaming_card(model, msg_state["text"], continued=True)
                                await feishu_client.update_card(msg_state["card_id"], old_final)
                                new_card = build_streaming_card(model, msg_state["text"][-1500:])
                                card_msg = await feishu_client.send_card(open_id, new_card)
                                msg_state["card_id"] = card_msg.get("data", {}).get("message_id", "")
                                msg_state["text"] = msg_state["text"][-1500:]
                                msg_state["last_update"] = now
                            except Exception:
                                pass
                            continue

                        # 正常流式更新
                        if now - msg_state.get("last_update", 0) >= 0.5 and msg_state.get("card_id"):
                            try:
                                update = build_streaming_card(model, msg_state["text"])
                                await feishu_client.update_card(msg_state["card_id"], update)
                                msg_state["last_update"] = now
                            except Exception:
                                pass

                # --- assistant ---
                elif etype == "assistant":
                    if not msg_state:
                        continue
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            # Only accumulate if stream_events were NOT sent
                            # (some providers skip streaming and only send
                            #  assistant messages).  Otherwise this is a
                            #  duplicate of what we already streamed.
                            if not msg_state["text"]:
                                msg_state["text"] += block.get("text", "")
                        elif block.get("type") == "tool_use":
                            msg_state["tool_count"] += 1
                            msg_state["tools"].append(
                                ToolCallRecord(
                                    tool_name=block.get("name", "unknown"),
                                    arguments=block.get("input", {}),
                                ),
                            )

                # --- user (tool results) ---
                elif etype == "user":
                    pass

                # --- result ---
                elif etype == "result":
                    self._dispatch_result(event, open_id, task_id, stderr_lines, entries)
                    task_id = uuid.uuid4().hex[:12]

                # --- system/api_retry ---
                elif etype == "system" and event.get("subtype") == "api_retry":
                    attempt = event.get("attempt", 0)
                    max_retries = event.get("max_retries", "?")
                    logger.warning("Claude API retry: attempt=%d", attempt)
                    await feishu_client.send_text(
                        open_id,
                        f"API 重试中 ({attempt}/{max_retries})...",
                    )

            # ---- process exited (stdout EOF) ----
            await proc.wait()
            exit_code = proc.returncode or 0
            self._last_error = ""
            if stderr_lines:
                self._last_error = f"exit={exit_code}\n" + "\n".join(stderr_lines[-10:])

            # Resolve any remaining pending futures as failed
            entries = self._response_futures.pop(open_id, [])
            for future, msg_state in entries:
                if not future.done():
                    reason = self._last_error or f"Claude CLI exited (code={exit_code})"
                    future.set_result(self._error_result("", reason))
                    # Update card with error if it exists
                    if msg_state.get("card_id"):
                        try:
                            err_card = build_error_card("Claude CLI 已退出", reason[:3500])
                            await feishu_client.update_card(msg_state["card_id"], err_card)
                        except Exception:
                            pass

            logger.info(
                "Claude interactive process exited: open_id=%s code=%s",
                open_id, exit_code,
            )

        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
        except Exception as exc:
            logger.exception("Claude read loop error: open_id=%s", open_id)
            # Resolve remaining futures
            entries = self._response_futures.pop(open_id, [])
            for future, _ in entries:
                if not future.done():
                    future.set_result(self._error_result("", str(exc)))
        finally:
            stderr_task.cancel()
            self._cleanup(open_id)

    def _dispatch_result(
        self, event: dict, open_id: str, task_id: str,
        stderr_lines: list[str], entries: list,
    ) -> None:
        """Handle a single ``result`` event — resolve the oldest pending future."""
        if not entries:
            logger.debug(
                "Result event with no waiter: open_id=%s task=%s", open_id, task_id,
            )
            return

        future, msg_state = entries.pop(0)

        subtype = event.get("subtype", "")
        cost = event.get("total_cost_usd", 0)
        duration_ms = event.get("duration_ms", 0)
        duration_s = duration_ms / 1000.0
        num_turns = event.get("num_turns", 0)
        error = event.get("error", "")
        usage = event.get("usage", {})

        text = msg_state.get("text", "")
        tools = msg_state.get("tools", [])
        tool_count = msg_state.get("tool_count", 0)
        card_id = msg_state.get("card_id", "")

        status = "failed" if subtype.startswith("error") else "completed"
        result_text = event.get("result", "") or text

        result = AgentResult(
            task_id=task_id,
            prompt="",
            model=event.get("model", "unknown"),
            status=status,
            text=result_text,
            tools_used=tools,
            error=error if subtype == "error" else "",
            session_id=event.get("session_id", ""),
            cost_usd=cost,
            duration_s=duration_s,
            num_turns=num_turns,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )

        # Update streaming card → final result card
        if card_id:
            try:
                final_card = build_tool_result_card(
                    task_id=task_id,
                    result_text=result_text,
                    status=status,
                    cost_usd=cost,
                    duration_s=duration_s,
                    tools_count=tool_count,
                    error=error,
                )
                asyncio.create_task(
                    feishu_client.update_card(card_id, final_card),
                )
            except Exception:
                pass

        if not future.done():
            future.set_result(result)

        audit_logger.log_agent_result(
            task_id=task_id,
            model="claude-code",
            status=status,
            tools_count=tool_count,
            text_len=len(result_text),
        )

        logger.info(
            "Claude done: task=%s status=%s cost=$%.4f time=%.1fs tools=%d",
            task_id, status, cost, duration_s, tool_count,
        )

    def _cleanup(self, open_id: str) -> None:
        """Clean up all state for a user after process exit or error."""
        self._processes.pop(open_id, None)
        self._stdin_writers.pop(open_id, None)
        self._reader_tasks.pop(open_id, None)
        self._consumed.discard(open_id)
        # Clean up session_registry entries for this user
        stale_sids = [
            sid for sid, info in session_registry.items()
            if info.get("open_id") == open_id
        ]
        for sid in stale_sids:
            del session_registry[sid]

    @staticmethod
    def _error_result(prompt: str, error: str, status: str = "failed") -> AgentResult:
        return AgentResult(
            task_id=uuid.uuid4().hex[:12],
            prompt=prompt,
            model="unknown",
            status=status,
            error=error,
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )


# Singleton
claude_cli_loop = ClaudeCLILoop()

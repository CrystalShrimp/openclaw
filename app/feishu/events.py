from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import uuid
from pathlib import Path

from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
)

from app.agent.cli_loop import claude_cli_loop
from app.approval.manager import approval_manager
from app.feishu.client import feishu_client
from app.audit.logger import audit_logger
from app.models.schemas import Session, TaskStatus
from app.profiles import discover_profiles, get_active_profile, switch_profile, test_profile
from config.settings import settings

logger = logging.getLogger("openclaw.events")


# ===== Claude native session readers =====
# claude persists the full conversation at
# ~/.claude/projects/<encoded-workspace>/<session_id>.jsonl — we read it
# back directly instead of duplicating the history in .sessions/ files.


def _find_claude_session_file(session_id: str) -> Path | None:
    """Locate the jsonl file for a claude session_id under ~/.claude/projects/.

    The intermediate directory name encodes the workspace path, but its
    exact transformation is claude-internal; we sidestep it by globbing
    on the session_id, which is unique.
    """
    if not session_id:
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _iter_claude_session_messages(session_id: str) -> list[dict]:
    """Return user/assistant messages from a claude session jsonl.

    Each entry: {"role": "user"|"assistant", "content": str}. Tool-only
    or system rows are skipped. Returns [] if the session file is absent
    or unreadable.
    """
    f = _find_claude_session_file(session_id)
    if f is None:
        return []
    out: list[dict] = []
    try:
        for raw in f.read_text("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("type") not in ("user", "assistant"):
                continue
            msg = row.get("message", {})
            content = msg.get("content", "")
            # claude stores content as a list of blocks for assistant
            # turns; flatten to text.
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(p for p in text_parts if p)
            if not isinstance(content, str):
                content = str(content)
            out.append({"role": row["type"], "content": content})
    except Exception as e:
        logger.warning("Failed to read claude session %s: %s", session_id, e)
    return out


# ===== Session management =====


class SessionManager:
    """Session manager with file-based persistence for Claude session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._user_sessions: dict[str, str] = {}  # open_id -> session_id
        self._lock = threading.Lock()
        self._state_dir = Path(settings.audit_log_path).parent / ".sessions"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _state_file(self, open_id: str) -> Path:
        return self._state_dir / f"{open_id}.json"

    def _load_all(self) -> None:
        """Load persisted sessions on startup."""
        if not self._state_dir.exists():
            return
        for f in self._state_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text("utf-8"))
                session = Session(**data)
                with self._lock:
                    self._sessions[session.session_id] = session
                    self._user_sessions[session.user_open_id] = session.session_id
            except Exception:
                pass

    def _save(self, session: Session) -> None:
        """Persist session to disk."""
        try:
            self._state_file(session.user_open_id).write_text(
                session.model_dump_json(indent=2), "utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

    def create_session(
        self,
        user_open_id: str,
        chat_id: str,
        workspace: str | None = None,
    ) -> Session:
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id=session_id,
            user_open_id=user_open_id,
            chat_id=chat_id,
            workspace=workspace or settings.default_workspace,
            approval_mode=settings.approval_mode,
        )
        with self._lock:
            self._sessions[session_id] = session
            self._user_sessions[user_open_id] = session_id
        self._save(session)
        return session

    def get_user_session(self, open_id: str) -> Session | None:
        sid = self._user_sessions.get(open_id)
        if sid:
            return self._sessions.get(sid)
        return None

    def save_session(self, session: Session) -> None:
        """Explicitly persist session changes."""
        self._save(session)

    def clean_old_sessions(self, open_id: str) -> None:
        """Remove all session state files except the current user's."""
        if not self._state_dir.exists():
            return
        for f in self._state_dir.glob("*.json"):
            if f.stem != open_id:
                try:
                    f.unlink()
                    logger.info("Cleaned old session: %s", f.name)
                except Exception as e:
                    logger.warning("Failed to clean %s: %s", f.name, e)

    def reset_user_session(self, open_id: str) -> Session:
        old_sid = self._user_sessions.get(open_id)
        if old_sid:
            old = self._sessions.pop(old_sid, None)
            chat_id = old.chat_id if old else ""
            mode = old.approval_mode if old else settings.approval_mode
            # 继承当前 workspace：/new 语义是"同一项目里开新会话"，不是回到 default。
            # 想换 workspace 用 /pwd。首次没有旧 session 时回落到 default。
            workspace = old.workspace if old else None
        else:
            chat_id = ""
            mode = settings.approval_mode
            workspace = None
        session = self.create_session(open_id, chat_id, workspace=workspace)
        session.approval_mode = mode
        self._save(session)
        return session


session_manager = SessionManager()


# ===== Helpers =====


def _is_user_allowed(open_id: str) -> bool:
    allowed = settings.get_allowed_users()
    if not allowed:
        return True
    return open_id in allowed


def _parse_message_text(content: str) -> str:
    try:
        data = json.loads(content)
        return data.get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return content.strip()


def _mode_label(mode: str) -> str:
    return {"h": "高容忍(全允许)", "m": "中风险(高风险审批)", "l": "低容忍(全审批)"}.get(mode, mode)


_SHORTCUTS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "shortcuts.json"


def _load_shortcuts() -> dict[str, str]:
    if not _SHORTCUTS_FILE.exists():
        return {}
    try:
        return json.loads(_SHORTCUTS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _decode_output(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ===== Event handlers =====


def on_message_receive(event: P2ImMessageReceiveV1) -> None:
    if not event.event:
        logger.debug("on_message_receive: no event payload")
        return
    msg = event.event.message
    sender = event.event.sender
    if not msg or not sender or not sender.sender_id:
        logger.debug("on_message_receive: missing msg/sender")
        return
    if msg.message_type != "text":
        return
    open_id = sender.sender_id.open_id or ""
    chat_id = msg.chat_id or ""
    message_id = msg.message_id or ""
    text = _parse_message_text(msg.content or "{}")
    if not text:
        return
    logger.info("From %s: %s", open_id, text[:100])
    asyncio.get_running_loop().create_task(_dispatch(open_id, chat_id, message_id, text))


def on_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    if not event.event:
        return P2CardActionTriggerResponse()
    action = event.event.action
    operator = event.event.operator
    if not action or not action.value or not operator:
        return P2CardActionTriggerResponse()
    approval_id = action.value.get("approval_id", "")
    act = action.value.get("act", "")
    card_type = action.value.get("type", "")
    open_id = operator.open_id or ""
    if not approval_id or not act:
        return P2CardActionTriggerResponse()

    # Model selection card: switch_model → approve + update session model
    if card_type == "model_selection" and act == "switch_model":
        target_model = action.value.get("model", "")
        if target_model not in ("haiku", "sonnet", "opus"):
            target_model = "sonnet"
        session = session_manager.get_user_session(open_id)
        if session:
            session.last_model = target_model
        approval_manager.set_switch_model(approval_id, target_model)
        asyncio.get_running_loop().create_task(
            approval_manager.handle_decision(approval_id, open_id, True)
        )
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = f"已切换模型: {target_model}"
        resp.toast = toast
        return resp

    # Profile switch card: switch provider (GLM, Kimi, etc.)
    if card_type == "profile_switch" and act == "switch_profile":
        profile_name = action.value.get("profile", "")
        profiles = discover_profiles()
        label = profiles.get(profile_name, {}).get("label", profile_name)

        if profile_name not in profiles:
            resp = P2CardActionTriggerResponse()
            toast = CallBackToast()
            toast.type = "error"
            toast.content = f"切换失败: 未找到 {label} 配置"
            resp.toast = toast
            return resp

        # 1. Switch the settings file
        if not switch_profile(profile_name):
            resp = P2CardActionTriggerResponse()
            toast = CallBackToast()
            toast.type = "error"
            toast.content = f"切换失败: 无法写入配置文件"
            resp.toast = toast
            return resp

        # 2. Kill any running CLI process
        claude_cli_loop.cancel_by_user(open_id)

        # 3. Test the new profile with a minimal API request
        ok, detail = test_profile()

        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        if ok:
            toast.type = "success"
            toast.content = f"成功切换为 {label} 模型"
        else:
            toast.type = "error"
            toast.content = f"切换失败: {label} 不可用 ({detail})"
        resp.toast = toast
        return resp

    approved = act == "approve"
    card_label = "工具执行" if card_type == "tool_execution" else "审批"
    asyncio.get_running_loop().create_task(
        approval_manager.handle_decision(approval_id, open_id, approved)
    )

    # Update card to show result (replaces buttons with status text)
    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "success" if approved else "error"
    toast.content = "已允许" if approved else "已拒绝"
    resp.toast = toast
    resp.card = _build_done_card(
        title=f"{'✅' if approved else '❌'} {card_label}已{'允许' if approved else '拒绝'}",
        color="green" if approved else "red",
        approval_id=approval_id,
    )
    return resp


def _build_done_card(title: str, color: str, approval_id: str) -> object:
    """Build a minimal card that replaces the approval card after decision."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard
    card = CallBackCard()
    card.type = "raw"
    card.data = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"审批ID: `{approval_id}`"}},
        ],
    }
    return card


# ===== Core dispatch =====


async def _dispatch(open_id: str, chat_id: str, message_id: str, text: str) -> None:
    if not _is_user_allowed(open_id):
        await feishu_client.send_text(open_id, "抱歉，您没有使用权限。")
        return

    text = text.strip()
    text_lower = text.lower()

    # --- /stop: interrupt current task ---
    if text_lower in ("/stop", "停止"):
        cancelled = await claude_cli_loop.cancel_and_wait(open_id)
        if cancelled:
            await feishu_client.send_text(open_id, "已中断会话。")
        else:
            await feishu_client.send_text(open_id, "没有正在运行的任务。")
        return

    # --- /status ---
    if text_lower == "/status":
        session = session_manager.get_user_session(open_id)
        if session:
            # Build context usage info
            ctx_info = ""
            if session.context_tokens > 0:
                pct = session.context_tokens / session.context_limit * 100
                ctx_info = (
                    f"上下文用量: `{session.context_tokens:,}` / `{session.context_limit:,}` ({pct:.0f}%)\n"
                )
                if pct >= settings.context_critical_percent:
                    if settings.compact_enabled:
                        ctx_info += "⚠️ 上下文即将耗尽，建议使用 `/compact` 压缩或 `/new` 开始新会话\n"
                    else:
                        ctx_info += "⚠️ 上下文即将耗尽，建议使用 `/new` 开始新会话\n"
                elif pct >= settings.context_warn_percent:
                    if settings.compact_enabled:
                        ctx_info += "⚠️ 上下文用量较高，可以用 `/compact` 压缩上下文\n"
                    else:
                        ctx_info += "⚠️ 上下文用量较高，请注意\n"
            await feishu_client.send_text(
                open_id,
                f"会话ID: `{session.session_id}`\n"
                f"Claude Session: `{session.claude_session_id or '未启动'}`\n"
                f"工作区: `{session.workspace}`\n"
                f"审批模式: {_mode_label(session.approval_mode)}\n"
                f"上次模型: `{session.last_model or '无'}`\n"
                f"当前供应商: `{get_active_profile()}`\n"
                f"消息数: {len(_iter_claude_session_messages(session.claude_session_id))}\n"
                f"{ctx_info}"
                f"状态: {session.status.value}",
            )
        else:
            await feishu_client.send_text(open_id, "没有活跃会话。用 /new 创建新会话。")
        return

    # --- /switch [profile]: switch provider profile (GLM, Kimi, etc.) ---
    if text_lower == "/switch" or text_lower.startswith("/switch "):
        parts = text.split(None, 1)
        profiles = discover_profiles()
        if not profiles:
            await feishu_client.send_text(
                open_id,
                "未发现任何 profile 配置。\n"
                "请在 config/ 目录下创建 settings_glm.json、settings_kimi.json 等文件。",
            )
            return

        # /switch <name>: direct switch without card
        if len(parts) == 2:
            name = parts[1].strip().lower()
            if name not in profiles:
                await feishu_client.send_text(
                    open_id,
                    f"未知 profile: `{name}`\n可用: {'、'.join(profiles.keys())}",
                )
                return
            label = profiles[name].get("label", name)
            if not switch_profile(name):
                await feishu_client.send_text(open_id, "切换失败: 无法写入 marker 文件")
                return
            claude_cli_loop.cancel_by_user(open_id)
            ok, detail = test_profile()
            if ok:
                await feishu_client.send_text(open_id, f"✅ 已切换到 {label}。\n{detail}")
            else:
                await feishu_client.send_text(
                    open_id, f"⚠️ 已切换到 {label}，但测试失败：{detail}",
                )
            return

        # /switch (no arg): show selection card
        active = get_active_profile()
        from app.feishu.cards import build_profile_selection_card
        import uuid as _uuid

        card = build_profile_selection_card(
            approval_id=_uuid.uuid4().hex[:12],
            profiles=profiles,
            active_profile=active,
        )
        await feishu_client.send_card(open_id, card)
        return

    # --- /model [haiku|sonnet|opus] ---  (must be before /mode to avoid prefix clash)
    if text_lower.startswith("/model"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        valid_models = ("haiku", "sonnet", "opus")
        if len(parts) < 2 or parts[1].strip().lower() not in valid_models:
            cur = session.last_model if session else settings.claude_default_model
            await feishu_client.send_text(
                open_id,
                f"当前模型: `{cur or settings.claude_default_model}`\n\n"
                "用法: `/model haiku|sonnet|opus`\n"
                "- `haiku` — 快速，适合简单任务\n"
                "- `sonnet` — 均衡，适合大多数开发任务\n"
                "- `opus` — 最强，适合复杂架构和分析",
            )
            return
        new_model = parts[1].strip().lower()
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        session.last_model = new_model
        session_manager.save_session(session)

        await claude_cli_loop.cancel_and_wait(open_id)

        await feishu_client.send_text(
            open_id,
            f"模型已切换: `{new_model}`，发消息或 `/continue` 即可继续",
        )
        return

    # --- /mode [h|m|l] ---
    if text_lower.startswith("/mode"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        if len(parts) < 2 or parts[1].strip().lower() not in ("h", "m", "l"):
            cur = session.approval_mode if session else settings.approval_mode
            await feishu_client.send_text(
                open_id,
                f"当前审批模式: {_mode_label(cur)}\n\n"
                "用法: `/mode h|m|l`\n"
                "- `h` 高容忍：默认允许所有工具调用\n"
                "- `m` 中风险：高风险工具(Bash/Write/Edit)需审批\n"
                "- `l` 低容忍：所有工具调用都需审批",
            )
            return
        new_mode = parts[1].strip().lower()
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        session.approval_mode = new_mode
        session_manager.save_session(session)
        await feishu_client.send_text(
            open_id, f"审批模式已切换: {_mode_label(new_mode)}",
        )
        return

    # --- /pwd <path|shortcut>: switch workspace ---
    if text_lower.startswith("/pwd"):
        parts = text.split(None, 1)
        shortcuts = _load_shortcuts()
        if len(parts) < 2 or not parts[1].strip():
            session = session_manager.get_user_session(open_id)
            ws = session.workspace if session else settings.default_workspace
            shortcut_list = ""
            if shortcuts:
                items = "\n".join(f"  `{k}` → `{v}`" for k, v in shortcuts.items())
                shortcut_list = f"\n\n快捷路径:\n{items}"
            await feishu_client.send_text(
                open_id,
                f"当前工作区: `{ws}`\n用法: `/pwd D:\\your\\path` 或 `/pwd 快捷名`{shortcut_list}",
            )
            return
        new_path = parts[1].strip()
        # Check shortcuts first
        if new_path in shortcuts:
            new_path = shortcuts[new_path]
        p = Path(new_path)
        if not p.is_absolute():
            await feishu_client.send_text(
                open_id, "错误：请使用绝对路径或已注册的快捷名，例如 `/pwd D:\\projects\\myapp`",
            )
            return
        if not p.exists():
            await feishu_client.send_text(open_id, f"错误：路径不存在: `{new_path}`")
            return
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id, workspace=new_path)
        else:
            session.workspace = str(p.resolve())
            session.claude_session_id = ""  # 工作区变了，旧 session 无法 resume
        await feishu_client.send_text(open_id, f"工作区已切换: `{session.workspace}`")
        return

    # --- /resume <session_id>: resume a specific session via native --resume ---
    if text_lower.startswith("/resume"):
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            session = session_manager.get_user_session(open_id)
            cur = session.claude_session_id if session else ""
            await feishu_client.send_text(
                open_id,
                f"当前 Claude Session: `{cur or '无'}`\n\n"
                "用法: `/resume <session_id>`\n"
                "session_id 是启动时卡片打印的 Session ID。\n"
                "用 claude 原生 `--resume <id>` 恢复指定会话。",
            )
            return
        resume_sid = parts[1].strip()
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        # Kill any running process so _start_process actually runs with
        # the new --resume flag.
        await claude_cli_loop.cancel_and_wait(open_id)
        await feishu_client.send_text(
            open_id,
            f"使用 `--resume {resume_sid}` 恢复会话...",
        )
        await _run_claude(
            "继续上次的任务", open_id, session,
            skip_classify=True, resume_session_id=resume_sid,
        )
        return

    # --- /continue [prompt]: resume most recent session via native --continue ---
    if text_lower.startswith("/continue"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id)

        arg = parts[1].strip() if len(parts) > 1 else ""
        # /continue <session_id> 形式不再有意义（claude 总是恢复最近会话），
        # 但仍接受参数当作普通提示处理。
        prompt = arg if arg else "继续上次的任务"

        # Force --continue even if openclaw has no recorded session: claude
        # looks up the most recent session in the workspace itself. Sentinel
        # just makes _start_process append --continue.
        if not session.claude_session_id:
            session.claude_session_id = "__continue__"
            session_manager.save_session(session)
        await _run_claude(prompt, open_id, session, skip_classify=True)
        return

    # --- /new: reset session (kill process + fresh session, keep workspace) ---
    if text_lower == "/new":
        await claude_cli_loop.cancel_and_wait(open_id)
        session = session_manager.reset_user_session(open_id)
        await feishu_client.send_text(
            open_id,
            f"新会话已创建。\n会话ID: `{session.session_id}`\n"
            f"工作区: `{session.workspace}`\n"
            f"审批模式: {_mode_label(session.approval_mode)}",
        )
        return

    # --- /clean: remove old session files, keep only current ---
    if text_lower == "/clean":
        session = session_manager.get_user_session(open_id)
        if session:
            session_manager.clean_old_sessions(open_id)
            await feishu_client.send_text(
                open_id,
                f"已清理旧会话文件，当前会话: `{session.session_id}`",
            )
        else:
            await feishu_client.send_text(open_id, "没有活跃会话。")
        return

    # --- /compact: trigger CLI's built-in compact on current session ---
    if text_lower == "/compact":
        if not settings.compact_enabled:
            await feishu_client.send_text(open_id, "压缩功能已禁用。请联系管理员开启。")
            return
        session = session_manager.get_user_session(open_id)
        if not session or not session.claude_session_id:
            await feishu_client.send_text(
                open_id,
                "没有活跃的 Claude 会话，无法压缩。\n"
                "先发一条消息启动会话，上下文不足时再使用 `/compact`。",
            )
            return
        await _run_claude("/compact", open_id, session, skip_classify=True)
        return

    # --- /mem <text>: append memo to CLAUDE.md in current workspace ---
    # CLAUDE.md is the filename claude auto-loads as in-session memory,
    # so writing there means the next prompt sees the memo with no extra
    # wiring. Legacy AGENT.md is migrated on first write.
    if text_lower.startswith("/mem "):
        content = text[5:].strip()
        if not content:
            await feishu_client.send_text(
                open_id, "用法: `/mem <内容>`，例如 `/mem CSMAR弹窗需要先关闭才能操作`",
            )
            return
        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.default_workspace
        memory_md = Path(workspace) / "CLAUDE.md"
        legacy_md = Path(workspace) / "AGENT.md"
        try:
            # One-shot migration: fold legacy AGENT.md into CLAUDE.md so
            # claude actually picks it up.
            if legacy_md.exists() and not memory_md.exists():
                memory_md.write_text(legacy_md.read_text("utf-8"), "utf-8")
                legacy_md.unlink()
                logger.info("Migrated %s → %s", legacy_md, memory_md)

            if memory_md.exists():
                existing = memory_md.read_text("utf-8").rstrip("\n")
                memory_md.write_text(existing + "\n" + content + "\n", "utf-8")
            else:
                memory_md.write_text(content + "\n", "utf-8")
            await feishu_client.send_text(open_id, f"已记录到 `{workspace}` 下的 CLAUDE.md")
        except Exception as e:
            await feishu_client.send_text(open_id, f"写入失败: {e}")
        return

    # --- /sh <command>: execute shell command in workspace ---
    if text_lower.startswith("/sh "):
        cmd = text[4:].strip()
        if not cmd:
            await feishu_client.send_text(
                open_id, "用法: `/sh <command>`，例如 `/sh mkdir ZhiWang`",
            )
            return
        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.default_workspace
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            out = _decode_output(stdout).strip()
            err = _decode_output(stderr).strip()
        except asyncio.TimeoutError:
            proc.kill()
            await feishu_client.send_text(open_id, f"⏱️ 命令超时(30s): `{cmd}`")
            return
        except Exception as e:
            await feishu_client.send_text(open_id, f"执行失败: `{e}`")
            return
        parts = [f"📁 `{workspace}`", f"▶ `{cmd}`"]
        if out:
            display = out if len(out) <= 4000 else out[:3950] + "\n... (输出已截断)"
            parts.append(f"```\n{display}\n```")
        if err:
            display = err if len(err) <= 1000 else err[:950] + "\n..."
            parts.append(f"⚠️ stderr:\n```\n{display}\n```")
        parts.append(f"退出码: {proc.returncode}")
        await feishu_client.send_text(open_id, "\n".join(parts))
        return

    # --- Default: run Claude CLI ---
    await _run_claude(text, open_id)


async def _run_claude(
    prompt: str,
    open_id: str,
    session: Session | None = None,
    skip_classify: bool = False,
    resume_session_id: str | None = None,
) -> None:
    """Run Claude CLI with optional model selection card for first message.

    resume_session_id: if set, start claude with ``--resume <id>`` to
    continue a specific session (used by /resume <id>). When None, the
    claude_session_id on the session drives ``--continue``.
    """
    audit_logger.log_command_received(open_id, prompt, "started")
    try:
        if not session:
            session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, "")

        # Determine model
        model = None

        # If user manually set a model via /model or previous card choice, use that
        if session.last_model and session.last_model in ("haiku", "sonnet", "opus"):
            model = session.last_model

        # First message (no process running): show card for manual selection
        elif not skip_classify and not claude_cli_loop.is_running(open_id):
            from app.feishu.cards import build_model_selection_card

            async def _send_model_card(aid: str) -> None:
                card = build_model_selection_card(
                    approval_id=aid,
                    prompt=prompt,
                    default_model=settings.claude_default_model,
                )
                await feishu_client.send_card(open_id, card)

            approved, final_model = await approval_manager.request_model_approval(
                prompt=prompt,
                model=settings.claude_default_model,
                send_card_fn=_send_model_card,
                open_id=open_id,
            )

            if not approved:
                await feishu_client.send_text(open_id, "已取消执行。")
                return

            model = final_model
            session.last_model = model
            await feishu_client.send_text(
                open_id,
                f"模型已确认: `{model}`，开始执行...",
            )

        # Run or send to existing interactive Claude CLI
        agent_result = await claude_cli_loop.send_and_wait(
            prompt=prompt,
            open_id=open_id,
            workspace=session.workspace,
            model=model or settings.claude_default_model,
            approval_mode=session.approval_mode,
            claude_session_id=session.claude_session_id or None,
            resume_session_id=resume_session_id,
        )

        # Process was killed (switch/stop/new) — caller already notified user
        if agent_result.status == "cancelled":
            return

        # Update session. Clear session_id on failure to avoid
        # --resume into a broken session on the next message.
        if agent_result.status == "failed":
            session.claude_session_id = ""
        elif agent_result.session_id:
            session.claude_session_id = agent_result.session_id
        if agent_result.input_tokens > 0:
            session.context_tokens = agent_result.input_tokens
        # NOTE: agent_messages intentionally not appended — claude already
        # persists the full conversation in ~/.claude/projects/*/<sid>.jsonl,
        # which _iter_claude_session_messages reads back on demand.

        # Build context warning suffix for notifications
        ctx_warning = ""
        if session.context_tokens > 0:
            ctx_pct = session.context_tokens / session.context_limit * 100
            if ctx_pct >= settings.context_critical_percent:
                if settings.compact_enabled:
                    ctx_warning = (
                        f"\n\n🚨 **上下文已用 {ctx_pct:.0f}%，即将耗尽！**\n"
                        f"使用 `/compact` 压缩上下文继续对话，或 `/new` 开始全新会话。"
                    )
                else:
                    ctx_warning = (
                        f"\n\n🚨 **上下文已用 {ctx_pct:.0f}%，即将耗尽！**\n"
                        f"请使用 `/new` 开始新会话，否则后续对话可能报错。"
                    )
            elif ctx_pct >= settings.context_warn_percent:
                if settings.compact_enabled:
                    ctx_warning = (
                        f"\n\n⚠️ 上下文用量: {ctx_pct:.0f}%，可以用 `/compact` 压缩上下文。"
                    )
                else:
                    ctx_warning = (
                        f"\n\n⚠️ 上下文用量: {ctx_pct:.0f}%，建议适时用 `/new` 开新会话。"
                    )

        # Persist session to disk
        session_manager.save_session(session)

        try:
            session.status = TaskStatus(agent_result.status)
        except ValueError:
            session.status = TaskStatus.COMPLETED

        # Send result summary notification
        if agent_result.status == "completed":
            sid_hint = f"\nSession: `{session.claude_session_id[:12]}...`" if session.claude_session_id else ""
            ctx_info = ""
            if session.context_tokens > 0:
                ctx_info = f" | 上下文: {session.context_tokens:,}"
            await feishu_client.send_text(
                open_id,
                f"✅ 任务完成 | 耗时: {agent_result.duration_s:.1f}s | "
                f"工具: {len(agent_result.tools_used)}次 | "
                f"费用: ${agent_result.cost_usd:.4f}{ctx_info}"
                f"{sid_hint}\n\n"
                f"直接发消息即可继续对话，或用 `/continue` 恢复上次会话。"
                f"{ctx_warning}",
            )
        elif agent_result.status == "failed":
            await feishu_client.send_text(
                open_id,
                f"❌ 任务失败 | 原因: {agent_result.error[:200]}\n\n"
                f"发消息即可重试，或用 `/continue` 恢复会话。"
                f"{ctx_warning}",
            )
        elif agent_result.status == "cancelled":
            await feishu_client.send_text(open_id, "任务已取消。发消息即可开始新任务。")

        audit_logger.log_command_received(open_id, prompt, agent_result.status)

    except Exception as e:
        logger.exception("Dispatch error for user %s", open_id)
        try:
            await feishu_client.send_text(
                open_id,
                f"处理指令时出错：{type(e).__name__}: {e}",
            )
        except Exception:
            pass

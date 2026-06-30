"""Hook callback endpoints for Claude Code PreToolUse approval.

Approval modes (per session):
  h = 高容忍: all tools auto-allowed (no approval card)
  m = 中风险: high-risk tools need approval card
  l = 低容忍: all tools need approval card

Flow:
  Claude Code → PreToolUse hook script → HTTP POST /hooks/pre_tool_use
  → this handler checks mode → sends Feishu card (if needed) → returns decision
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Request

from app.agent.cli_loop import session_registry
from app.approval.manager import approval_manager
from app.feishu.client import feishu_client
from app.feishu.cards import build_tool_approval_card

logger = logging.getLogger("openclaw.hooks")

router = APIRouter(prefix="/hooks", tags=["hooks"])

# High-risk tools that need approval in mode 'm'
HIGH_RISK_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Bash commands that are always safe (read-only)
_SAFE_COMMAND_PATTERNS = (
    "ls", "dir", "cat", "head", "tail", "find", "grep", "which", "where",
    "cd", "pwd", "whoami", "echo", "type", "wc", "sort", "uniq", "diff", "file",
    "stat", "du", "df", "uname", "hostname", "date", "env", "printenv",
    "git status", "git log", "git diff", "git branch", "git remote", "git show", "git tag",
    "python --version", "python3 --version", "node --version", "npm --version",
    "pip list", "pip show", "pip --version", "uv --version", "uv run python -c",
    "ollama list", "ollama --version",
    "test ", "test -f", "test -d", "test -e",
)
# Keywords that make a command high-risk even if the first word looks safe
_HIGH_RISK_KEYWORDS = (
    "rm ", "rmdir", "del ", "format", "shutdown", "reboot",
    "pip install", "npm install", "yarn add",
    "git push", "git reset", "git checkout",
    "chmod", "chown", "mkfs",
    "curl -X POST", "curl -X PUT", "curl -X DELETE",
    "wget ",
    "> ", ">> ",
    "ssh ", "scp ",
)

# Debug: track recent hook calls
_hook_log: list[str] = []


@router.post("/pre_tool_use")
async def pre_tool_use(request: Request) -> dict:
    """Called by the PreToolUse hook script.

    Input:  {"tool_name": "...", "tool_input": {...}, "session_id": "..."}
    Output: {"decision": "allow"/"deny", "reason": "..."}
    """
    try:
        data = await request.json()
    except Exception:
        return {"decision": "allow", "reason": "invalid input, allowing"}

    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    claude_session_id = data.get("session_id", "")

    logger.info(
        "PreToolUse hook: tool=%s session=%s registry_keys=%s",
        tool_name, claude_session_id, list(session_registry.keys()),
    )
    _hook_log.append(f"tool={tool_name} session={claude_session_id} registry={list(session_registry.keys())}")

    # Look up session info
    reg = session_registry.get(claude_session_id)
    if not reg:
        logger.warning("No session registry for %s, allowing", claude_session_id)
        _hook_log.append(f"→ ALLOW (no registry entry)")
        return {"decision": "allow", "reason": "session not tracked"}

    open_id = reg["open_id"]
    mode = reg.get("approval_mode", "m")

    # --- Mode h: allow everything ---
    if mode == "h":
        logger.info("Mode h: auto-allowing %s", tool_name)
        return {"decision": "allow", "reason": "high tolerance mode"}

    # --- Mode m: analyze risk ---
    if mode == "m":
        needs_approval = _is_high_risk(tool_name, tool_input)
        if not needs_approval:
            _hook_log.append(f"→ ALLOW (low risk in mode m)")
            return {"decision": "allow", "reason": "low risk, medium mode"}

    # --- Mode l or mode m + high-risk: send approval card ---
    approval_id = uuid.uuid4().hex[:12]
    _hook_log.append(f"→ SENDING CARD approval_id={approval_id} to open_id={open_id}")

    async def _send_card(aid: str) -> None:
        card = build_tool_approval_card(
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=claude_session_id,
        )
        try:
            result = await feishu_client.send_card(open_id, card)
            _hook_log.append(f"→ CARD SENT ok, msg_id={result.get('data', {}).get('message_id', '?')}")
        except Exception as e:
            _hook_log.append(f"→ CARD SEND FAILED: {e}")
            raise

    # Use the actual risk analysis result for both audit log and approval tracking
    actual_high_risk = (mode == "l") or _is_high_risk(tool_name, tool_input)

    approved = await approval_manager.request_tool_approval(
        tool_name=tool_name,
        tool_arguments=tool_input,
        risk_level="high" if actual_high_risk else "low",
        send_card_fn=_send_card,
        open_id=open_id,
    )

    decision = "allow" if approved else "deny"
    reason = "用户已批准" if approved else "用户已拒绝"
    _hook_log.append(f"→ {decision} ({reason})")
    logger.info("PreToolUse decision: %s for %s (mode=%s)", decision, tool_name, mode)

    return {"decision": decision, "reason": reason}


@router.get("/debug/hooks")
async def debug_hooks():
    """Debug endpoint to see recent hook calls."""
    from app.agent.cli_loop import session_registry as sr
    return {
        "session_registry": {k: {kk: vv for kk, vv in v.items() if kk != "task_id"} for k, v in sr.items()},
        "hook_log": _hook_log[-20:],
    }


def _is_high_risk(tool_name: str, arguments: dict | None = None) -> bool:
    """Determine if a tool call is high-risk.

    - Write/Edit/NotebookEdit: always high-risk
    - Bash: analyze the actual command content
    - Everything else: low-risk
    """
    if tool_name in HIGH_RISK_TOOLS:
        return True

    if tool_name == "Bash" and arguments:
        cmd = arguments.get("command", "").strip()
        # Extract first command in a chain
        cmd_first = cmd.split(";")[0].split("&&")[0].split("|")[0].strip()
        cmd_lower = cmd_first.lower()

        # Check high-risk keywords first
        for kw in _HIGH_RISK_KEYWORDS:
            if kw in cmd_lower:
                return True

        # Check safe commands
        for safe in _SAFE_COMMAND_PATTERNS:
            if cmd_lower == safe or cmd_lower.startswith(safe + " ") or cmd_lower.startswith(safe + "\t"):
                return False

        # Also allow: python -c "..." (inline scripts, usually for checks)
        if cmd_lower.startswith("python -c ") or cmd_lower.startswith("python3 -c "):
            return False

        # Unknown command → high-risk by default
        return True

    # Non-Bash, non-Write/Edit tools: low-risk
    return False

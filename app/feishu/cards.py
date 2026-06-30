from __future__ import annotations

import json


# ===== Model Selection Card (manual selection, before execution) =====


def build_model_selection_card(
    approval_id: str,
    prompt: str,
    default_model: str = "sonnet",
) -> dict:
    """模型选择卡片 — 让用户手动选择模型。"""
    display_prompt = prompt[:300]
    trunc = "..." if len(prompt) > 300 else ""

    model_desc = {"haiku": "Haiku (快速)", "sonnet": "Sonnet (均衡)", "opus": "Opus (最强)"}
    all_models = ["haiku", "sonnet", "opus"]
    other_models = [m for m in all_models if m != default_model]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择模型"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**推荐模型：** `{model_desc[default_model]}`\n"
                        f"**指令：**\n```\n{display_prompt}{trunc}\n```"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"确认 {model_desc[default_model]}"},
                        "type": "primary",
                        "value": {
                            "approval_id": approval_id,
                            "act": "approve",
                            "type": "model_selection",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"改用 {model_desc[other_models[0]]}"},
                        "type": "default",
                        "value": {
                            "approval_id": approval_id,
                            "act": "switch_model",
                            "model": other_models[0],
                            "type": "model_selection",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"改用 {model_desc[other_models[1]]}"},
                        "type": "default",
                        "value": {
                            "approval_id": approval_id,
                            "act": "switch_model",
                            "model": other_models[1],
                            "type": "model_selection",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "danger",
                        "value": {
                            "approval_id": approval_id,
                            "act": "reject",
                            "type": "model_selection",
                        },
                    },
                ],
            },
        ],
    }


# ===== Profile Selection Card (switch provider: GLM, Kimi, etc.) =====


def build_profile_selection_card(
    approval_id: str,
    profiles: dict[str, dict],
    active_profile: str,
) -> dict:
    """Profile 切换卡片 — 让用户选择不同的模型供应商。"""
    profile_lines = []
    for name, info in profiles.items():
        marker = " ← 当前" if name == active_profile else ""
        label = info.get("label", name)
        model = info.get("model", "")
        profile_lines.append(f"- **{label}** (`{name}`) — 模型: `{model}`{marker}")

    profile_text = "\n".join(profile_lines) if profile_lines else "未发现任何 profile 配置"

    buttons = []
    for name, info in profiles.items():
        label = info.get("label", name)
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"切换到 {label}"},
            "type": "default",
            "value": {
                "approval_id": approval_id,
                "act": "switch_profile",
                "profile": name,
                "type": "profile_switch",
            },
        })

    if not buttons:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "关闭"},
            "type": "default",
            "value": {"approval_id": approval_id, "act": "close", "type": "profile_switch"},
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "切换模型供应商"},
            "template": "purple",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": profile_text},
            },
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
        ],
    }


# ===== Tool Approval Card (used by PreToolUse hook) =====


def build_tool_approval_card(
    approval_id: str,
    tool_name: str,
    tool_input: dict,
    session_id: str = "",
) -> dict:
    """工具执行确认卡片 — PreToolUse hook 审批用。"""
    # Format input for display
    input_display = json.dumps(tool_input, ensure_ascii=False, indent=2)
    if len(input_display) > 1000:
        input_display = input_display[:1000] + "\n..."

    # Risk-based color
    high_risk_tools = {"Bash", "Write", "Edit", "NotebookEdit"}
    is_high = tool_name in high_risk_tools
    color = "red" if is_high else "orange"
    risk_label = "高风险" if is_high else "低风险"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "工具执行确认"},
            "template": color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**工具：** `{tool_name}`\n"
                        f"**风险：** {risk_label}\n"
                        f"**参数：**\n```\n{input_display}\n```"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许执行"},
                        "type": "primary",
                        "value": {
                            "approval_id": approval_id,
                            "act": "approve",
                            "type": "tool_execution",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝执行"},
                        "type": "danger",
                        "value": {
                            "approval_id": approval_id,
                            "act": "reject",
                            "type": "tool_execution",
                        },
                    },
                ],
            },
        ],
    }


# ===== Streaming Card (real-time output updates) =====


def build_streaming_card(
    model: str,
    accumulated_text: str,
    finished: bool = False,
    continued: bool = False,
    session_id: str = "",
) -> dict:
    """流式输出卡片 — Claude CLI 思考/输出实时更新。"""
    if finished:
        status = "执行完成"
        color = "green"
    elif continued:
        status = "▶ 输出继续..."
        color = "turquoise"
    else:
        status = "思考中..."
        color = "blue"
    sid_hint = f" [{session_id[:8]}]" if session_id else ""

    display_text = accumulated_text[:3500] if len(accumulated_text) > 3500 else accumulated_text
    trunc = f"\n\n... (truncated, total {len(accumulated_text)} chars)" if len(accumulated_text) > 3500 else ""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"[{model}] {status}{sid_hint}"},
            "template": color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": display_text + trunc,
                },
            },
        ],
    }


# ===== Final Result Card =====


def build_tool_result_card(
    task_id: str,
    result_text: str,
    status: str,
    cost_usd: float = 0,
    duration_s: float = 0,
    tools_count: int = 0,
    error: str = "",
) -> dict:
    """最终结果卡片 — Claude CLI 执行完成后显示。"""
    status_label = {
        "completed": "执行完成",
        "failed": "执行失败",
        "cancelled": "已取消",
    }.get(status, status)

    color = "green" if status == "completed" else "red"

    stats = (
        f"**任务：** `{task_id}`\n"
        f"**耗时：** {duration_s:.1f}s\n"
        f"**工具调用：** {tools_count} 次\n"
        f"**费用：** ${cost_usd:.4f}"
    )
    if error:
        stats += f"\n**错误：** {error[:500]}"

    display = result_text[:3500] if len(result_text) > 3500 else result_text
    trunc = f"\n\n... (truncated, total {len(result_text)} chars)" if len(result_text) > 3500 else ""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": status_label},
            "template": color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": stats},
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**结果：**\n{display}{trunc}"},
            },
        ],
    }


# ===== Error Card =====


def build_error_card(title: str, detail: str) -> dict:
    """错误卡片。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": detail[:3500]},
            },
        ],
    }


# ===== Simple Text Card =====


def build_simple_text_card(title: str, content: str, color: str = "blue") -> dict:
    """Build a simple text notification card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            },
        ],
    }

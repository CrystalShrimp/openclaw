from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


# ===== Enums =====


class RiskLevel(str, enum.Enum):
    LOW = "low"
    HIGH = "high"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


# ===== Feishu Event Models =====


class FeishuEventHeader(BaseModel):
    event_id: str = ""
    event_type: str = ""
    token: str = ""
    app_id: str = ""
    tenant_key: str = ""


class FeishuSender(BaseModel):
    sender_id: dict = Field(default_factory=dict)
    sender_type: str = ""
    tenant_key: str = ""


class FeishuMessageContent(BaseModel):
    text: str = ""


class FeishuMessageEvent(BaseModel):
    message_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    message_type: str = ""
    content: str = ""
    create_time: str = ""
    sender: FeishuSender = Field(default_factory=FeishuSender)


class FeishuEventPayload(BaseModel):
    header: FeishuEventHeader = Field(default_factory=FeishuEventHeader)
    event: FeishuMessageEvent | None = None


class FeishuEventRequest(BaseModel):
    schema_: str = Field("2.0", alias="schema")
    header: FeishuEventHeader = Field(default_factory=FeishuEventHeader)
    event: FeishuMessageEvent | None = None
    challenge: str | None = None


class FeishuCardAction(BaseModel):
    action: dict = Field(default_factory=dict)
    open_id: str = ""
    user_token: str = ""
    open_message_id: str = ""


# ===== Internal Models =====


class ParsedCommand(BaseModel):
    raw_text: str
    command: str
    risk_level: RiskLevel
    user_open_id: str = ""
    chat_id: str = ""
    message_id: str = ""


class ApprovalRequest(BaseModel):
    approval_id: str
    command: ParsedCommand
    risk_level: RiskLevel
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    decided_by: str = ""
    decided_at: datetime | None = None


class ExecutionResult(BaseModel):
    task_id: str
    command: str
    status: TaskStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def output(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        return "\n".join(parts)

    @property
    def truncated_output(self, max_len: int = 4000) -> str:
        text = self.output
        if len(text) <= max_len:
            return text
        return text[: max_len - 50] + f"\n\n... (truncated, total {len(text)} chars)"


# ===== Agent Models =====


class ToolCallRecord(BaseModel):
    tool_name: str
    arguments: dict
    result_summary: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    approved: bool = True


class AgentResult(BaseModel):
    task_id: str
    prompt: str
    model: str
    status: TaskStatus = TaskStatus.RUNNING
    text: str = ""
    tools_used: list[ToolCallRecord] = Field(default_factory=list)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Claude CLI specific fields
    session_id: str = ""  # Claude Code session_id (from system/init)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    input_tokens: int = 0   # context size proxy from usage.input_tokens
    output_tokens: int = 0


class Session(BaseModel):
    session_id: str
    user_open_id: str
    chat_id: str
    workspace: str
    status: TaskStatus = TaskStatus.QUEUED
    current_task_id: str | None = None
    result: ExecutionResult | None = None
    agent_messages: list[dict] = Field(default_factory=list)
    claude_session_id: str = ""  # Claude Code CLI session_id for --resume
    approval_mode: str = "m"     # h=高容忍(全允许) m=中(高风险审批) l=低(全审批)
    last_model: str = ""         # last used claude model
    context_tokens: int = 0      # last input_tokens (context usage proxy)
    context_limit: int = 200000  # context window limit

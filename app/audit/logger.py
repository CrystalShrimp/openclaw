from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from config.settings import settings

logger = logging.getLogger("openclaw.audit")


class AuditLogger:
    def __init__(self) -> None:
        self._log_path = Path(settings.audit_log_path)
        self._setup()

    def _setup(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self._log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    def log(
        self,
        *,
        user_id: str,
        action: str,
        risk_level: str = "",
        command: str = "",
        approved_by: str = "",
        status: str = "",
        result_summary: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "command": command[:500],
            "approved_by": approved_by,
            "status": status,
            "result_summary": result_summary[:500],
        }
        logger.info(json.dumps(entry, ensure_ascii=False))

    def log_command_received(self, user_id: str, command: str, risk_level: str) -> None:
        self.log(
            user_id=user_id,
            action="command_received",
            risk_level=risk_level,
            command=command,
        )

    def log_approval_requested(self, user_id: str, command: str, approval_id: str) -> None:
        self.log(
            user_id=user_id,
            action="approval_requested",
            command=command,
            status=f"approval_id={approval_id}",
        )

    def log_approval_decided(
        self, approval_id: str, decided_by: str, decision: str, command: str
    ) -> None:
        self.log(
            user_id=decided_by,
            action="approval_decided",
            approved_by=decided_by,
            command=command,
            status=f"approval_id={approval_id} decision={decision}",
        )

    def log_execution_result(
        self, user_id: str, command: str, status: str, result_summary: str
    ) -> None:
        self.log(
            user_id=user_id,
            action="execution_result",
            command=command,
            status=status,
            result_summary=result_summary,
        )

    def log_classification(
        self, user_id: str, prompt: str, difficulty: str, model: str
    ) -> None:
        self.log(
            user_id=user_id,
            action="task_classified",
            command=prompt,
            status=f"difficulty={difficulty} model={model}",
        )

    def log_tool_approval_requested(
        self, tool_name: str, approval_id: str, risk_level: str
    ) -> None:
        self.log(
            user_id="system",
            action="tool_approval_requested",
            command=f"tool:{tool_name}",
            risk_level=risk_level,
            status=f"approval_id={approval_id}",
        )

    def log_tool_executed(
        self, tool_name: str, risk_level: str, result_summary: str
    ) -> None:
        self.log(
            user_id="system",
            action="tool_executed",
            command=f"tool:{tool_name}",
            risk_level=risk_level,
            result_summary=result_summary,
        )

    def log_agent_result(
        self, task_id: str, model: str, status: str, tools_count: int, text_len: int
    ) -> None:
        self.log(
            user_id="system",
            action="agent_result",
            status=f"task_id={task_id} model={model} status={status} tools={tools_count} text_len={text_len}",
        )


audit_logger = AuditLogger()

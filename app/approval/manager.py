from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from config.settings import settings
from app.models.schemas import ApprovalRequest, ApprovalStatus, ParsedCommand, RiskLevel
from app.audit.logger import audit_logger

logger = logging.getLogger("openclaw.approval")


class ApprovalManager:
    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._callbacks: dict[str, asyncio.Future[bool]] = {}
        self._switch_models: dict[str, str] = {}  # approval_id -> switched model
        self._approval_users: dict[str, str] = {}  # approval_id -> open_id

    def request_approval(
        self,
        command: ParsedCommand,
        send_card_fn,  # Callable to send approval card to approver
    ) -> asyncio.Future[bool]:
        """Create an approval request and return a Future that resolves when decided."""
        approval_id = uuid.uuid4().hex[:12]
        request = ApprovalRequest(
            approval_id=approval_id,
            command=command,
            risk_level=command.risk_level,
        )
        self._pending[approval_id] = request

        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._callbacks[approval_id] = future

        # Set timeout
        loop = asyncio.get_event_loop()
        loop.create_task(self._expire_after(approval_id, settings.approval_timeout))

        audit_logger.log_approval_requested(
            user_id=command.user_open_id,
            command=command.command,
            approval_id=approval_id,
        )

        # Send approval card (fire-and-forget)
        loop.create_task(send_card_fn(approval_id, command))

        return future

    async def request_tool_approval(
        self,
        tool_name: str,
        tool_arguments: dict,
        risk_level: RiskLevel,
        send_card_fn,  # async Callable(approval_id: str) -> None
        open_id: str = "",
    ) -> bool:
        """Request approval for a tool execution.

        Args:
            tool_name: Name of the tool to execute.
            tool_arguments: Arguments passed to the tool.
            risk_level: Risk level of the tool.
            send_card_fn: Async function that sends the approval card.
            open_id: User's open_id for expiry notifications.

        Returns:
            True if approved, False if rejected/expired.
        """
        approval_id = uuid.uuid4().hex[:12]

        # Create a synthetic command for audit tracking
        command = ParsedCommand(
            raw_text=f"tool:{tool_name}",
            command=f"Tool: {tool_name}({', '.join(f'{k}={v!r}' for k, v in list(tool_arguments.items())[:5])})",
            risk_level=risk_level,
        )

        request = ApprovalRequest(
            approval_id=approval_id,
            command=command,
            risk_level=risk_level,
        )
        self._pending[approval_id] = request

        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._callbacks[approval_id] = future

        if open_id:
            self._approval_users[approval_id] = open_id

        loop = asyncio.get_event_loop()
        loop.create_task(self._expire_after(approval_id, settings.tool_approval_timeout))

        audit_logger.log_tool_approval_requested(
            tool_name=tool_name,
            approval_id=approval_id,
            risk_level=risk_level.value if hasattr(risk_level, "value") else str(risk_level),
        )

        # Send the tool approval card
        try:
            await send_card_fn(approval_id)
        except Exception as e:
            logger.error("Failed to send tool approval card: %s", e)
            future.set_result(False)

        return await future

    async def handle_decision(self, approval_id: str, decided_by: str, approved: bool) -> bool:
        """Handle an approval/rejection decision from card callback."""
        request = self._pending.get(approval_id)
        if not request:
            return False

        if request.status != ApprovalStatus.PENDING:
            return False

        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.decided_by = decided_by
        request.decided_at = datetime.now()

        audit_logger.log_approval_decided(
            approval_id=approval_id,
            decided_by=decided_by,
            decision="approved" if approved else "rejected",
            command=request.command.command,
        )

        future = self._callbacks.get(approval_id)
        if future and not future.done():
            future.set_result(approved)

        return True

    async def _expire_after(self, approval_id: str, timeout: int) -> None:
        request = self._pending.get(approval_id)
        if not request:
            return

        # Send warning before expiry (5 min before, but only if timeout > 5 min)
        warn_seconds = settings.tool_approval_warn_seconds
        if timeout > warn_seconds:
            await asyncio.sleep(timeout - warn_seconds)
            request = self._pending.get(approval_id)
            if request and request.status == ApprovalStatus.PENDING:
                logger.warning("Approval %s expiring in %ds", approval_id, warn_seconds)
                # Notify user via Feishu
                try:
                    from app.feishu.client import feishu_client
                    # Find the user from the registry — look up by approval_id
                    open_id = self._approval_users.get(approval_id, "")
                    if open_id:
                        await feishu_client.send_text(
                            open_id,
                            f"⚠️ 审批即将过期！\n"
                            f"任务: {request.command.command[:100]}\n"
                            f"还有 {warn_seconds // 60} 分钟自动拒绝，请尽快处理。",
                        )
                except Exception:
                    pass
            await asyncio.sleep(warn_seconds)
        else:
            await asyncio.sleep(timeout)

        request = self._pending.get(approval_id)
        if request and request.status == ApprovalStatus.PENDING:
            request.status = ApprovalStatus.EXPIRED
            future = self._callbacks.get(approval_id)
            if future and not future.done():
                future.set_result(False)
            audit_logger.log_approval_decided(
                approval_id=approval_id,
                decided_by="system",
                decision="expired",
                command=request.command.command,
            )
            # Notify user that approval expired
            try:
                from app.feishu.client import feishu_client
                open_id = self._approval_users.get(approval_id, "")
                if open_id:
                    await feishu_client.send_text(
                        open_id,
                        f"❌ 审批已过期，任务已被拒绝。\n"
                        f"任务: {request.command.command[:100]}\n"
                        f"你可以用 /continue 恢复会话继续。",
                    )
            except Exception:
                pass

    def cleanup(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)
        self._callbacks.pop(approval_id, None)
        self._approval_users.pop(approval_id, None)

    def set_switch_model(self, approval_id: str, model: str) -> None:
        """Store a model switch decision for a model selection card."""
        self._switch_models[approval_id] = model

    def get_switch_model(self, approval_id: str) -> str | None:
        """Get the switched model, if any. Returns None if not switched."""
        return self._switch_models.pop(approval_id, None)

    async def request_model_approval(
        self,
        prompt: str,
        model: str,
        send_card_fn,  # async Callable(approval_id: str) -> None
        open_id: str = "",
    ) -> tuple[bool, str]:
        """Request model selection approval.

        Returns:
            (approved, final_model) — approved=True and the (possibly switched) model.
        """
        from app.feishu.cards import build_model_selection_card
        import uuid as _uuid
        approval_id = _uuid.uuid4().hex[:12]

        self._pending[approval_id] = ApprovalRequest(
            approval_id=approval_id,
            command=ParsedCommand(raw_text=prompt, command=prompt, risk_level=RiskLevel.LOW),
            risk_level=RiskLevel.LOW,
        )
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._callbacks[approval_id] = future

        if open_id:
            self._approval_users[approval_id] = open_id

        loop = asyncio.get_event_loop()
        loop.create_task(self._expire_after(approval_id, settings.approval_timeout))

        try:
            await send_card_fn(approval_id)
        except Exception as e:
            logger.error("Failed to send model selection card: %s", e)
            future.set_result(False)

        approved = await future
        switched = self._switch_models.pop(approval_id, None)
        final_model = switched if switched else model
        self._pending.pop(approval_id, None)
        self._callbacks.pop(approval_id, None)
        self._approval_users.pop(approval_id, None)
        return approved, final_model


approval_manager = ApprovalManager()

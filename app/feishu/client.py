from __future__ import annotations

import json
import logging

import httpx

from config.settings import settings

logger = logging.getLogger("openclaw.feishu")

# Feishu API base
API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Feishu API client for sending messages and interactive cards."""

    def __init__(self) -> None:
        self._app_id = settings.feishu_app_id
        self._app_secret = settings.feishu_app_secret
        self._tenant_access_token: str = ""
        self._http = httpx.AsyncClient(timeout=30.0)

    async def _ensure_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        resp = await self._http.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Failed to get tenant_access_token: %s", data)
            raise RuntimeError(f"Feishu auth failed: {data.get('msg')}")
        self._tenant_access_token = data["tenant_access_token"]
        return self._tenant_access_token

    async def _api_headers(self) -> dict:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, receive_id: str, text: str, *, is_chat: bool = False) -> dict:
        """Send a text message to a user or chat."""
        receive_id_type = "chat_id" if is_chat else "open_id"
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_text failed: %s", data)
            # Retry with refreshed token
            if data.get("code") == 99991663:
                await self._refresh_token()
                return await self.send_text(receive_id, text, is_chat=is_chat)
        return data

    async def send_card(
        self, receive_id: str, card: dict, *, is_chat: bool = False
    ) -> dict:
        """Send an interactive card message."""
        receive_id_type = "chat_id" if is_chat else "open_id"
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_card failed: %s", data)
            # Retry with refreshed token (same as send_text)
            if data.get("code") == 99991663:
                await self._refresh_token()
                return await self.send_card(receive_id, card, is_chat=is_chat)
            # Raise on other errors so callers know the card wasn't delivered
            raise RuntimeError(f"send_card failed: code={data.get('code')} msg={data.get('msg')}")
        return data

    async def reply_text(self, message_id: str, text: str) -> dict:
        """Reply to a specific message with text."""
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        return resp.json()

    async def reply_card(self, message_id: str, card: dict) -> dict:
        """Reply to a specific message with a card."""
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )
        return resp.json()

    async def update_card(self, message_id: str, card: dict) -> dict:
        """Update an existing card message."""
        headers = await self._api_headers()
        resp = await self._http.patch(
            f"{API_BASE}/im/v1/messages/{message_id}",
            headers=headers,
            json={"content": json.dumps(card)},
        )
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()


feishu_client = FeishuClient()

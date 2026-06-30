"""Minimal WS test — connect to Feishu and log every raw message."""
import os
import sys
import json
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

from lark_oapi.core.enum import LogLevel
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("test")


class DebugWsClient(WsClient):
    """Override to log every raw frame received."""

    async def _handle_message(self, msg: bytes) -> None:
        try:
            frame = Frame()
            frame.ParseFromString(msg)
            ft = FrameType(frame.method)

            # Log raw frame info
            headers = {h.key: h.value for h in frame.headers}
            payload_preview = frame.payload[:200] if frame.payload else b"(empty)"
            logger.info(
                "RAW FRAME: type=%s, headers=%s, payload=%s",
                ft.name, headers, payload_preview,
            )
        except Exception as e:
            logger.error("Failed to parse frame: %s", e)

        # Still run original handler
        await super()._handle_message(msg)


def on_message(event):
    logger.info("=== MESSAGE EVENT RECEIVED ===")
    if event.event and event.event.message:
        msg = event.event.message
        logger.info("  content: %s", msg.content)
        logger.info("  chat_id: %s", msg.chat_id)
        logger.info("  msg_type: %s", msg.message_type)
    if event.event and event.event.sender:
        sender = event.event.sender
        logger.info("  sender_id: %s", sender.sender_id.__dict__ if sender.sender_id else None)


handler = (
    EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(on_message)
    .build()
)

client = DebugWsClient(
    app_id=os.getenv("FEISHU_APP_ID", ""),
    app_secret=os.getenv("FEISHU_APP_SECRET", ""),
    event_handler=handler,
    log_level=LogLevel.DEBUG,
    auto_reconnect=False,
)

logger.info("Starting WS client...")
client.start()

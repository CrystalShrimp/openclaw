"""WebSocket long-connection client for Feishu event subscription.

Subclasses lark-oapi's WS client to fix two issues:
1. CARD callbacks are silently dropped by the base SDK
2. Module-level ``loop`` captures the wrong event loop under uvicorn
"""
from __future__ import annotations

import asyncio
import base64
import http
import logging
from urllib.parse import urlparse, parse_qs

import websockets

from lark_oapi.core.const import UTF_8
from lark_oapi.core.enum import LogLevel
from lark_oapi.core.json import JSON
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws.client import (
    Client as _BaseClient,
    _get_by_key,
    _new_ping_frame,
    _select,
)
from lark_oapi.ws.const import (
    DEVICE_ID, SERVICE_ID,
    HEADER_MESSAGE_ID, HEADER_TRACE_ID, HEADER_SUM,
    HEADER_SEQ, HEADER_TYPE, HEADER_BIZ_RT,
)
from lark_oapi.ws.enum import MessageType, FrameType
from lark_oapi.ws.model import Response
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

logger = logging.getLogger("openclaw.ws")


def _running_loop() -> asyncio.AbstractEventLoop:
    """Always return the currently-running event loop."""
    return asyncio.get_running_loop()


class FeishuWsClient(_BaseClient):
    """WS client with correct loop handling and CARD support."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._msg_count = 0
        self._last_msg_time = ""
        self._last_msg_type = ""
        self._last_msg_error = ""
        self._dispatch_count = 0

    # ---- Override _receive_message_loop to avoid module-level loop ----

    async def _receive_message_loop(self):
        from lark_oapi.ws.exception import ConnectionClosedException
        import datetime
        try:
            while True:
                if self._conn is None:
                    raise ConnectionClosedException("connection is closed")
                msg = await self._conn.recv()
                self._msg_count += 1
                self._last_msg_time = datetime.datetime.now().isoformat()
                # Use running loop instead of module-level ``loop``
                _running_loop().create_task(self._handle_message(msg))
        except Exception as e:
            self._last_msg_error = str(e)
            logger.error(self._fmt_log("receive message loop exit, err: {}", e))
            await self._disconnect()
            if self._auto_reconnect:
                await self._reconnect()

    # ---- Override _connect to use the *running* loop ----

    async def _connect(self) -> None:
        await self._lock.acquire()
        if self._conn is not None:
            self._lock.release()
            return
        try:
            conn_url = self._get_conn_url()
            u = urlparse(conn_url)
            q = parse_qs(u.query)
            conn_id = q[DEVICE_ID][0]
            service_id = q[SERVICE_ID][0]

            conn = await websockets.connect(conn_url)
            self._conn = conn
            self._conn_url = conn_url
            self._conn_id = conn_id
            self._service_id = service_id

            logger.info(self._fmt_log("connected to {}", conn_url))
            # Use running loop, NOT the module-level ``loop``
            _running_loop().create_task(self._receive_message_loop())
        except websockets.InvalidStatusCode as e:
            from lark_oapi.ws.client import _parse_ws_conn_exception
            _parse_ws_conn_exception(e)
        finally:
            if self._lock.locked():
                self._lock.release()

    # ---- Override _handle_data_frame to support CARD ----

    async def _handle_data_frame(self, frame: Frame) -> None:
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        self._last_msg_type = message_type.value
        logger.info(
            "WS frame: type=%s, msg_id=%s, payload_len=%d",
            message_type.value, msg_id, len(pl) if pl else 0,
        )

        resp = Response(code=http.HTTPStatus.OK)
        try:
            if message_type in (MessageType.EVENT, MessageType.CARD):
                result = self._event_handler.do_without_validation(pl)
            else:
                logger.warning("Unknown frame type: %s", message_type.value)
                return

            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            logger.error("Handle failed: type=%s, id=%s, err=%s", message_type.value, msg_id, e)
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    # ---- Async start (non-blocking) ----

    def start_async(self) -> asyncio.Task:
        task = _running_loop().create_task(self._run_forever())
        return task

    async def _run_forever(self) -> None:
        try:
            await self._connect()
            _running_loop().create_task(self._ping_loop())
            logger.info("WS client running (receive + ping loops started)")
            await _select()  # block forever
        except Exception as e:
            logger.error("WS client error: %s", e)
            if self._auto_reconnect:
                await self._reconnect()


def create_ws_client(
    app_id: str,
    app_secret: str,
    event_handler: EventDispatcherHandler,
) -> FeishuWsClient:
    return FeishuWsClient(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=event_handler,
        log_level=LogLevel.DEBUG,
        auto_reconnect=True,
    )

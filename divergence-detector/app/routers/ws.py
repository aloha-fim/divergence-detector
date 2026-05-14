"""WebSocket route for live alert delivery."""
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import User
from app.services.ws_manager import manager

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/live")
async def live(ws: WebSocket, api_key: str = Query(...)):
    """Connect with `wss://.../ws/live?api_key=...`. Auth is via query string
    rather than header because browser WS clients can't set headers."""
    async with SessionLocal() as db:
        user = (await db.execute(
            select(User).where(User.api_key == api_key)
        )).scalar_one_or_none()

    if not user:
        await ws.close(code=4401, reason="invalid_api_key")
        return

    await manager.connect(user.id, ws)
    logger.info("WS connected user=%s", user.id)

    try:
        # Server-push only; client messages are pings we just acknowledge
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(user.id, ws)
        logger.info("WS disconnected user=%s", user.id)

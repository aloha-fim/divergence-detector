"""WebSocket connection registry.

In-process for now (single-node). Move to Redis pub/sub when scaling out
across multiple uvicorn workers. Per-user keying means alerts route only
to the subscribing user's open connections.
"""
import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        self._conns: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns[user_id].add(ws)

    async def disconnect(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._conns[user_id].discard(ws)
            if not self._conns[user_id]:
                del self._conns[user_id]

    async def send_to_user(self, user_id: int, payload: dict[str, Any]) -> int:
        """Push a JSON payload to every open socket for `user_id`. Returns
        the count of successful sends. Dead sockets are silently pruned."""
        async with self._lock:
            targets = list(self._conns.get(user_id, set()))
        if not targets:
            return 0
        body = json.dumps(payload, default=str)
        sent = 0
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(body)
                sent += 1
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns[user_id].discard(ws)
        return sent

    async def broadcast(self, payload: dict[str, Any]) -> int:
        """Push to all connected users (admin/debug use)."""
        async with self._lock:
            users = list(self._conns.keys())
        total = 0
        for u in users:
            total += await self.send_to_user(u, payload)
        return total


manager = WSManager()

"""
Transporte SSE (Server-Sent Events) para MCP.

Flujo del transporte HTTP+SSE (spec 2024-11-05):
  1. El cliente abre  GET /sse            -> stream SSE
  2. El servidor emite  event: endpoint   -> data: /messages?sessionId=<id>
  3. El cliente hace POST a esa URL con los mensajes JSON-RPC
  4. El servidor responde 202 Accepted y publica la respuesta por el stream
     como  event: message  -> data: {...json-rpc...}
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Dict, Optional

from ..config import settings

log = logging.getLogger("mcp.sse")


class SseSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: dict) -> None:
        await self.queue.put(json.dumps(payload, ensure_ascii=False, default=str))

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)


class SseSessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, SseSession] = {}

    def create(self) -> SseSession:
        session = SseSession(uuid.uuid4().hex)
        self._sessions[session.session_id] = session
        log.info("Sesión SSE abierta: %s (activas=%d)", session.session_id, len(self._sessions))
        return session

    def get(self, session_id: str) -> Optional[SseSession]:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.closed = True
            log.info("Sesión SSE cerrada: %s (activas=%d)", session_id, len(self._sessions))

    def count(self) -> int:
        return len(self._sessions)


sessions = SseSessionManager()


def sse_frame(data: str, event: Optional[str] = None, event_id: Optional[str] = None) -> str:
    """Construye un frame SSE válido."""
    chunk = ""
    if event:
        chunk += f"event: {event}\n"
    if event_id:
        chunk += f"id: {event_id}\n"
    for line in (data.splitlines() or [""]):
        chunk += f"data: {line}\n"
    return chunk + "\n"


async def event_stream(session: SseSession, messages_path: str) -> AsyncIterator[str]:
    """Generador del stream SSE de una sesión."""
    endpoint = f"{messages_path}?sessionId={session.session_id}"
    yield sse_frame(endpoint, event="endpoint")
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    session.queue.get(), timeout=settings.sse_keepalive_seconds
                )
            except asyncio.TimeoutError:
                # Comentario SSE de keep-alive (evita que proxies corten la conexión)
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield sse_frame(item, event="message")
    finally:
        sessions.remove(session.session_id)

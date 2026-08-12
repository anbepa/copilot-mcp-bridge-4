"""
Servidor MCP unificado (Filesystem + Terminal + Browser + API Testing) sobre FastAPI.

Endpoints expuestos:
  GET  /                 -> ficha informativa del servidor
  GET  /health           -> healthcheck (para el script de arranque / monitoreo)
  GET  /sse              -> stream SSE (transporte MCP HTTP+SSE)
  POST /messages         -> canal de entrada JSON-RPC de la sesión SSE
  POST /mcp              -> transporte Streamable HTTP (request/response JSON)
  GET  /mcp              -> stream SSE alternativo en la misma ruta
  GET  /tools            -> listado legible de tools (debug)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .core.protocol import handle_message
from .core.registry import registry
from .transport.sse import event_stream, sessions

# Importar los módulos de tools registra las herramientas en el registry.
from .tools import filesystem as _filesystem  # noqa: F401
from .tools import terminal as _terminal  # noqa: F401
from .tools import browser as _browser  # noqa: F401
from .tools import apitesting as _apitesting  # noqa: F401

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("mcp.app")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}

app = FastAPI(
    title="Unified MCP Server (Filesystem + Terminal + Browser + API Testing)",
    version=settings.server_version,
    description="Servidor MCP sobre SSE con herramientas de archivos y consola.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "Content-Type"],
)


# --------------------------------------------------------------------------- #
def _check_auth(authorization: Optional[str]) -> None:
    if not settings.auth_token:
        return
    expected = f"Bearer {settings.auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Token de autorización inválido o ausente.")


# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> dict:
    return {
        "name": settings.server_name,
        "version": settings.server_version,
        "protocolVersion": settings.protocol_version,
        "transports": {
            "sse": {"stream": "/sse", "messages": "/messages"},
            "streamableHttp": "/mcp",
        },
        "tools": registry.names(),
        "toolCount": len(registry.names()),
        "workspaceRoot": str(settings.workspace_root),
        "authRequired": bool(settings.auth_token),
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "tools": len(registry.names()),
        "activeSseSessions": sessions.count(),
    }


@app.get("/tools")
async def tools() -> dict:
    return {"tools": registry.list_tools()}


# --------------------------------------------------------------------------- #
# Transporte 1: HTTP + SSE  (GET /sse  +  POST /messages)
# --------------------------------------------------------------------------- #
@app.get("/sse")
async def sse_endpoint(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> StreamingResponse:
    _check_auth(authorization)
    session = sessions.create()
    log.info("Cliente SSE conectado desde %s", request.client.host if request.client else "?")
    return StreamingResponse(
        event_stream(session, "/messages"),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.post("/messages")
async def messages_endpoint(
    request: Request,
    sessionId: str = Query(..., description="Identificador de sesión SSE"),  # noqa: N803
    authorization: Optional[str] = Header(default=None),
) -> Response:
    _check_auth(authorization)
    session = sessions.get(sessionId)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión SSE inexistente o expirada.")

    try:
        payload: Any = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Cuerpo JSON inválido.")

    response = await handle_message(payload)
    if response is not None:
        if isinstance(response, list):
            for item in response:
                await session.send(item)
        else:
            await session.send(response)
    return Response(status_code=202, content="Accepted")


# --------------------------------------------------------------------------- #
# Transporte 2: Streamable HTTP  (POST /mcp) — recomendado por clientes nuevos
# --------------------------------------------------------------------------- #
@app.post("/mcp")
async def streamable_http(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> Response:
    _check_auth(authorization)
    try:
        payload: Any = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Cuerpo JSON inválido.")

    response = await handle_message(payload)
    if response is None:
        return Response(status_code=202)

    # Según la spec el servidor puede responder JSON o un stream SSE. Se usa SSE
    # solo cuando el cliente NO acepta application/json explícitamente.
    accept = (request.headers.get("accept") or "").lower()
    if "text/event-stream" in accept and "application/json" not in accept:
        body = json.dumps(response, ensure_ascii=False, default=str)

        async def once():
            yield f"event: message\ndata: {body}\n\n"

        return StreamingResponse(once(), media_type="text/event-stream", headers=SSE_HEADERS)

    return JSONResponse(response)


@app.get("/mcp")
async def streamable_http_stream(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> StreamingResponse:
    """Algunos clientes abren el stream sobre la misma ruta /mcp."""
    _check_auth(authorization)
    session = sessions.create()
    return StreamingResponse(
        event_stream(session, "/messages"),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.on_event("startup")
async def _on_startup() -> None:
    log.info("=" * 72)
    log.info("  %s v%s", settings.server_name, settings.server_version)
    log.info("  Workspace  : %s", settings.workspace_root)
    log.info("  Tools (%02d) : %s", len(registry.names()), ", ".join(registry.names()))
    log.info("  SSE        : GET /sse   |  POST /messages?sessionId=...")
    log.info("  StreamHTTP : POST /mcp")
    log.info("  Auth       : %s", "Bearer token requerido" if settings.auth_token else "abierta")
    log.info("=" * 72)

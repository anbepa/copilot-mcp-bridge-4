#!/usr/bin/env python3
"""
Adaptador STDIO para el MCP Unified Server.

El servidor unificado expone sus 82 herramientas por SSE / Streamable HTTP,
pero el `copilot-mcp-bridge` habla JSON-RPC 2.0 sobre **stdio** (lanza cada
servidor MCP como proceso hijo y se comunica por stdin/stdout, delimitado por
saltos de línea).

Este script cierra esa brecha reutilizando el núcleo agnóstico del transporte
(`server.core.protocol.handle_message`): lee mensajes JSON-RPC por stdin,
los procesa y escribe las respuestas por stdout. No arranca FastAPI ni uvicorn,
así que no requiere `fastapi`/`uvicorn` — solo la librería estándar (y,
opcionalmente, `playwright` para las tools `browser_*`).

Uso (lo invoca el bridge automáticamente vía config/default.json):
    python stdio_server.py [WORKSPACE_ROOT ...]

El primer argumento posicional (si existe) fija MCP_WORKSPACE_ROOT (sandbox de
filesystem). El bridge pasa aquí sus `roots`.

IMPORTANTE: stdout está reservado EXCLUSIVAMENTE para tramas JSON-RPC.
Todo el logging se redirige a stderr para no corromper el canal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def _bootstrap_env() -> None:
    """Carga .env (si existe) y aplica el workspace recibido por argv."""
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(
                key.strip(), value.strip().strip('"').strip("'")
            )

    # El bridge pasa sus roots como argumentos posicionales. Usamos el primero
    # como raíz del sandbox de filesystem.
    roots = [a for a in sys.argv[1:] if not a.startswith("-")]
    if roots:
        os.environ["MCP_WORKSPACE_ROOT"] = str(Path(roots[0]).expanduser().resolve())


def _configure_logging() -> None:
    # StreamHandler apunta a stderr por defecto: NUNCA a stdout.
    logging.basicConfig(
        level=getattr(logging, os.getenv("MCP_LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stderr,
    )


async def _serve() -> None:
    # Importar los módulos de tools registra las herramientas en el registry.
    from server.core.protocol import handle_message
    from server.tools import filesystem as _filesystem  # noqa: F401
    from server.tools import terminal as _terminal  # noqa: F401
    from server.tools import browser as _browser  # noqa: F401
    from server.tools import apitesting as _apitesting  # noqa: F401

    log = logging.getLogger("mcp.stdio")

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    out = sys.stdout

    def _write(obj: dict) -> None:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    log.info("MCP Unified Server (stdio) listo. Esperando mensajes JSON-RPC…")

    while True:
        raw = await reader.readline()
        if not raw:  # EOF: el bridge cerró stdin
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Línea no-JSON ignorada: %s", line[:120])
            continue

        try:
            response = await handle_message(message)
        except Exception as exc:  # noqa: BLE001
            log.exception("Error procesando mensaje")
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32603, "message": str(exc)},
            }

        if response is not None:
            _write(response)


def main() -> None:
    _bootstrap_env()
    _configure_logging()
    try:
        asyncio.run(_serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()

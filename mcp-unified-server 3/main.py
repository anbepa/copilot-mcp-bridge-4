#!/usr/bin/env python3
"""
Punto de entrada del servidor MCP unificado.

Uso:
    python main.py                 # host/puerto desde .env o valores por defecto
    python main.py --port 9000
    python main.py --workspace /ruta/al/proyecto
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def load_dotenv(path: Path) -> None:
    """Carga un .env sencillo sin dependencias externas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified MCP Server (Filesystem + Terminal + Browser + API Testing)")
    parser.add_argument("--host", default=None, help="Host de escucha (def. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Puerto (def. 8787)")
    parser.add_argument("--workspace", default=None, help="Raíz permitida para filesystem")
    parser.add_argument("--token", default=None, help="Bearer token de autenticación")
    parser.add_argument("--log-level", default=None, help="debug|info|warning|error")
    parser.add_argument("--reload", action="store_true", help="Recarga automática (dev)")
    return parser.parse_args()


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()

    if args.host:
        os.environ["MCP_HOST"] = args.host
    if args.port:
        os.environ["MCP_PORT"] = str(args.port)
    if args.workspace:
        os.environ["MCP_WORKSPACE_ROOT"] = str(Path(args.workspace).expanduser().resolve())
    if args.token:
        os.environ["MCP_AUTH_TOKEN"] = args.token
    if args.log_level:
        os.environ["MCP_LOG_LEVEL"] = args.log_level

    try:
        import uvicorn
    except ImportError:
        sys.exit(
            "❌ Falta uvicorn. Instala las dependencias con:\n"
            "   pip install -r requirements.txt"
        )

    from server.config import settings

    print("")
    print("╔" + "═" * 70 + "╗")
    print("║  MCP UNIFIED SERVER — Filesystem · Terminal · Browser · API Testing".ljust(71) + "║")
    print("╠" + "═" * 70 + "╣")
    print(f"║  Local SSE   : http://{settings.host}:{settings.port}/sse".ljust(71) + "║")
    print(f"║  Streamable  : http://{settings.host}:{settings.port}/mcp".ljust(71) + "║")
    print(f"║  Health      : http://{settings.host}:{settings.port}/health".ljust(71) + "║")
    print(f"║  Workspace   : {settings.workspace_root}".ljust(71) + "║")
    print("╚" + "═" * 70 + "╝")
    print("")

    uvicorn.run(
        "server.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=args.reload,
        timeout_keep_alive=300,
    )


if __name__ == "__main__":
    main()

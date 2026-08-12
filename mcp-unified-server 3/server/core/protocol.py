"""
Implementación nativa del protocolo MCP (JSON-RPC 2.0) sin SDK externo.

Métodos soportados:
  - initialize
  - notifications/initialized  (notificación)
  - ping
  - tools/list
  - tools/call
  - resources/list, prompts/list  (respuestas vacías para compatibilidad)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

from ..config import settings
from .registry import ToolError, registry

log = logging.getLogger("mcp.protocol")

# Códigos de error JSON-RPC
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

JsonDict = Dict[str, Any]


def _result(req_id: Any, result: JsonDict) -> JsonDict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> JsonDict:
    err: JsonDict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _to_content(value: Any) -> List[JsonDict]:
    """Normaliza cualquier retorno de tool al formato content[] de MCP."""
    if isinstance(value, list) and value and all(
        isinstance(i, dict) and "type" in i for i in value
    ):
        return value  # ya viene en formato MCP
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return [{"type": "text", "text": text}]


async def handle_message(
    message: Union[JsonDict, List[JsonDict]]
) -> Optional[Union[JsonDict, List[JsonDict]]]:
    """Procesa un mensaje (o batch) JSON-RPC y devuelve la respuesta o None."""
    if isinstance(message, list):
        responses = [await handle_message(m) for m in message]
        responses = [r for r in responses if r is not None]
        return responses or None

    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "El mensaje debe ser un objeto JSON-RPC.")

    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not method:
        return None if is_notification else _error(
            req_id, INVALID_REQUEST, "Falta el campo 'method'."
        )

    log.debug("-> %s (id=%s)", method, req_id)

    try:
        # ---------------------------------------------------------------- #
        if method == "initialize":
            client_proto = params.get("protocolVersion") or settings.protocol_version
            return _result(
                req_id,
                {
                    "protocolVersion": client_proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {},
                        "prompts": {},
                        "logging": {},
                    },
                    "serverInfo": {
                        "name": settings.server_name,
                        "version": settings.server_version,
                    },
                    "instructions": (
                        "Servidor MCP unificado. Grupo 'Filesystem' para CRUD de "
                        "archivos (read_file, write_file, create_directory, "
                        "list_directory, move_file, search_nodes) y grupo "
                        "'Terminal' para ejecución de comandos (run, run_background, "
                        "list_background, kill_background)."
                    ),
                },
            )

        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return _result(req_id, {})

        if method == "tools/list":
            return _result(req_id, {"tools": registry.list_tools()})

        if method == "resources/list":
            return _result(req_id, {"resources": []})

        if method == "resources/templates/list":
            return _result(req_id, {"resourceTemplates": []})

        if method == "prompts/list":
            return _result(req_id, {"prompts": []})

        if method == "logging/setLevel":
            return _result(req_id, {})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not name:
                return _error(req_id, INVALID_PARAMS, "Falta 'name' en tools/call.")
            try:
                value = await registry.call(name, arguments)
                return _result(
                    req_id, {"content": _to_content(value), "isError": False}
                )
            except ToolError as exc:
                return _result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"❌ {exc}"}],
                        "isError": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Error ejecutando la tool %s", name)
                return _result(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"❌ {type(exc).__name__}: {exc}",
                            }
                        ],
                        "isError": True,
                    },
                )

        if is_notification:
            return None
        return _error(req_id, METHOD_NOT_FOUND, f"Método no soportado: {method}")

    except Exception as exc:  # noqa: BLE001
        log.exception("Error interno procesando %s", method)
        if is_notification:
            return None
        return _error(req_id, INTERNAL_ERROR, str(exc))

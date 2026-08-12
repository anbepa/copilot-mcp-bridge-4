"""
Registro de herramientas (tools) MCP.

Permite declarar herramientas con un decorador y expone la lista en el
formato exacto que exige la especificación de Model Context Protocol.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

ToolHandler = Callable[..., Any | Awaitable[Any]]


class ToolError(Exception):
    """Error controlado de una herramienta (se devuelve con isError=true)."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: ToolHandler
    title: Optional[str] = None

    def to_mcp(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            payload["title"] = self.title
        return payload


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    # ------------------------------------------------------------------ #
    def tool(
        self,
        name: str,
        description: str,
        input_schema: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorador para registrar una herramienta."""

        def decorator(func: ToolHandler) -> ToolHandler:
            schema = input_schema or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            self._tools[name] = Tool(
                name=name,
                description=description,
                input_schema=schema,
                handler=func,
                title=title,
            )
            return func

        return decorator

    # ------------------------------------------------------------------ #
    def list_tools(self) -> List[Dict[str, Any]]:
        return [t.to_mcp() for t in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"Herramienta desconocida: '{name}'. "
                f"Disponibles: {', '.join(sorted(self._tools))}"
            )
        return self._tools[name]

    # ------------------------------------------------------------------ #
    def _validate(self, tool: Tool, arguments: Dict[str, Any]) -> None:
        schema = tool.input_schema
        required = schema.get("required", [])
        props = schema.get("properties", {})
        missing = [r for r in required if arguments.get(r) in (None, "")]
        if missing:
            raise ToolError(
                f"Faltan parámetros obligatorios para '{tool.name}': {', '.join(missing)}"
            )
        for key, value in arguments.items():
            spec = props.get(key)
            if not spec:
                continue
            expected = spec.get("type")
            if expected == "string" and not isinstance(value, str):
                raise ToolError(f"El parámetro '{key}' debe ser string.")
            if expected == "integer" and not isinstance(value, int):
                raise ToolError(f"El parámetro '{key}' debe ser entero.")
            if expected == "boolean" and not isinstance(value, bool):
                raise ToolError(f"El parámetro '{key}' debe ser booleano.")

    async def call(self, name: str, arguments: Optional[Dict[str, Any]]) -> Any:
        tool = self.get(name)
        args = dict(arguments or {})
        self._validate(tool, args)

        # Se filtran argumentos no declarados en la firma del handler.
        sig = inspect.signature(tool.handler)
        accepted = {k: v for k, v in args.items() if k in sig.parameters}

        result = tool.handler(**accepted)
        if inspect.isawaitable(result):
            result = await result
        return result


registry = ToolRegistry()

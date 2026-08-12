"""Utilidades de seguridad: resolución y validación de rutas."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from ..config import settings
from .registry import ToolError


def resolve_path(raw: str, must_exist: bool = False) -> Path:
    """
    Convierte una ruta recibida por una tool en un Path absoluto y seguro.

    - Rutas relativas se resuelven contra MCP_WORKSPACE_ROOT.
    - Si MCP_ALLOW_OUTSIDE_ROOT es False se bloquea cualquier escape del root
      (incluye '..' y symlinks).
    """
    if raw is None or str(raw).strip() == "":
        raise ToolError("La ruta no puede estar vacía.")

    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = settings.workspace_root / candidate

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover
        raise ToolError(f"Ruta inválida '{raw}': {exc}") from exc

    if not settings.allow_outside_root:
        root = settings.workspace_root
        if resolved != root and root not in resolved.parents:
            raise ToolError(
                f"Acceso denegado: '{resolved}' está fuera del workspace permitido "
                f"('{root}'). Ajusta MCP_WORKSPACE_ROOT o MCP_ALLOW_OUTSIDE_ROOT=true."
            )

    if must_exist and not resolved.exists():
        raise ToolError(f"La ruta no existe: {resolved}")

    return resolved


def display_path(path: Path) -> str:
    """Ruta legible (relativa al root cuando es posible)."""
    try:
        return str(path.relative_to(settings.workspace_root)) or "."
    except ValueError:
        return str(path)


_denylist_cache: List[re.Pattern[str]] | None = None


def _denylist() -> List[re.Pattern[str]]:
    global _denylist_cache
    if _denylist_cache is None:
        patterns = [p.strip() for p in settings.command_denylist.split(",") if p.strip()]
        _denylist_cache = [re.compile(p, re.IGNORECASE) for p in patterns]
    return _denylist_cache


def assert_command_allowed(command: str) -> None:
    if not settings.enable_terminal:
        raise ToolError(
            "Las herramientas de terminal están deshabilitadas (MCP_ENABLE_TERMINAL=false)."
        )
    if not command or not command.strip():
        raise ToolError("El comando no puede estar vacío.")
    for pattern in _denylist():
        if pattern.search(command):
            raise ToolError(
                f"Comando bloqueado por la política de seguridad (patrón: {pattern.pattern})."
            )


def truncate(text: str, limit: int | None = None) -> str:
    limit = limit or settings.max_output_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [salida truncada: {len(text) - limit} caracteres omitidos]"

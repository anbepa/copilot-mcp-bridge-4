"""Grupo de herramientas 1: Filesystem (CRUD de archivos)."""
from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from ..config import settings
from ..core.registry import ToolError, registry
from ..core.security import display_path, resolve_path


def _stat_info(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {
        "size_bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }


def _unified_diff(before: str, after: str, name: str) -> str:
    """Genera un diff unificado legible entre dos versiones de un texto."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    )
    return "".join(diff)


# --------------------------------------------------------------------------- #
@registry.tool(
    name="read_file",
    title="Leer archivo",
    description=(
        "Lee el contenido completo de un archivo de texto en formato UTF-8 y lo "
        "devuelve como string. Las rutas relativas se resuelven contra el workspace."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Ruta absoluta o relativa del archivo a leer.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
def read_file(path: str) -> str:
    target = resolve_path(path, must_exist=True)
    if target.is_dir():
        raise ToolError(f"'{display_path(target)}' es un directorio, no un archivo.")
    size = target.stat().st_size
    if size > settings.max_read_bytes:
        raise ToolError(
            f"El archivo pesa {size} bytes y supera el límite "
            f"({settings.max_read_bytes} bytes). Ajusta MCP_MAX_READ_BYTES."
        )
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return target.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
@registry.tool(
    name="write_file",
    title="Escribir archivo",
    description=(
        "Crea o sobrescribe por completo un archivo con el contenido de texto "
        "proporcionado (UTF-8). Crea los directorios padre si no existen."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta del archivo a escribir."},
            "content": {"type": "string", "description": "Contenido de texto completo."},
            "append": {
                "type": "boolean",
                "description": "Si es true añade al final en lugar de sobrescribir. Por defecto false.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)
def write_file(path: str, content: str, append: bool = False) -> Dict[str, Any]:
    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8", newline="") as fh:
        fh.write(content)
    return {
        "status": "ok",
        "operation": "append" if append else "write",
        "path": str(target),
        "bytes_written": len(content.encode("utf-8")),
        **_stat_info(target),
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="edit_file",
    title="Editar archivo por anclas",
    description=(
        "Modifica un archivo aplicando ediciones puntuales por anclas de texto, "
        "sin reescribir el archivo completo. Cada edición reemplaza una aparición "
        "EXACTA y ÚNICA de 'oldText' por 'newText' (respeta espacios e indentación). "
        "Es la forma preferida para cambios pequeños: más segura y barata que "
        "write_file. Usa dryRun=true para previsualizar el diff sin tocar el disco. "
        "Errores accionables: si el ancla no aparece se informa ENOMATCH; si aparece "
        "más de una vez se informa EAMBIGUOUS (amplía 'oldText' con más contexto)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta del archivo a editar."},
            "edits": {
                "type": "array",
                "description": "Lista de ediciones a aplicar en orden.",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {
                            "type": "string",
                            "description": "Texto EXACTO y ÚNICO a reemplazar (con su indentación).",
                        },
                        "newText": {
                            "type": "string",
                            "description": "Texto de reemplazo.",
                        },
                    },
                    "required": ["oldText", "newText"],
                    "additionalProperties": False,
                },
            },
            "dryRun": {
                "type": "boolean",
                "description": "Si es true no escribe en disco; solo devuelve el diff. Por defecto false.",
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
)
def edit_file(path: str, edits: List[Dict[str, str]], dryRun: bool = False) -> Dict[str, Any]:
    target = resolve_path(path, must_exist=True)
    if target.is_dir():
        raise ToolError(f"'{display_path(target)}' es un directorio, no un archivo.")
    if not isinstance(edits, list) or not edits:
        raise ToolError("El campo 'edits' debe ser una lista no vacía de ediciones.")

    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"'{display_path(target)}' no es texto UTF-8; edit_file solo edita texto."
        )

    updated = original
    applied: List[Dict[str, Any]] = []

    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or "oldText" not in edit or "newText" not in edit:
            raise ToolError(
                f"[EFIELDS] La edición #{i + 1} debe tener EXACTAMENTE los campos "
                f"'oldText' y 'newText'. No uses 'search'/'replace' ni 'old'/'new'."
            )
        old_text = edit["oldText"]
        new_text = edit["newText"]
        if old_text == "":
            raise ToolError(
                f"[EEMPTY] La edición #{i + 1} tiene 'oldText' vacío. Para añadir "
                f"contenido usa write_file con append=true."
            )
        count = updated.count(old_text)
        if count == 0:
            preview = old_text[:60].replace("\n", "\\n")
            raise ToolError(
                f"[ENOMATCH] El ancla de la edición #{i + 1} no aparece en el "
                f"archivo: '{preview}'. Verifica el texto EXACTO (incluida la "
                f"indentación y los saltos de línea) leyendo antes con read_file."
            )
        if count > 1:
            preview = old_text[:60].replace("\n", "\\n")
            raise ToolError(
                f"[EAMBIGUOUS] El ancla de la edición #{i + 1} aparece {count} "
                f"veces: '{preview}'. Amplía 'oldText' con más líneas de contexto "
                f"para que sea ÚNICO."
            )
        updated = updated.replace(old_text, new_text, 1)
        applied.append({"index": i + 1, "replaced_chars": len(old_text)})

    diff = _unified_diff(original, updated, display_path(target))

    if dryRun:
        return {
            "status": "ok",
            "operation": "edit",
            "dryRun": True,
            "path": str(target),
            "edits_applied": len(applied),
            "diff": diff,
        }

    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated)

    return {
        "status": "ok",
        "operation": "edit",
        "dryRun": False,
        "path": str(target),
        "edits_applied": len(applied),
        "diff": diff,
        "bytes_written": len(updated.encode("utf-8")),
        **_stat_info(target),
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="create_directory",
    title="Crear directorio",
    description=(
        "Crea un nuevo directorio en la ruta indicada (incluyendo los directorios "
        "padre necesarios). Es idempotente: no falla si ya existe."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta del directorio a crear."}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
def create_directory(path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    already = target.exists()
    if already and not target.is_dir():
        raise ToolError(f"Ya existe un archivo (no directorio) en '{display_path(target)}'.")
    target.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ok",
        "path": str(target),
        "created": not already,
        "message": "Directorio ya existente" if already else "Directorio creado",
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="list_directory",
    title="Listar directorio",
    description=(
        "Lista los archivos y carpetas de un directorio, detallando el tipo "
        "([FILE] / [DIR]), el tamaño en bytes y la fecha de modificación."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Ruta del directorio a listar. Usa '.' para el workspace actual.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
def list_directory(path: str) -> Dict[str, Any]:
    target = resolve_path(path, must_exist=True)
    if not target.is_dir():
        raise ToolError(f"'{display_path(target)}' no es un directorio.")

    entries: List[Dict[str, Any]] = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        entry: Dict[str, Any] = {
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "label": f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}",
            "path": str(item),
        }
        try:
            entry.update(_stat_info(item))
        except OSError:
            entry["size_bytes"] = None
        entries.append(entry)

    return {
        "path": str(target),
        "total": len(entries),
        "directories": sum(1 for e in entries if e["type"] == "directory"),
        "files": sum(1 for e in entries if e["type"] == "file"),
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="move_file",
    title="Mover o renombrar",
    description=(
        "Mueve o renombra un archivo o directorio desde 'source' hacia "
        "'destination'. Falla si el destino ya existe (salvo overwrite=true)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Ruta origen existente."},
            "destination": {"type": "string", "description": "Ruta destino."},
            "overwrite": {
                "type": "boolean",
                "description": "Permite sobrescribir el destino. Por defecto false.",
            },
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    },
)
def move_file(source: str, destination: str, overwrite: bool = False) -> Dict[str, Any]:
    src = resolve_path(source, must_exist=True)
    dst = resolve_path(destination)

    if dst.exists():
        if not overwrite:
            raise ToolError(
                f"El destino '{display_path(dst)}' ya existe. Usa overwrite=true para reemplazarlo."
            )
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {
        "status": "ok",
        "source": str(src),
        "destination": str(dst),
        "type": "directory" if dst.is_dir() else "file",
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="search_nodes",
    title="Buscar nodos",
    description=(
        "Búsqueda recursiva de archivos y directorios cuyo nombre coincide con un "
        "patrón (soporta comodines glob como '*.py' o subcadenas). Opcionalmente "
        "busca también dentro del contenido de los archivos de texto."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directorio raíz de la búsqueda."},
            "query": {
                "type": "string",
                "description": "Patrón a buscar: glob ('*.log') o subcadena ('config').",
            },
            "search_content": {
                "type": "boolean",
                "description": "Si es true busca también dentro del contenido de los archivos.",
            },
            "max_results": {
                "type": "integer",
                "description": "Máximo de coincidencias a devolver (por defecto 500).",
            },
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    },
)
def search_nodes(
    path: str,
    query: str,
    search_content: bool = False,
    max_results: int = 0,
) -> Dict[str, Any]:
    root = resolve_path(path, must_exist=True)
    if not root.is_dir():
        raise ToolError(f"'{display_path(root)}' no es un directorio.")

    limit = max_results if max_results and max_results > 0 else settings.max_search_results
    needle = query.lower()
    is_glob = any(ch in query for ch in "*?[")
    matches: List[Dict[str, Any]] = []
    truncated = False

    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"}

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in list(dirs) + list(files):
            if len(matches) >= limit:
                truncated = True
                break
            full = Path(current) / name
            hit = (
                fnmatch.fnmatch(name.lower(), needle)
                if is_glob
                else needle in name.lower()
            )
            if hit:
                matches.append(
                    {
                        "name": name,
                        "path": str(full),
                        "type": "directory" if full.is_dir() else "file",
                        "match": "name",
                    }
                )
        if len(matches) >= limit:
            truncated = True
            break

        if search_content:
            for name in files:
                if len(matches) >= limit:
                    truncated = True
                    break
                full = Path(current) / name
                try:
                    if full.stat().st_size > 2_000_000:
                        continue
                    text = full.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if needle in line.lower():
                        matches.append(
                            {
                                "name": name,
                                "path": str(full),
                                "type": "file",
                                "match": "content",
                                "line": lineno,
                                "preview": line.strip()[:200],
                            }
                        )
                        break

    return {
        "root": str(root),
        "query": query,
        "mode": "glob" if is_glob else "substring",
        "searched_content": search_content,
        "total": len(matches),
        "truncated": truncated,
        "results": matches,
    }


# --------------------------------------------------------------------------- #
# Herramientas adicionales de robustecimiento
# --------------------------------------------------------------------------- #
@registry.tool(
    name="delete_node",
    title="Eliminar archivo o directorio",
    description=(
        "Elimina un archivo o un directorio. Para directorios no vacíos se debe "
        "indicar recursive=true."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ruta a eliminar."},
            "recursive": {
                "type": "boolean",
                "description": "Elimina directorios con contenido. Por defecto false.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
def delete_node(path: str, recursive: bool = False) -> Dict[str, Any]:
    target = resolve_path(path, must_exist=True)
    if target == settings.workspace_root:
        raise ToolError("No se permite eliminar la raíz del workspace.")
    if target.is_dir():
        if any(target.iterdir()) and not recursive:
            raise ToolError(
                f"El directorio '{display_path(target)}' no está vacío. Usa recursive=true."
            )
        shutil.rmtree(target)
        kind = "directory"
    else:
        target.unlink()
        kind = "file"
    return {"status": "ok", "deleted": str(target), "type": kind}


@registry.tool(
    name="get_file_info",
    title="Información de un nodo",
    description="Devuelve metadatos detallados de un archivo o directorio (tamaño, fechas, permisos).",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Ruta a inspeccionar."}},
        "required": ["path"],
        "additionalProperties": False,
    },
)
def get_file_info(path: str) -> Dict[str, Any]:
    target = resolve_path(path, must_exist=True)
    st = target.stat()
    return {
        "path": str(target),
        "name": target.name,
        "type": "directory" if target.is_dir() else "file",
        "size_bytes": st.st_size,
        "created": datetime.fromtimestamp(st.st_ctime, timezone.utc).isoformat(),
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "accessed": datetime.fromtimestamp(st.st_atime, timezone.utc).isoformat(),
        "permissions": oct(st.st_mode & 0o777),
        "is_symlink": target.is_symlink(),
    }

"""
Grupo de herramientas 3: Browser (automatización web con Playwright).

Port a Python del catálogo de tools de Playwright MCP, adaptado al registry de
este servidor. Las tools operan sobre una única sesión de navegador compartida
(`server.browser.session.session`) y usan referencias (`ref=eN`) obtenidas con
`browser_snapshot`, evitando depender de capturas de pantalla.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..browser.session import HIGHLIGHT_JS, UNHIGHLIGHT_JS, now_stamp, session
from ..config import settings
from ..core.registry import ToolError, registry
from ..core.security import truncate

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_REF_DESC = (
    "Referencia exacta del elemento tomada del snapshot (p.ej. 'e12' o 'ref=e12'), "
    "o un selector CSS único."
)
_ELEMENT_DESC = "Descripción legible del elemento (para trazabilidad de la acción)."


def _str_prop(desc: str) -> Dict[str, str]:
    return {"type": "string", "description": desc}


async def _act(loc_target: str, action: str, **kwargs: Any) -> Any:
    loc = await session.locator(loc_target)
    return await getattr(loc, action)(**kwargs)


async def _page_state(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    page = await session.page()
    state: Dict[str, Any] = {"url": page.url, "title": await page.title()}
    if extra:
        state.update(extra)
    return state


def _write_or_return(content: str, filename: Optional[str], prefix: str, ext: str) -> Dict[str, Any]:
    if filename:
        path = session.resolve_output(filename, ext, prefix)
        path.write_text(content, encoding="utf-8")
        return {"savedTo": str(path), "bytes": len(content.encode("utf-8"))}
    return {"content": truncate(content)}


# =========================================================================== #
#  CORE AUTOMATION
# =========================================================================== #
@registry.tool(
    name="browser_navigate",
    title="Navigate to a URL",
    description="Navega el navegador a una URL. Arranca el navegador si aún no está abierto.",
    input_schema={
        "type": "object",
        "properties": {"url": _str_prop("La URL a la que navegar.")},
        "required": ["url"],
        "additionalProperties": False,
    },
)
async def browser_navigate(url: str) -> Dict[str, Any]:
    page = await session.page()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) and not url.startswith("about:"):
        url = "https://" + url
    response = await page.goto(url, wait_until=settings.browser_wait_until)
    return await _page_state(
        {"status": getattr(response, "status", None) if response else None, "navigatedTo": url}
    )


@registry.tool(
    name="browser_navigate_back",
    title="Go back",
    description="Vuelve a la página anterior del historial.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_navigate_back() -> Dict[str, Any]:
    page = await session.page()
    await page.go_back(wait_until=settings.browser_wait_until)
    return await _page_state()


@registry.tool(
    name="browser_close",
    title="Close browser",
    description="Cierra la página y libera el navegador junto con todos sus recursos.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_close() -> Dict[str, Any]:
    return await session.close()


@registry.tool(
    name="browser_snapshot",
    title="Page snapshot",
    description=(
        "Captura el snapshot de accesibilidad de la página actual y asigna una "
        "referencia [ref=eN] a cada elemento. Es preferible a una captura de "
        "pantalla: es el insumo para click, type, hover, etc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": _str_prop("Limita el snapshot a un elemento concreto (ref o selector)."),
            "filename": _str_prop("Guarda el snapshot en un archivo en lugar de devolverlo."),
            "depth": {"type": "integer", "description": "Profundidad máxima del árbol."},
            "boxes": {
                "type": "boolean",
                "description": "Incluye el bounding box [box=x,y,w,h] de cada elemento.",
            },
        },
        "additionalProperties": False,
    },
)
async def browser_snapshot(
    target: Optional[str] = None,
    filename: Optional[str] = None,
    depth: Optional[int] = None,
    boxes: bool = False,
) -> Dict[str, Any]:
    data = await session.snapshot(depth=depth, boxes=boxes, target=target)
    if filename:
        path = session.resolve_output(filename, ".md", "snapshot")
        path.write_text(data["snapshot"], encoding="utf-8")
        data = {**data, "snapshot": None, "savedTo": str(path)}
    else:
        data["snapshot"] = truncate(data["snapshot"])
    return data


@registry.tool(
    name="browser_find",
    title="Find in page snapshot",
    description=(
        "Busca texto o una expresión regular dentro del snapshot de accesibilidad "
        "y devuelve sólo los nodos coincidentes con su ref. Mucho más económico "
        "en tokens que capturar el snapshot completo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": _str_prop("Texto plano a buscar (coincidencia parcial, sin distinguir mayúsculas)."),
            "regex": _str_prop("Expresión regular. Usa '/patron/i' para no distinguir mayúsculas."),
            "context": {"type": "integer", "description": "Líneas de contexto alrededor (def. 2)."},
        },
        "additionalProperties": False,
    },
)
async def browser_find(
    text: Optional[str] = None, regex: Optional[str] = None, context: int = 2
) -> Dict[str, Any]:
    if bool(text) == bool(regex):
        raise ToolError("Proporciona exactamente uno de 'text' o 'regex'.")
    data = await session.snapshot()
    lines = data["snapshot"].splitlines()

    if regex:
        pattern = regex
        flags = 0
        m = re.match(r"^/(.*)/([a-z]*)$", regex)
        if m:
            pattern, mods = m.group(1), m.group(2)
            if "i" in mods:
                flags |= re.IGNORECASE
            if "m" in mods:
                flags |= re.MULTILINE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"Expresión regular inválida: {exc}") from exc
        hits = [i for i, l in enumerate(lines) if compiled.search(l)]
    else:
        needle = str(text).lower()
        hits = [i for i, l in enumerate(lines) if needle in l.lower()]

    matches: List[Dict[str, Any]] = []
    for i in hits[: settings.max_search_results]:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        ref = re.search(r"\[ref=(e\d+)\]", lines[i])
        matches.append(
            {
                "line": i + 1,
                "ref": ref.group(1) if ref else None,
                "match": lines[i].strip(),
                "context": "\n".join(lines[lo:hi]),
            }
        )
    return {"url": data["url"], "query": text or regex, "total": len(hits), "matches": matches}


@registry.tool(
    name="browser_click",
    title="Click",
    description="Hace clic sobre un elemento de la página.",
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC),
            "doubleClick": {"type": "boolean", "description": "Doble clic en lugar de clic simple."},
            "button": _str_prop("Botón del ratón: left | right | middle. Por defecto left."),
            "modifiers": {
                "type": "array",
                "description": "Teclas modificadoras: Alt, Control, Meta, Shift.",
                "items": {"type": "string"},
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    },
)
async def browser_click(
    target: str,
    element: Optional[str] = None,
    doubleClick: bool = False,  # noqa: N803
    button: str = "left",
    modifiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if button not in ("left", "right", "middle"):
        raise ToolError("'button' debe ser left, right o middle.")
    kwargs: Dict[str, Any] = {"button": button}
    if modifiers:
        kwargs["modifiers"] = modifiers
    await _act(target, "dblclick" if doubleClick else "click", **kwargs)
    return await _page_state(
        {"action": "doubleClick" if doubleClick else "click", "target": target, "element": element}
    )


@registry.tool(
    name="browser_hover",
    title="Hover mouse",
    description="Sitúa el puntero sobre un elemento de la página.",
    input_schema={
        "type": "object",
        "properties": {"element": _str_prop(_ELEMENT_DESC), "target": _str_prop(_REF_DESC)},
        "required": ["target"],
        "additionalProperties": False,
    },
)
async def browser_hover(target: str, element: Optional[str] = None) -> Dict[str, Any]:
    await _act(target, "hover")
    return await _page_state({"action": "hover", "target": target})


@registry.tool(
    name="browser_type",
    title="Type text",
    description="Escribe texto dentro de un elemento editable.",
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC),
            "text": _str_prop("Texto a escribir en el elemento."),
            "submit": {"type": "boolean", "description": "Pulsa Enter al terminar."},
            "slowly": {
                "type": "boolean",
                "description": "Escribe carácter a carácter (dispara manejadores de teclado).",
            },
        },
        "required": ["target", "text"],
        "additionalProperties": False,
    },
)
async def browser_type(
    target: str,
    text: str,
    element: Optional[str] = None,
    submit: bool = False,
    slowly: bool = False,
) -> Dict[str, Any]:
    loc = await session.locator(target)
    if slowly:
        await loc.click()
        await loc.press_sequentially(text, delay=30)
    else:
        await loc.fill(text)
    if submit:
        await loc.press("Enter")
    return await _page_state({"action": "type", "target": target, "submitted": submit})


@registry.tool(
    name="browser_fill_form",
    title="Fill form",
    description=(
        "Rellena varios campos de formulario en una sola llamada. Cada campo "
        "indica su tipo: textbox, checkbox, radio, combobox o slider."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "description": (
                    "Lista de campos. Cada elemento: {name, target, type, value}. "
                    "'type' ∈ textbox|checkbox|radio|combobox|slider."
                ),
                "items": {"type": "object"},
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    },
)
async def browser_fill_form(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(fields, list) or not fields:
        raise ToolError("'fields' debe ser una lista no vacía de campos.")
    results: List[Dict[str, Any]] = []
    for idx, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ToolError(f"El campo #{idx + 1} debe ser un objeto.")
        target = field.get("target") or field.get("ref")
        if not target:
            raise ToolError(f"El campo #{idx + 1} no indica 'target'.")
        ftype = (field.get("type") or "textbox").lower()
        value = field.get("value")
        loc = await session.locator(target)
        try:
            if ftype in ("checkbox", "radio"):
                truthy = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "on")
                await (loc.check() if truthy else loc.uncheck())
            elif ftype == "combobox":
                await loc.select_option(str(value))
            elif ftype == "slider":
                await loc.fill(str(value))
            else:
                await loc.fill("" if value is None else str(value))
            results.append({"target": target, "type": ftype, "status": "ok"})
        except Exception as exc:  # noqa: BLE001
            results.append({"target": target, "type": ftype, "status": "error", "error": str(exc)})
    return await _page_state({"filled": len(results), "fields": results})


@registry.tool(
    name="browser_select_option",
    title="Select option",
    description="Selecciona una o varias opciones en un desplegable (<select>).",
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC),
            "values": {
                "type": "array",
                "description": "Valores a seleccionar (uno o varios).",
                "items": {"type": "string"},
            },
        },
        "required": ["target", "values"],
        "additionalProperties": False,
    },
)
async def browser_select_option(
    target: str, values: List[str], element: Optional[str] = None
) -> Dict[str, Any]:
    if not isinstance(values, list) or not values:
        raise ToolError("'values' debe ser una lista con al menos un valor.")
    loc = await session.locator(target)
    selected = await loc.select_option([str(v) for v in values])
    return await _page_state({"action": "selectOption", "target": target, "selected": selected})


@registry.tool(
    name="browser_press_key",
    title="Press a key",
    description="Pulsa una tecla del teclado, p.ej. 'ArrowLeft', 'Enter' o el carácter 'a'.",
    input_schema={
        "type": "object",
        "properties": {"key": _str_prop("Nombre de la tecla o carácter a generar.")},
        "required": ["key"],
        "additionalProperties": False,
    },
)
async def browser_press_key(key: str) -> Dict[str, Any]:
    page = await session.page()
    await page.keyboard.press(key)
    return await _page_state({"action": "pressKey", "key": key})


@registry.tool(
    name="browser_drag",
    title="Drag mouse",
    description="Arrastra y suelta desde un elemento origen hasta un elemento destino.",
    input_schema={
        "type": "object",
        "properties": {
            "startElement": _str_prop("Descripción legible del elemento origen."),
            "startTarget": _str_prop("Referencia o selector del elemento origen."),
            "endElement": _str_prop("Descripción legible del elemento destino."),
            "endTarget": _str_prop("Referencia o selector del elemento destino."),
        },
        "required": ["startTarget", "endTarget"],
        "additionalProperties": False,
    },
)
async def browser_drag(
    startTarget: str,  # noqa: N803
    endTarget: str,  # noqa: N803
    startElement: Optional[str] = None,  # noqa: N803
    endElement: Optional[str] = None,  # noqa: N803
) -> Dict[str, Any]:
    source = await session.locator(startTarget)
    destination = await session.locator(endTarget)
    await source.drag_to(destination)
    return await _page_state({"action": "drag", "from": startTarget, "to": endTarget})


@registry.tool(
    name="browser_drop",
    title="Drop files or data onto an element",
    description=(
        "Suelta archivos o datos con tipo MIME sobre un elemento, como si se "
        "arrastraran desde fuera de la página. Requiere 'paths' o 'data'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC),
            "paths": {
                "type": "array",
                "description": "Rutas absolutas de los archivos a soltar.",
                "items": {"type": "string"},
            },
            "data": {
                "type": "object",
                "description": 'Datos por tipo MIME, p.ej. {"text/plain": "hola"}.',
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    },
)
async def browser_drop(
    target: str,
    element: Optional[str] = None,
    paths: Optional[List[str]] = None,
    data: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not paths and not data:
        raise ToolError("Debes proporcionar al menos uno de 'paths' o 'data'.")
    page = await session.page()
    loc = await session.locator(target)
    if paths:
        for p in paths:
            if not Path(p).exists():
                raise ToolError(f"El archivo a soltar no existe: {p}")
        await loc.set_input_files(paths)
        return await _page_state({"action": "drop", "target": target, "files": paths})

    payload = await page.evaluate_handle(
        """(items) => {
            const dt = new DataTransfer();
            for (const [mime, value] of Object.entries(items)) dt.setData(mime, value);
            return dt;
        }""",
        data,
    )
    await loc.dispatch_event("drop", {"dataTransfer": payload})
    return await _page_state({"action": "drop", "target": target, "data": list(data or {})})


@registry.tool(
    name="browser_file_upload",
    title="Upload files",
    description=(
        "Sube uno o varios archivos al selector de archivos activo. Si se omite "
        "'paths', se cancela el diálogo de selección."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "description": "Rutas absolutas de los archivos a subir.",
                "items": {"type": "string"},
            },
            "target": _str_prop("Input[type=file] destino (ref o selector). Recomendado."),
        },
        "additionalProperties": False,
    },
)
async def browser_file_upload(
    paths: Optional[List[str]] = None, target: Optional[str] = None
) -> Dict[str, Any]:
    page = await session.page()
    if not paths:
        return await _page_state({"action": "fileUpload", "cancelled": True})
    for p in paths:
        if not Path(p).exists():
            raise ToolError(f"El archivo a subir no existe: {p}")
    loc = await session.locator(target) if target else page.locator("input[type=file]").first
    await loc.set_input_files(paths)
    return await _page_state({"action": "fileUpload", "files": paths})


@registry.tool(
    name="browser_handle_dialog",
    title="Handle a dialog",
    description="Acepta o descarta el diálogo pendiente (alert, confirm, prompt o beforeunload).",
    input_schema={
        "type": "object",
        "properties": {
            "accept": {"type": "boolean", "description": "true para aceptar, false para descartar."},
            "promptText": _str_prop("Texto a introducir si el diálogo es de tipo prompt."),
        },
        "required": ["accept"],
        "additionalProperties": False,
    },
)
async def browser_handle_dialog(accept: bool, promptText: Optional[str] = None) -> Dict[str, Any]:  # noqa: N803
    dialog = session.pending_dialog
    if dialog is None:
        raise ToolError("No hay ningún diálogo pendiente en la página.")
    info = {"type": dialog.type, "message": dialog.message}
    if accept:
        if promptText is not None:
            await dialog.accept(promptText)
        else:
            await dialog.accept()
    else:
        await dialog.dismiss()
    session.pending_dialog = None
    return {"handled": True, "accepted": accept, "dialog": info}


@registry.tool(
    name="browser_evaluate",
    title="Evaluate JavaScript",
    description=(
        "Evalúa una expresión JavaScript en la página o sobre un elemento. "
        "La función debe tener la forma '() => {...}' o '(element) => {...}'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC + " Si se indica, se pasa como argumento a la función."),
            "function": _str_prop("Función JS: () => { ... } o (element) => { ... }"),
            "filename": _str_prop("Guarda el resultado en un archivo en lugar de devolverlo."),
        },
        "required": ["function"],
        "additionalProperties": False,
    },
)
async def browser_evaluate(
    function: str,
    target: Optional[str] = None,
    element: Optional[str] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    page = await session.page()
    if target:
        loc = await session.locator(target)
        result = await loc.evaluate(function)
    else:
        result = await page.evaluate(function)
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return {"url": page.url, **_write_or_return(text, filename, "evaluate", ".txt")}


@registry.tool(
    name="browser_run_code_unsafe",
    title="Run Playwright code (unsafe)",
    description=(
        "Ejecuta un fragmento de código Playwright (Python async) con acceso a "
        "'page', 'context' y 'session'. INSEGURO: equivale a ejecución remota de "
        "código. Deshabilitado por defecto (MCP_ENABLE_UNSAFE_BROWSER_CODE=true)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": _str_prop(
                "Cuerpo de una función async con 'page' disponible. "
                "Ej.: await page.goto('https://x.com'); return await page.title()"
            ),
            "filename": _str_prop("Carga el código desde este archivo (prevalece sobre 'code')."),
        },
        "additionalProperties": False,
    },
)
async def browser_run_code_unsafe(
    code: Optional[str] = None, filename: Optional[str] = None
) -> Dict[str, Any]:
    if not settings.enable_unsafe_browser_code:
        raise ToolError(
            "browser_run_code_unsafe está deshabilitado por seguridad. "
            "Actívalo con MCP_ENABLE_UNSAFE_BROWSER_CODE=true sólo en entornos de confianza."
        )
    if filename:
        path = Path(filename)
        if not path.is_absolute():
            path = settings.workspace_root / path
        if not path.exists():
            raise ToolError(f"No existe el archivo de código: {path}")
        code = path.read_text(encoding="utf-8")
    if not code or not code.strip():
        raise ToolError("Debes proporcionar 'code' o 'filename'.")

    page = await session.page()
    context = await session.context()
    body = "\n".join("    " + line for line in code.splitlines())
    wrapper = f"async def __mcp_snippet(page, context, session):\n{body}\n"
    namespace: Dict[str, Any] = {}
    try:
        exec(compile(wrapper, "<browser_run_code_unsafe>", "exec"), namespace)  # noqa: S102
        result = await namespace["__mcp_snippet"](page, context, session)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
    return {
        "url": page.url,
        "result": result if isinstance(result, (str, int, float, bool, type(None), list, dict)) else str(result),
    }


@registry.tool(
    name="browser_wait_for",
    title="Wait for",
    description="Espera a que aparezca un texto, desaparezca un texto o transcurra un tiempo.",
    input_schema={
        "type": "object",
        "properties": {
            "time": {"type": "number", "description": "Segundos a esperar."},
            "text": _str_prop("Texto que debe aparecer en la página."),
            "textGone": _str_prop("Texto que debe desaparecer de la página."),
        },
        "additionalProperties": False,
    },
)
async def browser_wait_for(
    time: Optional[float] = None,
    text: Optional[str] = None,
    textGone: Optional[str] = None,  # noqa: N803
) -> Dict[str, Any]:
    if time is None and not text and not textGone:
        raise ToolError("Indica al menos uno de 'time', 'text' o 'textGone'.")
    page = await session.page()
    waited: List[str] = []
    if text:
        await page.get_by_text(text).first.wait_for(state="visible", timeout=settings.browser_timeout_ms)
        waited.append(f"apareció '{text}'")
    if textGone:
        await page.get_by_text(textGone).first.wait_for(state="hidden", timeout=settings.browser_timeout_ms)
        waited.append(f"desapareció '{textGone}'")
    if time is not None:
        await asyncio.sleep(min(float(time), settings.browser_timeout_ms / 1000))
        waited.append(f"{time}s")
    return await _page_state({"waitedFor": waited})


@registry.tool(
    name="browser_resize",
    title="Resize browser window",
    description="Cambia el tamaño del viewport del navegador.",
    input_schema={
        "type": "object",
        "properties": {
            "width": {"type": "integer", "description": "Ancho en píxeles."},
            "height": {"type": "integer", "description": "Alto en píxeles."},
        },
        "required": ["width", "height"],
        "additionalProperties": False,
    },
)
async def browser_resize(width: int, height: int) -> Dict[str, Any]:
    page = await session.page()
    await page.set_viewport_size({"width": int(width), "height": int(height)})
    return await _page_state({"viewport": {"width": int(width), "height": int(height)}})


@registry.tool(
    name="browser_take_screenshot",
    title="Take a screenshot",
    description=(
        "Captura una imagen de la página o de un elemento. Para actuar sobre la "
        "página usa browser_snapshot, no la captura."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC + " Captura sólo ese elemento."),
            "type": _str_prop("Formato: png | jpeg. Por defecto png."),
            "filename": _str_prop("Nombre del archivo destino (relativo al directorio de salida)."),
            "fullPage": {
                "type": "boolean",
                "description": "Captura la página completa con scroll (incompatible con 'target').",
            },
        },
        "additionalProperties": False,
    },
)
async def browser_take_screenshot(
    element: Optional[str] = None,
    target: Optional[str] = None,
    type: str = "png",  # noqa: A002
    filename: Optional[str] = None,
    fullPage: bool = False,  # noqa: N803
) -> Dict[str, Any]:
    if fullPage and target:
        raise ToolError("'fullPage' no puede combinarse con la captura de un elemento ('target').")
    fmt = (type or "png").lower()
    if fmt not in ("png", "jpeg"):
        raise ToolError("'type' debe ser png o jpeg.")
    path = session.resolve_output(filename, f".{fmt}", "page")
    if target:
        loc = await session.locator(target)
        await loc.screenshot(path=str(path), type=fmt)
    else:
        page = await session.page()
        await page.screenshot(path=str(path), type=fmt, full_page=fullPage)
    return await _page_state(
        {"savedTo": str(path), "format": fmt, "fullPage": fullPage, "bytes": path.stat().st_size}
    )


@registry.tool(
    name="browser_console_messages",
    title="Get console messages",
    description="Devuelve los mensajes de consola capturados en la página.",
    input_schema={
        "type": "object",
        "properties": {
            "level": _str_prop("Nivel mínimo: error | warning | info | log. Por defecto info."),
            "all": {
                "type": "boolean",
                "description": "Todos los mensajes de la sesión, no sólo desde la última navegación.",
            },
            "filename": _str_prop("Guarda los mensajes en un archivo en lugar de devolverlos."),
        },
        "additionalProperties": False,
    },
)
async def browser_console_messages(
    level: str = "info", all: bool = False, filename: Optional[str] = None  # noqa: A002
) -> Dict[str, Any]:
    order = {"error": 0, "warning": 1, "warn": 1, "info": 2, "log": 3, "debug": 4}
    threshold = order.get((level or "info").lower(), 2)
    source = session.console_messages if all else session.console_messages[session.nav_console_mark:]
    filtered = [m for m in source if order.get(str(m["level"]).lower(), 3) <= threshold]
    if filename:
        text = "\n".join(f"[{m['level']}] {m['text']}" for m in filtered)
        return {"total": len(filtered), **_write_or_return(text, filename, "console", ".log")}
    return {"total": len(filtered), "level": level, "messages": filtered}


@registry.tool(
    name="browser_network_requests",
    title="List network requests",
    description=(
        "Lista numerada de las peticiones de red desde la carga de la página. "
        "Usa browser_network_request con el número para ver el detalle."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "static": {
                "type": "boolean",
                "description": "Incluye recursos estáticos (imágenes, fuentes, scripts). Def. false.",
            },
            "filter": _str_prop("Sólo peticiones cuya URL case con esta expresión regular."),
            "filename": _str_prop("Guarda el listado en un archivo."),
        },
        "additionalProperties": False,
    },
)
async def browser_network_requests(
    static: bool = False, filter: Optional[str] = None, filename: Optional[str] = None  # noqa: A002
) -> Dict[str, Any]:
    STATIC = {"image", "font", "stylesheet", "media", "script"}
    entries = list(session.network_requests)
    if not static:
        entries = [e for e in entries if e["resourceType"] not in STATIC]
    if filter:
        try:
            rx = re.compile(filter)
        except re.error as exc:
            raise ToolError(f"Filtro regex inválido: {exc}") from exc
        entries = [e for e in entries if rx.search(e["url"])]
    listing = [
        {
            "index": i + 1,
            "method": e["method"],
            "url": e["url"],
            "resourceType": e["resourceType"],
            "status": e["status"],
        }
        for i, e in enumerate(entries)
    ]
    if filename:
        text = "\n".join(f"{r['index']}. [{r['status']}] {r['method']} {r['url']}" for r in listing)
        return {"total": len(listing), **_write_or_return(text, filename, "network", ".log")}
    return {"total": len(listing), "requests": listing}


@registry.tool(
    name="browser_network_request",
    title="Show network request details",
    description=(
        "Detalle completo (cabeceras y cuerpo) de una petición de red concreta, "
        "usando el número que devuelve browser_network_requests."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Índice 1-based de la petición."},
            "part": _str_prop("Devuelve sólo una parte: request | response | headers | body."),
            "filename": _str_prop("Guarda el resultado en un archivo."),
        },
        "required": ["index"],
        "additionalProperties": False,
    },
)
async def browser_network_request(
    index: int, part: Optional[str] = None, filename: Optional[str] = None
) -> Dict[str, Any]:
    entries = session.network_requests
    if index < 1 or index > len(entries):
        raise ToolError(
            f"Índice fuera de rango: {index}. Hay {len(entries)} peticiones registradas."
        )
    entry = entries[index - 1]
    request, response = entry["request"], entry["response"]
    detail: Dict[str, Any] = {
        "index": index,
        "request": {
            "method": entry["method"],
            "url": entry["url"],
            "resourceType": entry["resourceType"],
            "headers": await request.all_headers() if request else {},
            "postData": getattr(request, "post_data", None),
        },
        "response": None,
    }
    if response is not None:
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001
            body = "<cuerpo binario o no disponible>"
        detail["response"] = {
            "status": entry["status"],
            "headers": await response.all_headers(),
            "body": truncate(body, 50_000),
        }
    if part:
        key = part.lower()
        if key in ("request", "response"):
            detail = {"index": index, key: detail[key]}
        elif key == "headers":
            detail = {"index": index, "headers": detail["request"]["headers"]}
        elif key == "body":
            detail = {"index": index, "body": (detail.get("response") or {}).get("body")}
        else:
            raise ToolError("'part' debe ser request, response, headers o body.")
    if filename:
        text = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
        return _write_or_return(text, filename, "request", ".json")
    return detail


# =========================================================================== #
#  TAB MANAGEMENT
# =========================================================================== #
@registry.tool(
    name="browser_tabs",
    title="Manage tabs",
    description="Lista, crea, cierra o selecciona una pestaña del navegador.",
    input_schema={
        "type": "object",
        "properties": {
            "action": _str_prop("Operación: list | new | close | select."),
            "index": {"type": "integer", "description": "Índice de pestaña para close/select."},
            "url": _str_prop("URL a abrir cuando action=new."),
        },
        "required": ["action"],
        "additionalProperties": False,
    },
)
async def browser_tabs(
    action: str, index: Optional[int] = None, url: Optional[str] = None
) -> Dict[str, Any]:
    act = (action or "").lower()
    context = await session.context()

    async def listing() -> List[Dict[str, Any]]:
        out = []
        for i, p in enumerate(context.pages):
            out.append({"index": i, "url": p.url, "title": await p.title(), "closed": p.is_closed()})
        return out

    if act == "list":
        return {"action": "list", "total": len(context.pages), "tabs": await listing()}
    if act == "new":
        page = await session.new_page(url)
        return {"action": "new", "index": len(context.pages) - 1, "url": page.url, "tabs": await listing()}
    if act == "select":
        if index is None:
            raise ToolError("'index' es obligatorio para action=select.")
        page = await session.select_page(int(index))
        return {"action": "select", "index": int(index), "url": page.url}
    if act == "close":
        pages = context.pages
        target_index = int(index) if index is not None else pages.index(await session.page())
        if target_index < 0 or target_index >= len(pages):
            raise ToolError(f"Índice de pestaña fuera de rango: {target_index}.")
        await pages[target_index].close()
        remaining = context.pages
        if remaining:
            session.set_page(remaining[-1])
        return {"action": "close", "closedIndex": target_index, "remaining": len(remaining)}
    raise ToolError("'action' debe ser list, new, close o select.")


# =========================================================================== #
#  CONFIGURATION / INSTALLATION
# =========================================================================== #
@registry.tool(
    name="browser_get_config",
    title="Get config",
    description="Devuelve la configuración efectiva del navegador tras aplicar variables de entorno.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_get_config() -> Dict[str, Any]:
    try:
        import playwright  # type: ignore

        installed = True
        version = getattr(playwright, "__version__", "desconocida")
    except ImportError:
        installed = False
        version = None
    return {
        "playwrightInstalled": installed,
        "playwrightVersion": version,
        "enabled": settings.enable_browser,
        "engine": settings.browser_engine,
        "headless": settings.browser_headless,
        "viewport": {
            "width": settings.browser_viewport_width,
            "height": settings.browser_viewport_height,
        },
        "timeoutMs": settings.browser_timeout_ms,
        "waitUntil": settings.browser_wait_until,
        "userAgent": settings.browser_user_agent or None,
        "executablePath": settings.browser_executable_path or None,
        "outputDir": str(session.output_dir()),
        "unsafeCodeEnabled": settings.enable_unsafe_browser_code,
        "browserOpen": session.started,
    }


@registry.tool(
    name="browser_install",
    title="Install browser",
    description=(
        "Instala el navegador de Playwright requerido (descarga el binario). "
        "Úsalo si una tool falla indicando que el navegador no está instalado."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "engine": _str_prop("Motor a instalar: chromium | firefox | webkit. Def. el configurado.")
        },
        "additionalProperties": False,
    },
)
async def browser_install(engine: Optional[str] = None) -> Dict[str, Any]:
    target = (engine or settings.browser_engine).lower()
    if target not in ("chromium", "firefox", "webkit"):
        raise ToolError("'engine' debe ser chromium, firefox o webkit.")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", target,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError("La instalación del navegador superó los 900s.")
    output = stdout.decode("utf-8", errors="replace")
    return {
        "engine": target,
        "exitCode": proc.returncode,
        "success": proc.returncode == 0,
        "output": truncate(output, 20_000),
    }


# =========================================================================== #
#  NETWORK (mocking / estado)
# =========================================================================== #
@registry.tool(
    name="browser_network_state_set",
    title="Set network state",
    description="Pone el navegador en modo offline u online.",
    input_schema={
        "type": "object",
        "properties": {"state": _str_prop("'offline' u 'online'.")},
        "required": ["state"],
        "additionalProperties": False,
    },
)
async def browser_network_state_set(state: str) -> Dict[str, Any]:
    value = (state or "").lower()
    if value not in ("online", "offline"):
        raise ToolError("'state' debe ser 'online' u 'offline'.")
    context = await session.context()
    await context.set_offline(value == "offline")
    return {"state": value, "offline": value == "offline"}


@registry.tool(
    name="browser_route",
    title="Mock network requests",
    description="Intercepta las peticiones que casen con un patrón y devuelve una respuesta simulada.",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": _str_prop('Patrón de URL, p.ej. "**/api/users" o "**/*.png".'),
            "status": {"type": "integer", "description": "Código HTTP a devolver (def. 200)."},
            "body": _str_prop("Cuerpo de la respuesta (texto o JSON serializado)."),
            "contentType": _str_prop('Content-Type, p.ej. "application/json".'),
            "headers": {
                "type": "array",
                "description": 'Cabeceras extra en formato "Nombre: Valor".',
                "items": {"type": "string"},
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
)
async def browser_route(
    pattern: str,
    status: int = 200,
    body: Optional[str] = None,
    contentType: Optional[str] = None,  # noqa: N803
    headers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    context = await session.context()
    extra: Dict[str, str] = {}
    for raw in headers or []:
        if ":" not in raw:
            raise ToolError(f'Cabecera inválida: "{raw}". Usa el formato "Nombre: Valor".')
        name, _, value = raw.partition(":")
        extra[name.strip()] = value.strip()

    async def handler(route: Any, request: Any) -> None:  # noqa: ARG001
        await route.fulfill(
            status=status,
            body=body or "",
            content_type=contentType or "text/plain",
            headers=extra or None,
        )

    await context.route(pattern, handler)
    session.routes.append(
        {"pattern": pattern, "status": status, "contentType": contentType, "handler": handler}
    )
    return {"status": "routed", "pattern": pattern, "mockStatus": status, "activeRoutes": len(session.routes)}


@registry.tool(
    name="browser_route_list",
    title="List network routes",
    description="Lista todas las rutas de red simuladas que están activas.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_route_list() -> Dict[str, Any]:
    return {
        "total": len(session.routes),
        "routes": [
            {"pattern": r["pattern"], "status": r["status"], "contentType": r["contentType"]}
            for r in session.routes
        ],
    }


@registry.tool(
    name="browser_unroute",
    title="Remove network routes",
    description="Elimina las rutas simuladas que casen con un patrón, o todas si se omite.",
    input_schema={
        "type": "object",
        "properties": {"pattern": _str_prop("Patrón a eliminar. Omítelo para eliminar todas.")},
        "additionalProperties": False,
    },
)
async def browser_unroute(pattern: Optional[str] = None) -> Dict[str, Any]:
    context = await session.context()
    removed = 0
    remaining = []
    for route in session.routes:
        if pattern is None or route["pattern"] == pattern:
            try:
                await context.unroute(route["pattern"], route["handler"])
            except Exception:  # noqa: BLE001
                pass
            removed += 1
        else:
            remaining.append(route)
    session.routes = remaining
    return {"removed": removed, "remaining": len(remaining)}


# =========================================================================== #
#  STORAGE: cookies
# =========================================================================== #
@registry.tool(
    name="browser_cookie_list",
    title="List cookies",
    description="Lista todas las cookies, opcionalmente filtradas por dominio y/o path.",
    input_schema={
        "type": "object",
        "properties": {
            "domain": _str_prop("Filtra por dominio."),
            "path": _str_prop("Filtra por path."),
        },
        "additionalProperties": False,
    },
)
async def browser_cookie_list(
    domain: Optional[str] = None, path: Optional[str] = None
) -> Dict[str, Any]:
    context = await session.context()
    cookies = await context.cookies()
    if domain:
        cookies = [c for c in cookies if domain in (c.get("domain") or "")]
    if path:
        cookies = [c for c in cookies if (c.get("path") or "") == path]
    return {"total": len(cookies), "cookies": cookies}


@registry.tool(
    name="browser_cookie_get",
    title="Get cookie",
    description="Obtiene una cookie concreta por su nombre.",
    input_schema={
        "type": "object",
        "properties": {"name": _str_prop("Nombre de la cookie.")},
        "required": ["name"],
        "additionalProperties": False,
    },
)
async def browser_cookie_get(name: str) -> Dict[str, Any]:
    context = await session.context()
    for cookie in await context.cookies():
        if cookie.get("name") == name:
            return {"found": True, "cookie": cookie}
    return {"found": False, "cookie": None, "name": name}


@registry.tool(
    name="browser_cookie_set",
    title="Set cookie",
    description="Crea una cookie con banderas opcionales (domain, path, expires, httpOnly, secure, sameSite).",
    input_schema={
        "type": "object",
        "properties": {
            "name": _str_prop("Nombre de la cookie."),
            "value": _str_prop("Valor de la cookie."),
            "domain": _str_prop("Dominio de la cookie."),
            "path": _str_prop("Path de la cookie."),
            "expires": {"type": "number", "description": "Expiración como timestamp Unix."},
            "httpOnly": {"type": "boolean", "description": "Marca la cookie como HttpOnly."},
            "secure": {"type": "boolean", "description": "Marca la cookie como Secure."},
            "sameSite": _str_prop("Atributo SameSite: Strict | Lax | None."),
        },
        "required": ["name", "value"],
        "additionalProperties": False,
    },
)
async def browser_cookie_set(
    name: str,
    value: str,
    domain: Optional[str] = None,
    path: Optional[str] = None,
    expires: Optional[float] = None,
    httpOnly: Optional[bool] = None,  # noqa: N803
    secure: Optional[bool] = None,
    sameSite: Optional[str] = None,  # noqa: N803
) -> Dict[str, Any]:
    page = await session.page()
    context = await session.context()
    cookie: Dict[str, Any] = {"name": name, "value": value}
    if domain:
        cookie["domain"] = domain
        cookie["path"] = path or "/"
    elif path:
        cookie["url"] = page.url
        cookie["path"] = path
    else:
        cookie["url"] = page.url
    if expires is not None:
        cookie["expires"] = float(expires)
    if httpOnly is not None:
        cookie["httpOnly"] = bool(httpOnly)
    if secure is not None:
        cookie["secure"] = bool(secure)
    if sameSite:
        if sameSite not in ("Strict", "Lax", "None"):
            raise ToolError("'sameSite' debe ser Strict, Lax o None.")
        cookie["sameSite"] = sameSite
    await context.add_cookies([cookie])
    return {"status": "ok", "cookie": cookie}


@registry.tool(
    name="browser_cookie_delete",
    title="Delete cookie",
    description="Elimina una cookie concreta por su nombre.",
    input_schema={
        "type": "object",
        "properties": {"name": _str_prop("Nombre de la cookie a eliminar.")},
        "required": ["name"],
        "additionalProperties": False,
    },
)
async def browser_cookie_delete(name: str) -> Dict[str, Any]:
    context = await session.context()
    cookies = await context.cookies()
    keep = [c for c in cookies if c.get("name") != name]
    deleted = len(cookies) - len(keep)
    await context.clear_cookies()
    if keep:
        await context.add_cookies(keep)
    return {"status": "ok", "deleted": deleted, "name": name}


@registry.tool(
    name="browser_cookie_clear",
    title="Clear cookies",
    description="Elimina todas las cookies del contexto del navegador.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_cookie_clear() -> Dict[str, Any]:
    context = await session.context()
    before = len(await context.cookies())
    await context.clear_cookies()
    return {"status": "ok", "cleared": before}


# =========================================================================== #
#  STORAGE: localStorage / sessionStorage
# =========================================================================== #
def _register_web_storage(kind: str) -> None:
    """Genera las 5 tools (list/get/set/delete/clear) para local o session storage."""
    label = "localStorage" if kind == "local" else "sessionStorage"
    api = "localStorage" if kind == "local" else "sessionStorage"

    @registry.tool(
        name=f"browser_{kind}storage_list",
        title=f"List {label}",
        description=f"Lista todos los pares clave-valor de {label}.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def _list() -> Dict[str, Any]:
        page = await session.page()
        items = await page.evaluate(f"() => ({{...{api}}})")
        return {"storage": label, "total": len(items or {}), "items": items or {}}

    @registry.tool(
        name=f"browser_{kind}storage_get",
        title=f"Get {label} item",
        description=f"Obtiene un elemento de {label} por su clave.",
        input_schema={
            "type": "object",
            "properties": {"key": _str_prop("Clave a consultar.")},
            "required": ["key"],
            "additionalProperties": False,
        },
    )
    async def _get(key: str) -> Dict[str, Any]:
        page = await session.page()
        value = await page.evaluate(f"(k) => {api}.getItem(k)", key)
        return {"storage": label, "key": key, "value": value, "found": value is not None}

    @registry.tool(
        name=f"browser_{kind}storage_set",
        title=f"Set {label} item",
        description=f"Establece un elemento en {label}.",
        input_schema={
            "type": "object",
            "properties": {
                "key": _str_prop("Clave a establecer."),
                "value": _str_prop("Valor a almacenar."),
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    )
    async def _set(key: str, value: str) -> Dict[str, Any]:
        page = await session.page()
        await page.evaluate(f"({{k, v}}) => {api}.setItem(k, v)", {"k": key, "v": value})
        return {"storage": label, "status": "ok", "key": key, "value": value}

    @registry.tool(
        name=f"browser_{kind}storage_delete",
        title=f"Delete {label} item",
        description=f"Elimina un elemento de {label}.",
        input_schema={
            "type": "object",
            "properties": {"key": _str_prop("Clave a eliminar.")},
            "required": ["key"],
            "additionalProperties": False,
        },
    )
    async def _delete(key: str) -> Dict[str, Any]:
        page = await session.page()
        await page.evaluate(f"(k) => {api}.removeItem(k)", key)
        return {"storage": label, "status": "ok", "deleted": key}

    @registry.tool(
        name=f"browser_{kind}storage_clear",
        title=f"Clear {label}",
        description=f"Vacía por completo {label}.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def _clear() -> Dict[str, Any]:
        page = await session.page()
        count = await page.evaluate(f"() => {{ const n = {api}.length; {api}.clear(); return n; }}")
        return {"storage": label, "status": "ok", "cleared": count}


_register_web_storage("local")
_register_web_storage("session")


@registry.tool(
    name="browser_storage_state",
    title="Save storage state",
    description="Guarda el estado de almacenamiento (cookies y localStorage) en un archivo reutilizable.",
    input_schema={
        "type": "object",
        "properties": {"filename": _str_prop("Archivo destino. Def. storage-state-<timestamp>.json")},
        "additionalProperties": False,
    },
)
async def browser_storage_state(filename: Optional[str] = None) -> Dict[str, Any]:
    context = await session.context()
    path = session.resolve_output(filename, ".json", "storage-state")
    await context.storage_state(path=str(path))
    return {"status": "ok", "savedTo": str(path), "bytes": path.stat().st_size}


@registry.tool(
    name="browser_set_storage_state",
    title="Restore storage state",
    description=(
        "Restaura el estado de almacenamiento (cookies y localStorage) desde un "
        "archivo. Limpia las cookies y el localStorage existentes antes de restaurar."
    ),
    input_schema={
        "type": "object",
        "properties": {"filename": _str_prop("Ruta del archivo de estado a restaurar.")},
        "required": ["filename"],
        "additionalProperties": False,
    },
)
async def browser_set_storage_state(filename: str) -> Dict[str, Any]:
    path = Path(filename)
    if not path.is_absolute():
        candidate = session.output_dir() / filename
        path = candidate if candidate.exists() else settings.workspace_root / filename
    if not path.exists():
        raise ToolError(f"No existe el archivo de estado: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    context = await session.context()
    page = await session.page()

    await context.clear_cookies()
    if state.get("cookies"):
        await context.add_cookies(state["cookies"])
    restored_origins = 0
    for origin in state.get("origins", []):
        items = {i["name"]: i["value"] for i in origin.get("localStorage", [])}
        if not items:
            continue
        try:
            await page.evaluate(
                "(items) => { localStorage.clear(); for (const [k,v] of Object.entries(items)) localStorage.setItem(k,v); }",
                items,
            )
            restored_origins += 1
        except Exception:  # noqa: BLE001
            pass
    return {
        "status": "ok",
        "restoredFrom": str(path),
        "cookies": len(state.get("cookies", [])),
        "origins": restored_origins,
    }


# =========================================================================== #
#  DEVTOOLS: highlight / tracing / video
# =========================================================================== #
@registry.tool(
    name="browser_highlight",
    title="Highlight element",
    description="Dibuja un recuadro persistente sobre un elemento de la página.",
    input_schema={
        "type": "object",
        "properties": {
            "element": _str_prop(_ELEMENT_DESC),
            "target": _str_prop(_REF_DESC),
            "style": _str_prop('CSS adicional, p.ej. "outline: 2px dashed red".'),
        },
        "required": ["target"],
        "additionalProperties": False,
    },
)
async def browser_highlight(
    target: str, element: Optional[str] = None, style: Optional[str] = None
) -> Dict[str, Any]:
    page = await session.page()
    loc = await session.locator(target)
    ref = await loc.get_attribute("data-mcp-ref")
    if not ref:
        ref = f"h{now_stamp()}"
        await loc.evaluate("(el, r) => el.setAttribute('data-mcp-ref', r)", ref)
    ok = await page.evaluate(
        HIGHLIGHT_JS, {"selector": f'[data-mcp-ref="{ref}"]', "key": ref, "style": style or ""}
    )
    if not ok:
        raise ToolError(f"No se pudo resaltar el elemento '{target}'.")
    return {"status": "highlighted", "target": target, "key": ref}


@registry.tool(
    name="browser_hide_highlight",
    title="Hide element highlight",
    description="Elimina el recuadro de resaltado añadido previamente (o todos si se omite el target).",
    input_schema={
        "type": "object",
        "properties": {"element": _str_prop(_ELEMENT_DESC), "target": _str_prop(_REF_DESC)},
        "additionalProperties": False,
    },
)
async def browser_hide_highlight(
    target: Optional[str] = None, element: Optional[str] = None
) -> Dict[str, Any]:
    page = await session.page()
    key = None
    if target:
        loc = await session.locator(target)
        key = await loc.get_attribute("data-mcp-ref")
    removed = await page.evaluate(UNHIGHLIGHT_JS, key)
    return {"status": "ok", "removed": removed}


@registry.tool(
    name="browser_start_tracing",
    title="Start tracing",
    description="Inicia la grabación de un trace de Playwright (acciones, capturas y snapshots).",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_start_tracing() -> Dict[str, Any]:
    context = await session.context()
    if session.tracing_active:
        return {"status": "already_active"}
    await context.tracing.start(screenshots=True, snapshots=True, sources=False)
    session.tracing_active = True
    return {"status": "started", "hint": "Detén la grabación con browser_stop_tracing."}


@registry.tool(
    name="browser_stop_tracing",
    title="Stop tracing",
    description="Detiene la grabación del trace y lo guarda en un archivo .zip abrible con trace.playwright.dev.",
    input_schema={
        "type": "object",
        "properties": {"filename": _str_prop("Archivo destino. Def. trace-<timestamp>.zip")},
        "additionalProperties": False,
    },
)
async def browser_stop_tracing(filename: Optional[str] = None) -> Dict[str, Any]:
    if not session.tracing_active:
        raise ToolError("No hay ninguna grabación de trace activa (usa browser_start_tracing).")
    context = await session.context()
    path = session.resolve_output(filename, ".zip", "trace")
    await context.tracing.stop(path=str(path))
    session.tracing_active = False
    return {"status": "stopped", "savedTo": str(path), "viewer": "https://trace.playwright.dev"}


@registry.tool(
    name="browser_start_video",
    title="Start video",
    description=(
        "Inicia la grabación en vídeo de la sesión. Crea un contexto nuevo, por lo "
        "que se pierde el estado de la página actual (navega de nuevo tras iniciar)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filename": _str_prop("Directorio o archivo destino del vídeo."),
            "size": {"type": "object", "description": 'Tamaño del vídeo: {"width":W,"height":H}.'},
        },
        "additionalProperties": False,
    },
)
async def browser_start_video(
    filename: Optional[str] = None, size: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    await session.page()  # garantiza navegador arrancado
    if session.video_active:
        return {"status": "already_active", "directory": session.video_dir}
    directory = session.resolve_output(filename or f"video-{now_stamp()}", "", "video")
    directory.mkdir(parents=True, exist_ok=True)
    extra: Dict[str, Any] = {"record_video_dir": str(directory)}
    if size:
        extra["record_video_size"] = {
            "width": int(size.get("width", settings.browser_viewport_width)),
            "height": int(size.get("height", settings.browser_viewport_height)),
        }
    old_context = await session.context()
    new_context = await session._new_context(**extra)  # noqa: SLF001
    page = await new_context.new_page()
    session.attach_listeners(page)
    session._context = new_context  # noqa: SLF001
    session.set_page(page)
    try:
        await old_context.close()
    except Exception:  # noqa: BLE001
        pass
    session.video_active = True
    session.video_dir = str(directory)
    return {
        "status": "started",
        "directory": str(directory),
        "note": "Se creó un contexto nuevo: navega de nuevo con browser_navigate.",
    }


@registry.tool(
    name="browser_stop_video",
    title="Stop video",
    description="Detiene la grabación de vídeo y devuelve la ruta del archivo generado.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
async def browser_stop_video() -> Dict[str, Any]:
    if not session.video_active:
        raise ToolError("No hay ninguna grabación de vídeo activa (usa browser_start_video).")
    page = await session.page()
    video = page.video
    context = await session.context()
    await context.close()
    path = await video.path() if video else None
    session.video_active = False
    directory = session.video_dir
    session._context = None  # noqa: SLF001
    session._page = None  # noqa: SLF001
    return {"status": "stopped", "savedTo": str(path) if path else None, "directory": directory}


# =========================================================================== #
#  COORDINATE-BASED (vision)
# =========================================================================== #
@registry.tool(
    name="browser_mouse_move_xy",
    title="Move mouse",
    description="Mueve el puntero del ratón a unas coordenadas concretas.",
    input_schema={
        "type": "object",
        "properties": {
            "x": {"type": "number", "description": "Coordenada X."},
            "y": {"type": "number", "description": "Coordenada Y."},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    },
)
async def browser_mouse_move_xy(x: float, y: float) -> Dict[str, Any]:
    page = await session.page()
    await page.mouse.move(float(x), float(y))
    return {"action": "move", "x": x, "y": y}


@registry.tool(
    name="browser_mouse_click_xy",
    title="Click at coordinates",
    description="Hace clic con el ratón en unas coordenadas concretas.",
    input_schema={
        "type": "object",
        "properties": {
            "x": {"type": "number", "description": "Coordenada X."},
            "y": {"type": "number", "description": "Coordenada Y."},
            "button": _str_prop("Botón: left | right | middle. Def. left."),
            "clickCount": {"type": "integer", "description": "Número de clics. Def. 1."},
            "delay": {"type": "number", "description": "Milisegundos entre pulsar y soltar. Def. 0."},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    },
)
async def browser_mouse_click_xy(
    x: float,
    y: float,
    button: str = "left",
    clickCount: int = 1,  # noqa: N803
    delay: float = 0,
) -> Dict[str, Any]:
    if button not in ("left", "right", "middle"):
        raise ToolError("'button' debe ser left, right o middle.")
    page = await session.page()
    await page.mouse.click(
        float(x), float(y), button=button, click_count=int(clickCount), delay=float(delay)
    )
    return await _page_state({"action": "click", "x": x, "y": y, "button": button})


@registry.tool(
    name="browser_mouse_down",
    title="Press mouse down",
    description="Pulsa (y mantiene) un botón del ratón.",
    input_schema={
        "type": "object",
        "properties": {"button": _str_prop("Botón: left | right | middle. Def. left.")},
        "additionalProperties": False,
    },
)
async def browser_mouse_down(button: str = "left") -> Dict[str, Any]:
    if button not in ("left", "right", "middle"):
        raise ToolError("'button' debe ser left, right o middle.")
    page = await session.page()
    await page.mouse.down(button=button)
    return {"action": "mouseDown", "button": button}


@registry.tool(
    name="browser_mouse_up",
    title="Press mouse up",
    description="Suelta un botón del ratón.",
    input_schema={
        "type": "object",
        "properties": {"button": _str_prop("Botón: left | right | middle. Def. left.")},
        "additionalProperties": False,
    },
)
async def browser_mouse_up(button: str = "left") -> Dict[str, Any]:
    if button not in ("left", "right", "middle"):
        raise ToolError("'button' debe ser left, right o middle.")
    page = await session.page()
    await page.mouse.up(button=button)
    return {"action": "mouseUp", "button": button}


@registry.tool(
    name="browser_mouse_drag_xy",
    title="Drag mouse to coordinates",
    description="Arrastra el botón izquierdo del ratón desde unas coordenadas hasta otras.",
    input_schema={
        "type": "object",
        "properties": {
            "startX": {"type": "number", "description": "X inicial."},
            "startY": {"type": "number", "description": "Y inicial."},
            "endX": {"type": "number", "description": "X final."},
            "endY": {"type": "number", "description": "Y final."},
        },
        "required": ["startX", "startY", "endX", "endY"],
        "additionalProperties": False,
    },
)
async def browser_mouse_drag_xy(
    startX: float, startY: float, endX: float, endY: float  # noqa: N803
) -> Dict[str, Any]:
    page = await session.page()
    await page.mouse.move(float(startX), float(startY))
    await page.mouse.down()
    await page.mouse.move(float(endX), float(endY))
    await page.mouse.up()
    return {"action": "drag", "from": [startX, startY], "to": [endX, endY]}


@registry.tool(
    name="browser_mouse_wheel",
    title="Scroll mouse wheel",
    description="Desplaza la rueda del ratón (scroll) los deltas indicados.",
    input_schema={
        "type": "object",
        "properties": {
            "deltaX": {"type": "number", "description": "Desplazamiento horizontal."},
            "deltaY": {"type": "number", "description": "Desplazamiento vertical."},
        },
        "required": ["deltaX", "deltaY"],
        "additionalProperties": False,
    },
)
async def browser_mouse_wheel(deltaX: float, deltaY: float) -> Dict[str, Any]:  # noqa: N803
    page = await session.page()
    await page.mouse.wheel(float(deltaX), float(deltaY))
    return {"action": "wheel", "deltaX": deltaX, "deltaY": deltaY}

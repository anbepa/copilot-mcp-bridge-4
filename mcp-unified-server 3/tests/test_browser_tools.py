#!/usr/bin/env python3
"""
Suite de pruebas en proceso para el grupo de herramientas `browser_*`.

Playwright no se instala en CI por defecto (descarga ~150 MB de binarios), por
lo que estas pruebas inyectan un *doble de prueba* (FakePage / FakeContext /
FakeLocator) que implementa el subconjunto de la API de Playwright que usan las
tools. Así se valida — sin navegador real — que:

  * cada tool mapea sus argumentos a la llamada correcta de Playwright,
  * la resolución de referencias 'e12' -> '[data-mcp-ref="e12"]' funciona,
  * los caminos de error devuelven ToolError con mensajes accionables,
  * las puertas de seguridad (código unsafe, navegador deshabilitado) cierran.

Uso:  python3 tests/test_browser_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKDIR = tempfile.mkdtemp(prefix="mcp-browser-tests-")
os.environ.setdefault("MCP_WORKSPACE_ROOT", WORKDIR)
os.environ.setdefault("MCP_BROWSER_OUTPUT_DIR", str(Path(WORKDIR) / "out"))

from server.browser.session import session  # noqa: E402
from server.config import settings  # noqa: E402
from server.core.registry import ToolError, registry  # noqa: E402
import server.tools.browser  # noqa: E402,F401

PASSED = 0
FAILED: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  \033[92m✓\033[0m {label}")
    else:
        FAILED.append(label)
        print(f"  \033[91m✗\033[0m {label}  {detail}")


async def call(_tool: str, /, **kwargs: Any) -> Any:
    return await registry.call(_tool, kwargs)


async def expect_error(_label: str, _tool: str, /, **kwargs: Any) -> None:
    label, name = _label, _tool
    try:
        await registry.call(name, kwargs)
    except ToolError as exc:
        check(label, True)
        globals()["LAST_ERROR"] = str(exc)
        return
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"se esperaba ToolError, llegó {type(exc).__name__}: {exc}")
        return
    check(label, False, "no lanzó ToolError")


# =========================================================================== #
#  Dobles de prueba (subconjunto de la API async de Playwright)
# =========================================================================== #
CALLS: List[Dict[str, Any]] = []


def record(what: str, **data: Any) -> None:
    CALLS.append({"call": what, **data})


def last(what: str) -> Optional[Dict[str, Any]]:
    for entry in reversed(CALLS):
        if entry["call"] == what:
            return entry
    return None


class FakeKeyboard:
    async def press(self, key: str) -> None:
        record("keyboard.press", key=key)

    async def type(self, text: str, delay: int = 0) -> None:
        record("keyboard.type", text=text)


class FakeMouse:
    async def move(self, x: float, y: float, steps: int = 1) -> None:
        record("mouse.move", x=x, y=y, steps=steps)

    async def click(self, x: float, y: float, button: str = "left", click_count: int = 1, **kw: Any) -> None:
        record("mouse.click", x=x, y=y, button=button, clickCount=click_count)

    async def down(self, button: str = "left") -> None:
        record("mouse.down", button=button)

    async def up(self, button: str = "left") -> None:
        record("mouse.up", button=button)

    async def wheel(self, dx: float, dy: float) -> None:
        record("mouse.wheel", dx=dx, dy=dy)


class FakeLocator:
    def __init__(self, selector: str, page: "FakePage", count: int = 1) -> None:
        self.selector = selector
        self._page = page
        self._count = count

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return self._count

    async def click(self, **kw: Any) -> None:
        record("click", selector=self.selector, **kw)

    async def dblclick(self, **kw: Any) -> None:
        record("dblclick", selector=self.selector, **kw)

    async def hover(self, **kw: Any) -> None:
        record("hover", selector=self.selector, **kw)

    async def fill(self, value: str, **kw: Any) -> None:
        record("fill", selector=self.selector, value=value)

    async def press(self, key: str, **kw: Any) -> None:
        record("press", selector=self.selector, key=key)

    async def press_sequentially(self, text: str, delay: int = 0) -> None:
        record("press_sequentially", selector=self.selector, text=text, delay=delay)

    async def check(self) -> None:
        record("check", selector=self.selector)

    async def uncheck(self) -> None:
        record("uncheck", selector=self.selector)

    async def select_option(self, values: Any, **kw: Any) -> List[str]:
        record("select_option", selector=self.selector, values=values)
        return [str(v) for v in (values if isinstance(values, list) else [values])]

    async def set_input_files(self, files: Any) -> None:
        record("set_input_files", selector=self.selector, files=files)

    async def drag_to(self, other: "FakeLocator", **kw: Any) -> None:
        record("drag_to", source=self.selector, target=other.selector)

    async def get_attribute(self, name: str) -> Optional[str]:
        if name == "data-mcp-ref" and 'data-mcp-ref="' in self.selector:
            return self.selector.split('data-mcp-ref="')[1].split('"')[0]
        return None

    async def screenshot(self, path: str, type: str = "png", **kw: Any) -> None:  # noqa: A002
        record("locator.screenshot", selector=self.selector, path=path, type=type)
        Path(path).write_bytes(b"\x89PNG-fake-element")

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        record("wait_for", selector=self.selector, state=state)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        record("locator.evaluate", selector=self.selector, expression=expression)
        return {"evaluatedOn": self.selector}


SNAPSHOT_TREE = "\n".join(
    [
        'document "Panel de control" [ref=e1]',
        '  heading "Bienvenido, Andres" [ref=e2]',
        '  textbox "Usuario" [ref=e3]',
        '  textbox "Contraseña" [ref=e4]',
        '  checkbox "Recordarme" [ref=e5]',
        '  combobox "País" [ref=e6]',
        '  button "Iniciar sesión" [ref=e7]',
        '  link "¿Olvidaste tu contraseña?" [ref=e8]',
    ]
)


class FakePage:
    def __init__(self, context: "FakeContext", url: str = "about:blank") -> None:
        self._context = context
        self.url = url
        self._title = "Página en blanco"
        self._closed = False
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.viewport = {"width": 1280, "height": 720}
        self.storage: Dict[str, Dict[str, str]] = {"local": {}, "session": {}}
        self.listeners: Dict[str, List[Any]] = {}
        self.main_frame = object()

    # --- eventos -------------------------------------------------------- #
    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload: Any) -> None:
        for handler in self.listeners.get(event, []):
            handler(payload)

    # --- ciclo de vida ------------------------------------------------- #
    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True
        if self in self._context.pages:
            self._context.pages.remove(self)
        record("page.close", url=self.url)

    async def bring_to_front(self) -> None:
        record("bring_to_front", url=self.url)

    async def title(self) -> str:
        return self._title

    # --- navegación ----------------------------------------------------- #
    async def goto(self, url: str, wait_until: str = "load", **kw: Any):
        record("goto", url=url, wait_until=wait_until)
        self.url = url
        self._title = f"Título de {url}"

        class Resp:
            status = 200

        return Resp()

    async def go_back(self, **kw: Any) -> None:
        record("go_back")
        self.url = "https://anterior.example/"

    # --- interacción ---------------------------------------------------- #
    def locator(self, selector: str) -> FakeLocator:
        missing = 'data-mcp-ref="e999"' in selector
        return FakeLocator(selector, self, count=0 if missing else 1)

    def get_by_text(self, text: str) -> FakeLocator:
        return FakeLocator(f"text={text}", self)

    async def set_viewport_size(self, size: Dict[str, int]) -> None:
        record("set_viewport_size", **size)
        self.viewport = dict(size)

    async def screenshot(self, path: str, type: str = "png", full_page: bool = False, **kw: Any) -> None:  # noqa: A002
        record("page.screenshot", path=path, type=type, full_page=full_page)
        Path(path).write_bytes(b"\x89PNG-fake-page")

    async def set_input_files(self, *a: Any, **kw: Any) -> None:
        record("page.set_input_files")

    # --- evaluate: emula el JS de snapshot, highlight y web storage ----- #
    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        record("evaluate", expression=expression[:60], arg=arg)
        if "data-mcp-ref" in expression and "tree" in expression:
            return {
                "url": self.url,
                "title": self._title,
                "tree": SNAPSHOT_TREE,
                "count": len(SNAPSHOT_TREE.splitlines()),
            }
        for kind, api in (("local", "localStorage"), ("session", "sessionStorage")):
            store = self.storage[kind]
            if f"{{...{api}}}" in expression:
                return dict(store)
            if f"{api}.getItem" in expression:
                return store.get(arg)
            if f"{api}.setItem" in expression:
                store[arg["k"]] = arg["v"]
                return None
            if f"{api}.removeItem" in expression:
                store.pop(arg, None)
                return None
            if f"{api}.clear()" in expression:
                n = len(store)
                store.clear()
                return n
        if "mcp-highlight" in expression or "outline" in expression:
            return 3
        return {"evaluated": True, "arg": arg}


class FakeContext:
    def __init__(self) -> None:
        self.pages: List[FakePage] = []
        self._cookies: List[Dict[str, Any]] = []
        self.offline = False
        self.routes: List[str] = []
        self.tracing = FakeTracing()

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        record("new_page")
        return page

    async def cookies(self) -> List[Dict[str, Any]]:
        return list(self._cookies)

    async def add_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        record("add_cookies", cookies=cookies)
        self._cookies.extend(cookies)

    async def clear_cookies(self) -> None:
        record("clear_cookies")
        self._cookies = []

    async def set_offline(self, offline: bool) -> None:
        record("set_offline", offline=offline)
        self.offline = offline

    async def route(self, pattern: str, handler: Any) -> None:
        record("route", pattern=pattern)
        self.routes.append(pattern)

    async def unroute(self, pattern: str, handler: Any = None) -> None:
        record("unroute", pattern=pattern)
        if pattern in self.routes:
            self.routes.remove(pattern)

    async def storage_state(self, path: str) -> Dict[str, Any]:
        record("storage_state", path=path)
        Path(path).write_text(json.dumps({"cookies": self._cookies, "origins": []}), encoding="utf-8")
        return {"cookies": self._cookies, "origins": []}

    async def close(self) -> None:
        record("context.close")


class FakeTracing:
    async def start(self, **kw: Any) -> None:
        record("tracing.start", **kw)

    async def stop(self, path: str) -> None:
        record("tracing.stop", path=path)
        Path(path).write_bytes(b"PK-fake-trace")


def install_fakes() -> FakePage:
    """Inyecta el doble de prueba dentro del singleton `session`."""
    context = FakeContext()
    page = FakePage(context, url="https://demo.local/login")
    page._title = "Panel de control"
    context.pages.append(page)
    session._playwright = object()
    session._browser = object()
    session._context = context
    session._page = page
    session.console_messages.clear()
    session.network_requests.clear()
    session.routes.clear()
    session.pending_dialog = None
    session.nav_console_mark = 0
    session.nav_network_mark = 0
    CALLS.clear()
    return page


# =========================================================================== #
#  Pruebas
# =========================================================================== #
async def test_registration() -> None:
    print("\n\033[1m[1] Registro y esquemas de las tools browser_*\033[0m")
    names = [n for n in registry.names() if n.startswith("browser_")]
    check("se registran >= 55 tools browser_*", len(names) >= 55, f"encontradas {len(names)}")

    esenciales = [
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_fill_form", "browser_take_screenshot", "browser_tabs",
        "browser_console_messages", "browser_network_requests", "browser_evaluate",
        "browser_cookie_set", "browser_localstorage_set", "browser_sessionstorage_set",
        "browser_highlight", "browser_start_tracing", "browser_mouse_click_xy",
    ]
    faltan = [n for n in esenciales if n not in names]
    check("están todas las tools esenciales del catálogo", not faltan, f"faltan {faltan}")

    malos = []
    for tool in registry.list_tools():
        if not tool["name"].startswith("browser_"):
            continue
        schema = tool["inputSchema"]
        if schema.get("type") != "object" or "properties" not in schema:
            malos.append(tool["name"])
        if not tool.get("description"):
            malos.append(tool["name"])
        for prop, spec in schema["properties"].items():
            if "type" not in spec or "description" not in spec:
                malos.append(f"{tool['name']}.{prop}")
    check("todos los inputSchema son válidos y documentados", not malos, f"{malos[:5]}")


async def test_navigation_and_snapshot() -> None:
    print("\n\033[1m[2] Navegación y snapshot\033[0m")
    page = install_fakes()

    res = await call("browser_navigate", url="https://ejemplo.com/portal")
    check("browser_navigate llama a page.goto", last("goto")["url"] == "https://ejemplo.com/portal")
    check("browser_navigate devuelve url y title", res["url"] == "https://ejemplo.com/portal" and res["title"])
    check("browser_navigate devuelve el status HTTP", res["status"] == 200)

    await call("browser_navigate", url="ejemplo.com")
    check("browser_navigate antepone https:// si falta el esquema",
          last("goto")["url"] == "https://ejemplo.com")

    await call("browser_navigate_back")
    check("browser_navigate_back invoca go_back", last("go_back") is not None)

    page.url = "https://demo.local/login"
    snap = await call("browser_snapshot")
    check("browser_snapshot devuelve el árbol de accesibilidad", "[ref=e7]" in snap["snapshot"])
    check("browser_snapshot informa del número de elementos", snap["elements"] == 8)

    snap2 = await call("browser_snapshot", target="e7")
    check("browser_snapshot con target filtra a ese nodo",
          snap2["snapshot"].strip().startswith('button "Iniciar sesión"'))

    out = Path(WORKDIR) / "out" / "snap.md"
    snap3 = await call("browser_snapshot", filename="snap.md")
    check("browser_snapshot con filename escribe a disco", out.exists() and snap3["savedTo"] == str(out))


async def test_find() -> None:
    print("\n\033[1m[3] browser_find (búsqueda en el snapshot)\033[0m")
    install_fakes()

    res = await call("browser_find", text="contraseña")
    check("browser_find encuentra sin distinguir mayúsculas/acentos exactos", res["total"] == 2, str(res["total"]))
    check("browser_find extrae el ref del nodo", res["matches"][0]["ref"] == "e4")
    check("browser_find incluye contexto alrededor", "\n" in res["matches"][0]["context"])

    res = await call("browser_find", regex=r"/BUTTON|LINK/i")
    check("browser_find soporta sintaxis /patron/i", res["total"] == 2, str(res["total"]))

    res = await call("browser_find", regex=r"textbox")
    check("browser_find soporta regex sin delimitadores", res["total"] == 2)

    await expect_error("browser_find exige text XOR regex", "browser_find", text="a", regex="b")
    await expect_error("browser_find rechaza regex inválida", "browser_find", regex="[sin-cerrar")


async def test_interaction() -> None:
    print("\n\033[1m[4] Interacción: click / hover / type / form / select / teclas\033[0m")
    install_fakes()

    await call("browser_click", target="e7", element="Botón de login")
    check("browser_click resuelve 'e7' a [data-mcp-ref=\"e7\"]",
          last("click")["selector"] == '[data-mcp-ref="e7"]')
    check("browser_click usa button=left por defecto", last("click")["button"] == "left")

    await call("browser_click", target="ref=e7", button="right")
    check("browser_click acepta el formato 'ref=eN'",
          last("click")["selector"] == '[data-mcp-ref="e7"]')
    check("browser_click propaga el botón derecho", last("click")["button"] == "right")

    await call("browser_click", target="#submit", doubleClick=True, modifiers=["Shift"])
    check("browser_click acepta selectores CSS crudos", last("dblclick")["selector"] == "#submit")
    check("browser_click propaga doubleClick y modifiers", last("dblclick")["modifiers"] == ["Shift"])

    await expect_error("browser_click rechaza un botón inválido", "browser_click", target="e7", button="middle-ish")
    await expect_error("una ref inexistente da un error accionable", "browser_click", target="e999")
    check("el error de ref sugiere re-ejecutar browser_snapshot",
          "browser_snapshot" in globals().get("LAST_ERROR", ""))

    await call("browser_hover", target="e8")
    check("browser_hover invoca hover sobre el elemento",
          last("hover")["selector"] == '[data-mcp-ref="e8"]')

    await call("browser_type", target="e3", text="aabernal")
    check("browser_type usa fill por defecto", last("fill")["value"] == "aabernal")

    await call("browser_type", target="e4", text="secreto", slowly=True, submit=True)
    check("browser_type con slowly escribe carácter a carácter",
          last("press_sequentially")["text"] == "secreto")
    check("browser_type con submit pulsa Enter", last("press")["key"] == "Enter")

    res = await call("browser_fill_form", fields=[
        {"name": "Usuario", "target": "e3", "type": "textbox", "value": "andres"},
        {"name": "Recordarme", "target": "e5", "type": "checkbox", "value": True},
        {"name": "País", "target": "e6", "type": "combobox", "value": "Colombia"},
    ])
    check("browser_fill_form procesa los 3 campos", res["filled"] == 3)
    check("browser_fill_form marca el checkbox con check()", last("check") is not None)
    check("browser_fill_form usa select_option en un combobox",
          last("select_option")["values"] == "Colombia")
    check("browser_fill_form reporta ok por campo",
          all(f["status"] == "ok" for f in res["fields"]))

    await call("browser_fill_form", fields=[{"target": "e5", "type": "checkbox", "value": False}])
    check("browser_fill_form desmarca cuando value=False", last("uncheck") is not None)

    await expect_error("browser_fill_form rechaza lista vacía", "browser_fill_form", fields=[])
    await expect_error("browser_fill_form exige target por campo",
                       "browser_fill_form", fields=[{"value": "x"}])

    res = await call("browser_select_option", target="e6", values=["CO", "MX"])
    check("browser_select_option pasa la lista de valores",
          last("select_option")["values"] == ["CO", "MX"])
    check("browser_select_option devuelve lo seleccionado", res["selected"] == ["CO", "MX"])
    await expect_error("browser_select_option exige valores", "browser_select_option", target="e6", values=[])

    await call("browser_press_key", key="Enter")
    check("browser_press_key usa el teclado de la página", last("keyboard.press")["key"] == "Enter")

    await call("browser_drag", startTarget="e3", endTarget="e7")
    check("browser_drag arrastra de origen a destino",
          last("drag_to")["source"] == '[data-mcp-ref="e3"]'
          and last("drag_to")["target"] == '[data-mcp-ref="e7"]')


async def test_files_and_dialogs() -> None:
    print("\n\033[1m[5] Subida de archivos y diálogos\033[0m")
    install_fakes()
    sample = Path(WORKDIR) / "adjunto.txt"
    sample.write_text("hola", encoding="utf-8")

    await call("browser_file_upload", paths=[str(sample)], target="e3")
    check("browser_file_upload envía las rutas al input",
          last("set_input_files")["files"] == [str(sample)])
    await expect_error("browser_file_upload rechaza rutas inexistentes",
                       "browser_file_upload", paths=[str(Path(WORKDIR) / "no-existe.txt")], target="e3")

    await expect_error("browser_handle_dialog avisa si no hay diálogo pendiente",
                       "browser_handle_dialog", accept=True)

    class FakeDialog:
        type = "prompt"
        message = "¿Tu nombre?"

        def __init__(self) -> None:
            self.accepted: Any = None
            self.dismissed = False

        async def accept(self, text: Optional[str] = None) -> None:
            self.accepted = text if text is not None else ""

        async def dismiss(self) -> None:
            self.dismissed = True

    dialog = FakeDialog()
    session.pending_dialog = dialog
    res = await call("browser_handle_dialog", accept=True, promptText="Andres")
    check("browser_handle_dialog acepta con promptText", dialog.accepted == "Andres")
    check("browser_handle_dialog devuelve el diálogo tratado", res["dialog"]["type"] == "prompt")
    check("browser_handle_dialog limpia el diálogo pendiente", session.pending_dialog is None)

    dialog2 = FakeDialog()
    session.pending_dialog = dialog2
    await call("browser_handle_dialog", accept=False)
    check("browser_handle_dialog descarta cuando accept=false", dialog2.dismissed is True)


async def test_evaluate_and_unsafe() -> None:
    print("\n\033[1m[6] Evaluación de JS y puerta de seguridad del código arbitrario\033[0m")
    install_fakes()

    await call("browser_evaluate", function="() => document.title")
    check("browser_evaluate evalúa en la página", last("evaluate") is not None)

    await call("browser_evaluate", function="(el) => el.textContent", target="e2")
    check("browser_evaluate con target evalúa sobre el elemento",
          last("locator.evaluate")["selector"] == '[data-mcp-ref="e2"]')

    settings_backup = settings.enable_unsafe_browser_code
    object.__setattr__(settings, "enable_unsafe_browser_code", False)
    await expect_error("browser_run_code_unsafe está bloqueado por defecto",
                       "browser_run_code_unsafe", code="return 1")
    check("el error de código unsafe indica cómo habilitarlo",
          "MCP_ENABLE_UNSAFE_BROWSER_CODE" in globals().get("LAST_ERROR", ""))

    object.__setattr__(settings, "enable_unsafe_browser_code", True)
    res = await call("browser_run_code_unsafe", code="return await page.title()")
    check("browser_run_code_unsafe ejecuta el snippet cuando se habilita",
          res["result"] == "Panel de control", str(res))
    await expect_error("browser_run_code_unsafe exige code o filename", "browser_run_code_unsafe")
    object.__setattr__(settings, "enable_unsafe_browser_code", settings_backup)


async def test_viewport_screenshot_wait() -> None:
    print("\n\033[1m[7] Viewport, capturas y esperas\033[0m")
    install_fakes()

    res = await call("browser_resize", width=1024, height=768)
    check("browser_resize cambia el viewport", last("set_viewport_size")["width"] == 1024)
    check("browser_resize devuelve el nuevo viewport", res["viewport"] == {"width": 1024, "height": 768})

    res = await call("browser_take_screenshot", filename="captura.png")
    check("browser_take_screenshot guarda el archivo", Path(res["savedTo"]).exists())
    check("browser_take_screenshot reporta el tamaño en bytes", res["bytes"] > 0)

    res = await call("browser_take_screenshot", target="e7", type="jpeg", filename="boton.jpeg")
    check("browser_take_screenshot de un elemento usa locator.screenshot",
          last("locator.screenshot")["selector"] == '[data-mcp-ref="e7"]')
    check("browser_take_screenshot respeta el formato jpeg", res["format"] == "jpeg")

    await expect_error("browser_take_screenshot rechaza fullPage + target",
                       "browser_take_screenshot", target="e7", fullPage=True)
    await expect_error("browser_take_screenshot rechaza formatos no soportados",
                       "browser_take_screenshot", type="gif")

    res = await call("browser_wait_for", text="Bienvenido")
    check("browser_wait_for espera a que aparezca texto", last("wait_for")["state"] == "visible")
    await call("browser_wait_for", textGone="Cargando")
    check("browser_wait_for espera a que desaparezca texto", last("wait_for")["state"] == "hidden")
    res = await call("browser_wait_for", time=0.01)
    check("browser_wait_for admite espera por tiempo", "0.01s" in res["waitedFor"])
    await expect_error("browser_wait_for exige al menos un criterio", "browser_wait_for")


async def test_console_and_network() -> None:
    print("\n\033[1m[8] Consola y red\033[0m")
    install_fakes()

    session.console_messages.extend([
        {"level": "log", "text": "arranque ok", "at": ""},
        {"level": "warning", "text": "recurso obsoleto", "at": ""},
        {"level": "error", "text": "TypeError: x is not a function", "at": ""},
    ])
    res = await call("browser_console_messages", level="log", all=True)
    check("browser_console_messages devuelve los mensajes capturados", res["total"] == 3, str(res["total"]))
    res = await call("browser_console_messages", all=True)
    check("browser_console_messages usa nivel 'info' por defecto (oculta 'log')",
          res["total"] == 2, str(res["total"]))
    res = await call("browser_console_messages", level="error", all=True)
    check("browser_console_messages filtra por nivel", res["total"] == 1, str(res["total"]))

    class FakeReq:
        post_data = None

        async def all_headers(self) -> Dict[str, str]:
            return {"accept": "*/*"}

    class FakeRes:
        async def all_headers(self) -> Dict[str, str]:
            return {"content-type": "application/json"}

        async def text(self) -> str:
            return '{"ok":true}'

    session.network_requests.extend([
        {"method": "GET", "url": "https://demo.local/api/users", "resourceType": "fetch",
         "status": 200, "request": FakeReq(), "response": FakeRes()},
        {"method": "GET", "url": "https://demo.local/logo.png", "resourceType": "image",
         "status": 200, "request": FakeReq(), "response": FakeRes()},
        {"method": "POST", "url": "https://demo.local/api/login", "resourceType": "xhr",
         "status": 401, "request": FakeReq(), "response": FakeRes()},
    ])
    res = await call("browser_network_requests")
    check("browser_network_requests oculta recursos estáticos por defecto", res["total"] == 2, str(res["total"]))
    res = await call("browser_network_requests", static=True)
    check("browser_network_requests con static=true los incluye", res["total"] == 3)
    res = await call("browser_network_requests", filter="api/login")
    check("browser_network_requests filtra por regex de URL", res["total"] == 1)
    await expect_error("browser_network_requests rechaza regex inválida",
                       "browser_network_requests", filter="[mal")

    res = await call("browser_network_request", index=1)
    check("browser_network_request devuelve cabeceras de petición",
          res["request"]["headers"]["accept"] == "*/*")
    check("browser_network_request devuelve el cuerpo de respuesta",
          res["response"]["body"] == '{"ok":true}')
    res = await call("browser_network_request", index=3, part="body")
    check("browser_network_request con part=body acota la salida", set(res) == {"index", "body"})
    await expect_error("browser_network_request valida el índice",
                       "browser_network_request", index=99)
    await expect_error("browser_network_request valida 'part'",
                       "browser_network_request", index=1, part="galletas")

    await call("browser_network_state_set", state="offline")
    check("browser_network_state_set activa el modo offline", last("set_offline")["offline"] is True)
    await call("browser_network_state_set", state="online")
    check("browser_network_state_set vuelve a online", last("set_offline")["offline"] is False)
    await expect_error("browser_network_state_set valida el estado",
                       "browser_network_state_set", state="lunar")

    await call("browser_route", pattern="**/api/**", status=503, body='{"err":1}')
    check("browser_route registra la intercepción", last("route")["pattern"] == "**/api/**")
    res = await call("browser_route_list")
    check("browser_route_list lista las rutas activas", res["total"] == 1)
    await call("browser_unroute", pattern="**/api/**")
    res = await call("browser_route_list")
    check("browser_unroute elimina la ruta", res["total"] == 0)


async def test_tabs() -> None:
    print("\n\033[1m[9] Gestión de pestañas\033[0m")
    install_fakes()

    res = await call("browser_tabs", action="list")
    check("browser_tabs list devuelve la pestaña inicial", res["total"] == 1)

    res = await call("browser_tabs", action="new", url="https://nueva.example/")
    check("browser_tabs new abre una pestaña", res["index"] == 1)
    check("browser_tabs new navega a la URL indicada", last("goto")["url"] == "https://nueva.example/")

    res = await call("browser_tabs", action="select", index=0)
    check("browser_tabs select cambia de pestaña", res["index"] == 0)
    check("browser_tabs select trae la pestaña al frente", last("bring_to_front") is not None)

    res = await call("browser_tabs", action="close", index=1)
    check("browser_tabs close cierra la pestaña indicada", res["remaining"] == 1)

    await expect_error("browser_tabs valida la acción", "browser_tabs", action="teletransportar")
    await expect_error("browser_tabs select exige índice", "browser_tabs", action="select")
    await expect_error("browser_tabs valida índices fuera de rango",
                       "browser_tabs", action="select", index=42)


async def test_storage() -> None:
    print("\n\033[1m[10] Cookies, localStorage, sessionStorage y storage state\033[0m")
    install_fakes()

    await call("browser_cookie_set", name="sesion", value="abc123", domain="demo.local", path="/")
    res = await call("browser_cookie_list")
    check("browser_cookie_set crea la cookie", res["total"] == 1)
    res = await call("browser_cookie_get", name="sesion")
    check("browser_cookie_get la recupera", res["found"] and res["cookie"]["value"] == "abc123")
    res = await call("browser_cookie_get", name="inexistente")
    check("browser_cookie_get informa cuando no existe", res["found"] is False)
    await expect_error("browser_cookie_set valida sameSite",
                       "browser_cookie_set", name="a", value="b", sameSite="Quizas")

    await call("browser_cookie_set", name="otra", value="xyz", domain="demo.local")
    res = await call("browser_cookie_delete", name="sesion")
    check("browser_cookie_delete borra sólo la indicada", res["deleted"] == 1)
    res = await call("browser_cookie_list")
    check("browser_cookie_delete conserva el resto", res["total"] == 1)
    res = await call("browser_cookie_clear")
    check("browser_cookie_clear vacía el contexto", res["cleared"] == 1)

    for kind in ("local", "session"):
        await call(f"browser_{kind}storage_set", key="tema", value="oscuro")
        res = await call(f"browser_{kind}storage_get", key="tema")
        check(f"browser_{kind}storage_set/get funcionan", res["value"] == "oscuro")
        res = await call(f"browser_{kind}storage_list")
        check(f"browser_{kind}storage_list devuelve los pares", res["items"] == {"tema": "oscuro"})
        await call(f"browser_{kind}storage_delete", key="tema")
        res = await call(f"browser_{kind}storage_list")
        check(f"browser_{kind}storage_delete elimina la clave", res["total"] == 0)
        await call(f"browser_{kind}storage_set", key="a", value="1")
        res = await call(f"browser_{kind}storage_clear")
        check(f"browser_{kind}storage_clear vacía el almacén", res["cleared"] == 1)

    check("localStorage y sessionStorage están aislados entre sí",
          session._page.storage["local"] == {} and session._page.storage["session"] == {})

    res = await call("browser_storage_state", filename="estado.json")
    check("browser_storage_state guarda el estado en disco", Path(res["savedTo"]).exists())
    res2 = await call("browser_set_storage_state", filename=res["savedTo"])
    check("browser_set_storage_state relee el archivo guardado", res2["status"] == "ok", str(res2))
    await expect_error("browser_set_storage_state avisa si el archivo no existe",
                       "browser_set_storage_state", filename="/tmp/no-existe-jamas.json")


async def test_devtools_and_vision() -> None:
    print("\n\033[1m[11] DevTools (highlight, tracing, vídeo) y modo visión\033[0m")
    install_fakes()

    res = await call("browser_highlight", target="e7", element="Botón de login")
    check("browser_highlight resalta el elemento indicado", res["status"] == "highlighted", str(res))
    check("browser_highlight devuelve la clave del resaltado", res["key"] == "e7")
    res = await call("browser_hide_highlight")
    check("browser_hide_highlight limpia los resaltados", res["status"] == "ok")

    res = await call("browser_start_tracing")
    check("browser_start_tracing arranca el trace", last("tracing.start") is not None)
    check("browser_start_tracing marca la sesión como activa", session.tracing_active is True)
    res = await call("browser_start_tracing")
    check("browser_start_tracing es idempotente", res["status"] == "already_active")
    res = await call("browser_stop_tracing", filename="traza.zip")
    check("browser_stop_tracing guarda el archivo de traza", Path(res["savedTo"]).exists())
    await expect_error("browser_stop_tracing avisa si no hay traza activa", "browser_stop_tracing")

    await expect_error("browser_stop_video avisa si no se está grabando", "browser_stop_video")

    await call("browser_mouse_move_xy", x=100, y=200)
    check("browser_mouse_move_xy mueve el ratón", (last("mouse.move")["x"], last("mouse.move")["y"]) == (100, 200))
    await call("browser_mouse_click_xy", x=10, y=20, button="right", clickCount=2)
    check("browser_mouse_click_xy propaga botón y número de clics",
          last("mouse.click")["button"] == "right" and last("mouse.click")["clickCount"] == 2)
    await call("browser_mouse_down", button="left")
    await call("browser_mouse_up", button="left")
    check("browser_mouse_down/up funcionan", last("mouse.down") and last("mouse.up"))
    await call("browser_mouse_drag_xy", startX=1, startY=2, endX=3, endY=4)
    check("browser_mouse_drag_xy hace down -> move -> up",
          last("mouse.up") is not None and last("mouse.move")["x"] == 3)
    await call("browser_mouse_wheel", deltaX=0, deltaY=250)
    check("browser_mouse_wheel hace scroll", last("mouse.wheel")["dy"] == 250)
    await expect_error("las tools de ratón validan el botón", "browser_mouse_down", button="pulgar")


async def test_config_and_guards() -> None:
    print("\n\033[1m[12] Configuración y guardas del entorno\033[0m")
    install_fakes()

    cfg = await call("browser_get_config")
    check("browser_get_config informa si Playwright está instalado", "playwrightInstalled" in cfg)
    check("browser_get_config expone el motor configurado", cfg["engine"] in ("chromium", "firefox", "webkit"))
    check("browser_get_config expone el viewport", cfg["viewport"]["width"] == settings.browser_viewport_width)
    check("browser_get_config expone el directorio de salida", Path(cfg["outputDir"]).exists())
    check("browser_get_config marca el navegador como abierto", cfg["browserOpen"] is True)
    await expect_error("browser_install valida el motor", "browser_install", engine="netscape")

    res = await call("browser_close")
    check("browser_close cierra la sesión", res["status"] == "closed" and res["wasOpen"] is True)
    check("browser_close deja la sesión sin página", session.started is False)

    backup = settings.enable_browser
    object.__setattr__(settings, "enable_browser", False)
    await expect_error("con MCP_ENABLE_BROWSER=false las tools se bloquean",
                       "browser_snapshot")
    object.__setattr__(settings, "enable_browser", backup)

    try:
        import playwright  # noqa: F401
        tiene_pw = True
    except ImportError:
        tiene_pw = False
    if not tiene_pw:
        await expect_error("sin Playwright, navegar da un error accionable", "browser_navigate",
                           url="https://ejemplo.com")
        msg = globals().get("LAST_ERROR", "")
        check("el error explica cómo instalar Playwright",
              "pip install playwright" in msg and "playwright install" in msg)
    else:
        check("Playwright está instalado (se omite la prueba de error accionable)", True)


async def main() -> int:
    print("\033[1m╔══════════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1m║  Pruebas de las herramientas browser_* (con doble de prueba) ║\033[0m")
    print("\033[1m╚══════════════════════════════════════════════════════════════╝\033[0m")
    for test in (
        test_registration, test_navigation_and_snapshot, test_find, test_interaction,
        test_files_and_dialogs, test_evaluate_and_unsafe, test_viewport_screenshot_wait,
        test_console_and_network, test_tabs, test_storage, test_devtools_and_vision,
        test_config_and_guards,
    ):
        await test()

    total = PASSED + len(FAILED)
    print("\n" + "═" * 64)
    if FAILED:
        print(f"\033[91mFALLARON {len(FAILED)}/{total} comprobaciones:\033[0m")
        for name in FAILED:
            print(f"  · {name}")
        return 1
    print(f"\033[92mTODO OK — {PASSED}/{total} comprobaciones superadas.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
Sesión de navegador basada en Playwright (async API).

Encapsula el ciclo de vida de Playwright / Browser / Context / Page y mantiene
el estado que consumen las herramientas `browser_*`:
  - mensajes de consola
  - peticiones de red
  - diálogos pendientes
  - rutas (mocks) activas
  - referencias de elementos (`ref`) generadas por el snapshot de accesibilidad

El módulo NO importa Playwright a nivel superior: si la librería no está
instalada, las tools devuelven un error accionable en vez de romper el arranque
del servidor MCP.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import settings
from ..core.registry import ToolError

# --------------------------------------------------------------------------- #
# Script de snapshot: asigna refs estables (data-mcp-ref) y construye un árbol
# de accesibilidad legible por un LLM.
# --------------------------------------------------------------------------- #
SNAPSHOT_JS = r"""
(opts) => {
  const maxDepth = (opts && opts.depth) || 25;
  const withBoxes = !!(opts && opts.boxes);
  let counter = 0;
  const ROLE_BY_TAG = {
    A: 'link', BUTTON: 'button', INPUT: 'textbox', SELECT: 'combobox',
    TEXTAREA: 'textbox', IMG: 'img', H1: 'heading', H2: 'heading',
    H3: 'heading', H4: 'heading', H5: 'heading', H6: 'heading',
    NAV: 'navigation', MAIN: 'main', FORM: 'form', TABLE: 'table',
    UL: 'list', OL: 'list', LI: 'listitem', LABEL: 'label',
    HEADER: 'banner', FOOTER: 'contentinfo', SECTION: 'region',
    OPTION: 'option', IFRAME: 'iframe', P: 'paragraph'
  };
  const INTERACTIVE = new Set(['link','button','textbox','combobox','checkbox','radio','option','tab','menuitem','slider']);

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'range') return 'slider';
      if (t === 'hidden') return null;
      return 'textbox';
    }
    return ROLE_BY_TAG[tag] || null;
  }
  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const l = document.getElementById(labelledby);
      if (l) return (l.textContent || '').trim();
    }
    if (el.tagName === 'IMG') return (el.getAttribute('alt') || '').trim();
    if (el.tagName === 'INPUT') {
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      if (el.id) {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) return (lab.textContent || '').trim();
      }
      return (el.getAttribute('name') || '').trim();
    }
    let text = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) text += node.textContent;
    }
    text = text.replace(/\s+/g, ' ').trim();
    if (!text && el.children.length === 0) text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    return text.slice(0, 160);
  }
  function stateOf(el) {
    const bits = [];
    if (el.disabled) bits.push('disabled');
    if (el.checked) bits.push('checked');
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (el.value) bits.push('value="' + String(el.value).slice(0, 80) + '"');
    }
    if (el.getAttribute('aria-expanded')) bits.push('expanded=' + el.getAttribute('aria-expanded'));
    return bits;
  }

  const lines = [];
  function walk(el, depth) {
    if (depth > maxDepth) return;
    for (const child of el.children) {
      if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','META','LINK'].includes(child.tagName)) continue;
      if (!isVisible(child)) continue;
      const role = roleOf(child);
      const name = nameOf(child);
      if (role && (INTERACTIVE.has(role) || name)) {
        const ref = 'e' + (++counter);
        child.setAttribute('data-mcp-ref', ref);
        let line = '  '.repeat(depth) + '- ' + role;
        if (name) line += ' "' + name.replace(/"/g, "'") + '"';
        line += ' [ref=' + ref + ']';
        const st = stateOf(child);
        if (st.length) line += ' [' + st.join('] [') + ']';
        if (withBoxes) {
          const r = child.getBoundingClientRect();
          line += ' [box=' + Math.round(r.x) + ',' + Math.round(r.y) + ',' +
                  Math.round(r.width) + ',' + Math.round(r.height) + ']';
        }
        lines.push(line);
        walk(child, depth + 1);
      } else {
        walk(child, depth);
      }
    }
  }
  walk(document.body, 0);
  return { title: document.title, url: location.href, tree: lines.join('\n'), count: counter };
}
"""

HIGHLIGHT_JS = r"""
(args) => {
  const el = document.querySelector(args.selector);
  if (!el) return false;
  const box = el.getBoundingClientRect();
  const id = 'mcp-highlight-' + args.key;
  const prev = document.getElementById(id);
  if (prev) prev.remove();
  const ov = document.createElement('div');
  ov.id = id;
  ov.setAttribute('data-mcp-highlight', args.key);
  ov.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483647;' +
    'outline:3px solid #ff0055;border-radius:3px;' +
    'left:' + box.x + 'px;top:' + box.y + 'px;' +
    'width:' + box.width + 'px;height:' + box.height + 'px;' + (args.style || '');
  document.body.appendChild(ov);
  return true;
}
"""

UNHIGHLIGHT_JS = r"""
(key) => {
  const sel = key ? '[data-mcp-highlight="' + key + '"]' : '[data-mcp-highlight]';
  const nodes = document.querySelectorAll(sel);
  nodes.forEach(n => n.remove());
  return nodes.length;
}
"""


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class BrowserSession:
    """Ciclo de vida y estado del navegador. Una única sesión por servidor."""

    REF_RE = re.compile(r"^(?:ref=)?(e\d+)$")

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

        self.console_messages: List[Dict[str, Any]] = []
        self.network_requests: List[Dict[str, Any]] = []
        self.pending_dialog: Any = None
        self.last_dialog: Optional[Dict[str, Any]] = None
        self.routes: List[Dict[str, Any]] = []
        self.tracing_active = False
        self.video_active = False
        self.video_dir: Optional[str] = None
        self.nav_console_mark = 0
        self.nav_network_mark = 0

    # ------------------------------------------------------------------ #
    # Infraestructura
    # ------------------------------------------------------------------ #
    @property
    def started(self) -> bool:
        return self._page is not None

    def output_dir(self) -> Path:
        raw = settings.browser_output_dir or str(settings.workspace_root / "browser-output")
        path = Path(raw)
        if not path.is_absolute():
            path = settings.workspace_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_output(self, filename: Optional[str], default_ext: str, prefix: str) -> Path:
        if not filename:
            filename = f"{prefix}-{now_stamp()}{default_ext}"
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = self.output_dir() / candidate
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def import_playwright() -> Any:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ToolError(
                "Playwright no está instalado. Instálalo con:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium\n"
                "O ejecuta start.sh con ENABLE_BROWSER=true para que lo haga por ti. "
                "También puedes usar la tool 'browser_install'."
            ) from exc
        return async_playwright

    # ------------------------------------------------------------------ #
    async def ensure_page(self) -> Any:
        """Devuelve la página activa, arrancando el navegador si hace falta."""
        if not settings.enable_browser:
            raise ToolError(
                "Las herramientas de navegador están deshabilitadas (MCP_ENABLE_BROWSER=false)."
            )
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            if self._playwright is None:
                async_playwright = self.import_playwright()
                self._playwright = await async_playwright().start()

            engine = getattr(self._playwright, settings.browser_engine, None)
            if engine is None:
                raise ToolError(
                    f"Motor de navegador desconocido: '{settings.browser_engine}'. "
                    "Usa chromium, firefox o webkit."
                )
            launch_args: Dict[str, Any] = {"headless": settings.browser_headless}
            if settings.browser_executable_path:
                launch_args["executable_path"] = settings.browser_executable_path
            try:
                self._browser = await engine.launch(**launch_args)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(
                    f"No se pudo iniciar {settings.browser_engine}: {exc}\n"
                    "¿Ejecutaste 'python -m playwright install "
                    f"{settings.browser_engine}'? (o la tool 'browser_install')"
                ) from exc

            self._context = await self._new_context()
            self._page = await self._context.new_page()
            self.attach_listeners(self._page)
            return self._page

    async def _new_context(self, **extra: Any) -> Any:
        context_args: Dict[str, Any] = {
            "viewport": {
                "width": settings.browser_viewport_width,
                "height": settings.browser_viewport_height,
            },
            "ignore_https_errors": True,
        }
        if settings.browser_user_agent:
            context_args["user_agent"] = settings.browser_user_agent
        context_args.update(extra)
        context = await self._browser.new_context(**context_args)
        context.set_default_timeout(settings.browser_timeout_ms)
        return context

    def attach_listeners(self, page: Any) -> None:
        def on_console(msg: Any) -> None:
            try:
                self.console_messages.append(
                    {
                        "level": msg.type,
                        "text": msg.text,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        def on_request(request: Any) -> None:
            self.network_requests.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "resourceType": request.resource_type,
                    "status": None,
                    "request": request,
                    "response": None,
                }
            )

        def on_response(response: Any) -> None:
            for entry in reversed(self.network_requests):
                if entry["url"] == response.url and entry["status"] is None:
                    entry["status"] = response.status
                    entry["response"] = response
                    break

        def on_dialog(dialog: Any) -> None:
            self.pending_dialog = dialog
            self.last_dialog = {"type": dialog.type, "message": dialog.message}

        def on_navigated(frame: Any) -> None:
            try:
                if frame == page.main_frame:
                    self.nav_console_mark = len(self.console_messages)
                    self.nav_network_mark = len(self.network_requests)
            except Exception:  # noqa: BLE001
                pass

        page.on("console", on_console)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("dialog", on_dialog)
        page.on("framenavigated", on_navigated)

    # ------------------------------------------------------------------ #
    async def page(self) -> Any:
        return await self.ensure_page()

    async def context(self) -> Any:
        await self.ensure_page()
        return self._context

    async def new_page(self, url: Optional[str] = None) -> Any:
        context = await self.context()
        page = await context.new_page()
        self.attach_listeners(page)
        self._page = page
        if url:
            await page.goto(url)
        return page

    async def select_page(self, index: int) -> Any:
        context = await self.context()
        pages = context.pages
        if index < 0 or index >= len(pages):
            raise ToolError(
                f"Índice de pestaña fuera de rango: {index}. Pestañas abiertas: {len(pages)}."
            )
        self._page = pages[index]
        await self._page.bring_to_front()
        return self._page

    def set_page(self, page: Any) -> None:
        self._page = page

    async def close(self) -> Dict[str, Any]:
        was_open = self.started
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        self._playwright = self._browser = self._context = self._page = None
        self.console_messages.clear()
        self.network_requests.clear()
        self.routes.clear()
        self.pending_dialog = None
        self.tracing_active = False
        self.video_active = False
        self.nav_console_mark = 0
        self.nav_network_mark = 0
        return {"status": "closed", "wasOpen": was_open}

    # ------------------------------------------------------------------ #
    # Utilidades para las tools
    # ------------------------------------------------------------------ #
    async def locator(self, target: str) -> Any:
        """
        Resuelve un `target` a un Locator de Playwright.

        Acepta:
          - una referencia del snapshot:  'e12'  o  'ref=e12'
          - cualquier selector de Playwright: CSS, 'text=...', 'xpath=...', etc.
        """
        if target is None or not str(target).strip():
            raise ToolError(
                "Falta 'target'. Usa una referencia del snapshot (p.ej. 'e12', "
                "obtenida con browser_snapshot) o un selector CSS."
            )
        page = await self.page()
        match = self.REF_RE.match(str(target).strip())
        if match:
            selector = f'[data-mcp-ref="{match.group(1)}"]'
            loc = page.locator(selector)
            if await loc.count() == 0:
                raise ToolError(
                    f"La referencia '{target}' ya no existe en la página. "
                    "Vuelve a ejecutar browser_snapshot para obtener refs actualizadas."
                )
            return loc.first
        return page.locator(str(target)).first

    async def snapshot(
        self,
        depth: Optional[int] = None,
        boxes: bool = False,
        target: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = await self.page()
        data = await page.evaluate(SNAPSHOT_JS, {"depth": depth or 25, "boxes": boxes})
        tree = data.get("tree", "")
        if target:
            loc = await self.locator(target)
            ref = await loc.get_attribute("data-mcp-ref")
            if ref:
                tree = "\n".join(l for l in tree.splitlines() if f"[ref={ref}]" in l)
        return {
            "url": data.get("url"),
            "title": data.get("title"),
            "elements": data.get("count", 0),
            "snapshot": tree,
        }


session = BrowserSession()

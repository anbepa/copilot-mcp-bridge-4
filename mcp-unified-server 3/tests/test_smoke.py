#!/usr/bin/env python3
"""
Suite de validación end-to-end del MCP Unified Server.

Arranca el servidor real, ejecuta el handshake MCP sobre SSE, invoca TODAS las
herramientas y verifica los resultados. No requiere pytest.

Uso:  python tests/test_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  \033[0;32m✓\033[0m {label}")
    else:
        FAILED += 1
        print(f"  \033[0;31m✗\033[0m {label} {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url: str, payload: Any, method: str = "POST") -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class Client:
    """Cliente MCP mínimo sobre Streamable HTTP (/mcp)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        status, body = http_json(f"{self.base}/mcp", msg)
        assert status == 200, f"{method} -> HTTP {status}: {body}"
        return body

    def tool(self, name: str, args: Optional[dict] = None) -> Dict[str, Any]:
        resp = self.call("tools/call", {"name": name, "arguments": args or {}})
        result = resp.get("result", {})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        return {"isError": result.get("isError", False), "data": parsed, "raw": text}


def sse_handshake(base: str) -> bool:
    """Verifica el transporte SSE nativo: GET /sse + POST /messages."""
    import threading

    frames: list[str] = []
    error: list[str] = []

    def reader() -> None:
        try:
            req = urllib.request.Request(f"{base}/sse")
            req.add_header("Accept", "text/event-stream")
            with urllib.request.urlopen(req, timeout=20) as resp:
                buf = ""
                for raw in resp:
                    buf += raw.decode(errors="replace")
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        frames.append(frame)
                        if len(frames) >= 2:
                            return
        except Exception as exc:  # noqa: BLE001
            error.append(str(exc))

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    endpoint = None
    for _ in range(60):
        if frames:
            m = re.search(r"data:\s*(/messages\?sessionId=\w+)", frames[0])
            if m:
                endpoint = m.group(1)
                break
        time.sleep(0.2)
    if not endpoint:
        print(f"    (sin evento endpoint; error={error})")
        return False

    status, _ = http_json(
        f"{base}{endpoint}",
        {"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
    )
    if status != 202:
        print(f"    (POST /messages devolvió {status})")
        return False

    for _ in range(50):
        if len(frames) >= 2:
            break
        time.sleep(0.2)
    t.join(timeout=1)
    return len(frames) >= 2 and '"result"' in frames[1] and "read_file" in frames[1]


def main() -> int:
    port = free_port()
    workspace = Path(tempfile.mkdtemp(prefix="mcp_test_"))
    env = {
        **os.environ,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_WORKSPACE_ROOT": str(workspace),
        "MCP_LOG_LEVEL": "warning",
        "MCP_SSE_KEEPALIVE": "5",
    }
    base = f"http://127.0.0.1:{port}"

    print("\n\033[1m=== MCP UNIFIED SERVER · SUITE DE VALIDACIÓN ===\033[0m")
    print(f"  workspace temporal: {workspace}")
    print(f"  puerto: {port}\n")

    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "main.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/health", timeout=2).read()
                ready = True
                break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    print(proc.stdout.read() if proc.stdout else "")
                    return 1
                time.sleep(0.5)
        check("El servidor arranca y responde /health", ready)
        if not ready:
            return 1

        c = Client(base)

        # ---------------- Protocolo -------------------------------------- #
        print("\n\033[1m[1] Protocolo MCP\033[0m")
        init = c.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        )
        check("initialize responde serverInfo", "serverInfo" in init.get("result", {}))
        check(
            "initialize declara capability 'tools'",
            "tools" in init.get("result", {}).get("capabilities", {}),
        )
        status, _ = http_json(f"{base}/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("notifications/initialized -> 202", status == 202)
        check("ping responde", "result" in c.call("ping"))

        listing = c.call("tools/list")
        names = [t["name"] for t in listing["result"]["tools"]]
        required = [
            "read_file", "write_file", "create_directory", "list_directory",
            "move_file", "search_nodes", "run", "run_background",
            "list_background", "kill_background",
        ]
        for name in required:
            check(f"tools/list expone '{name}'", name in names)
        check(f"total de tools = {len(names)}", len(names) >= 10)
        check(
            "cada tool tiene inputSchema válido",
            all(t.get("inputSchema", {}).get("type") == "object" for t in listing["result"]["tools"]),
        )

        # ---------------- Transporte SSE --------------------------------- #
        print("\n\033[1m[2] Transporte SSE\033[0m")
        check("GET /sse emite endpoint y POST /messages devuelve la respuesta", sse_handshake(base))

        # ---------------- Filesystem ------------------------------------- #
        print("\n\033[1m[3] Herramientas de Filesystem\033[0m")
        r = c.tool("create_directory", {"path": "proyecto/src"})
        check("create_directory crea la ruta", not r["isError"] and (workspace / "proyecto/src").is_dir())

        r = c.tool("write_file", {"path": "proyecto/src/app.py", "content": "print('hola MCP')\n"})
        check("write_file escribe el archivo", not r["isError"] and r["data"]["bytes_written"] > 0)

        r = c.tool("read_file", {"path": "proyecto/src/app.py"})
        check("read_file devuelve el contenido UTF-8", r["raw"] == "print('hola MCP')\n")

        c.tool("write_file", {"path": "proyecto/notas.txt", "content": "TODO: revisar despliegue\n"})
        r = c.tool("list_directory", {"path": "proyecto"})
        check("list_directory lista archivos y directorios",
              not r["isError"] and r["data"]["total"] == 2)
        check("list_directory etiqueta el tipo [DIR]/[FILE]",
              any(e["label"].startswith("[DIR]") for e in r["data"]["entries"]))

        r = c.tool("move_file", {"source": "proyecto/notas.txt", "destination": "proyecto/docs/notas.md"})
        check("move_file mueve y renombra",
              not r["isError"] and (workspace / "proyecto/docs/notas.md").exists())

        r = c.tool("search_nodes", {"path": ".", "query": "*.py"})
        check("search_nodes encuentra por patrón glob",
              not r["isError"] and any(m["name"] == "app.py" for m in r["data"]["results"]))
        r = c.tool("search_nodes", {"path": ".", "query": "despliegue", "search_content": True})
        check("search_nodes busca dentro del contenido", r["data"]["total"] >= 1)

        r = c.tool("get_file_info", {"path": "proyecto/src/app.py"})
        check("get_file_info devuelve metadatos", r["data"]["type"] == "file")

        r = c.tool("delete_node", {"path": "proyecto/docs", "recursive": True})
        check("delete_node elimina recursivamente",
              not r["isError"] and not (workspace / "proyecto/docs").exists())

        # ---------------- Seguridad -------------------------------------- #
        print("\n\033[1m[4] Seguridad (sandbox de rutas)\033[0m")
        r = c.tool("read_file", {"path": "../../../etc/passwd"})
        check("bloquea path traversal fuera del workspace", r["isError"] is True)
        r = c.tool("read_file", {"path": "no_existe.txt"})
        check("error controlado si el archivo no existe", r["isError"] is True)
        r = c.tool("write_file", {"path": "x.txt"})
        check("valida parámetros obligatorios faltantes", r["isError"] is True)
        r = c.tool("herramienta_inexistente", {})
        check("rechaza herramientas desconocidas", r["isError"] is True)

        # ---------------- Terminal --------------------------------------- #
        print("\n\033[1m[5] Herramientas de Terminal\033[0m")
        r = c.tool("run", {"command": "echo MCP_OK"})
        check("run ejecuta y devuelve stdout",
              not r["isError"] and "MCP_OK" in r["data"]["stdout"] and r["data"]["exitCode"] == 0)

        r = c.tool("run", {"command": "python -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\""})
        check("run captura stderr y exitCode", r["data"]["exitCode"] == 3 and "boom" in r["data"]["stderr"])

        r = c.tool("run", {"command": "python -c \"import time; time.sleep(5)\"", "timeout": 1})
        check("run aplica el timeout", r["isError"] is True)

        r = c.tool("run_background", {"command": "python -c \"import time;print('bg-start',flush=True);time.sleep(30)\""})
        pid = r["data"].get("processId")
        check("run_background devuelve processId", bool(pid) and r["data"]["started"] is True
              and r["data"]["status"] == "running")

        r = c.tool("list_background")
        check("list_background lista el proceso activo",
              any(p["processId"] == pid for p in r["data"]["processes"]))

        time.sleep(0.7)
        r = c.tool("get_background_output", {"processId": pid})
        check("get_background_output captura la salida", "bg-start" in r["data"]["output"])

        r = c.tool("kill_background", {"processId": pid})
        check("kill_background termina el proceso", r["data"]["status"] == "killed")

        r = c.tool("list_background")
        target = next(p for p in r["data"]["processes"] if p["processId"] == pid)
        check("el proceso queda como 'finished'", target["status"] == "finished")

        r = c.tool("kill_background", {"processId": "proc_inexistente"})
        check("kill_background con id inválido da error controlado", r["isError"] is True)

        r = c.tool("get_system_info")
        check("get_system_info reporta el entorno", "platform" in r["data"])

        # ---------------- Browser (Playwright) --------------------------- #
        print("\n\033[1m[6] Herramientas de Browser (Playwright)\033[0m")
        browser_names = [n for n in names if n.startswith("browser_")]
        check(f"tools/list expone {len(browser_names)} tools browser_*", len(browser_names) >= 55)
        for name in (
            "browser_navigate", "browser_snapshot", "browser_find", "browser_click",
            "browser_type", "browser_fill_form", "browser_take_screenshot",
            "browser_tabs", "browser_console_messages", "browser_network_requests",
            "browser_cookie_set", "browser_localstorage_set", "browser_storage_state",
            "browser_highlight", "browser_start_tracing", "browser_mouse_click_xy",
        ):
            check(f"tools/list expone '{name}'", name in names)

        browser_tools = [t for t in listing["result"]["tools"] if t["name"].startswith("browser_")]
        check(
            "cada tool de browser documenta todas sus propiedades",
            all(
                "type" in spec and "description" in spec
                for t in browser_tools
                for spec in t["inputSchema"].get("properties", {}).values()
            ),
        )

        r = c.tool("browser_get_config", {})
        check("browser_get_config responde sin necesitar navegador", not r["isError"])
        cfg = r["data"]
        check("browser_get_config indica si Playwright está instalado", "playwrightInstalled" in cfg)
        check("browser_get_config expone motor y viewport",
              cfg["engine"] in ("chromium", "firefox", "webkit") and "width" in cfg["viewport"])

        if not cfg["playwrightInstalled"]:
            r = c.tool("browser_navigate", {"url": "https://example.com"})
            check("sin Playwright, browser_navigate devuelve isError (no rompe el servidor)", r["isError"])
            check("el error indica cómo instalar Playwright",
                  "pip install playwright" in r["raw"] and "playwright install" in r["raw"])
            r = c.tool("browser_run_code_unsafe", {"code": "return 1"})
            check("browser_run_code_unsafe está bloqueado por defecto",
                  r["isError"] and "MCP_ENABLE_UNSAFE_BROWSER_CODE" in r["raw"])
        else:
            r = c.tool("browser_navigate", {"url": "about:blank"})
            check("con Playwright, browser_navigate abre la página", not r["isError"])
            c.tool("browser_close", {})

        check("el servidor sigue vivo tras las llamadas de browser",
              "result" in c.call("ping"))

        # ---------------- API Testing & QA ------------------------------- #
        print("\n\033[1m[7] Herramientas de API Testing & QA\033[0m")
        for name in (
            "set_api_auth", "set_session_variable", "build_and_send_request",
            "validate_api_response", "validate_json_schema", "extract_response_data",
            "run_postman_collection", "generate_test_report", "get_api_session",
        ):
            check(f"tools/list expone '{name}'", name in names)

        # El propio servidor MCP hace de API bajo prueba (endpoint /health).
        r = c.tool("set_session_variable", {"key": "baseUrl", "value": base})
        check("set_session_variable crea la variable", not r["isError"]
              and r["data"]["status"] == "created")

        r = c.tool("set_api_auth", {"type": "bearer", "token": "token-de-prueba"})
        check("set_api_auth configura bearer y enmascara el token",
              not r["isError"] and r["data"]["auth"]["token"].startswith("***"))

        r = c.tool("build_and_send_request",
                   {"method": "GET", "url": "{{baseUrl}}/health", "name": "Health check"})
        check("build_and_send_request ejecuta la petición end-to-end",
              not r["isError"] and r["data"]["statusCode"] == 200, r["raw"][:200])
        check("devuelve statusCode, responseTimeMs, responseHeaders y responseBody",
              {"statusCode", "responseTimeMs", "responseHeaders", "responseBody"}
              <= set(r["data"]))
        check("interpola {{baseUrl}} en la URL", r["data"]["url"].endswith("/health"))
        check("el cuerpo se parsea como JSON",
              r["data"]["jsonParsed"] and r["data"]["responseBody"]["status"] == "ok")

        r = c.tool("validate_api_response", {
            "expectedStatus": 200,
            "maxResponseTimeMs": 10000,
            "requiredFields": ["status", "tools"],
            "valueAssertions": [
                {"jsonPath": "$.status", "operator": "equals", "expected": "ok"},
                {"jsonPath": "$.tools", "operator": "greaterThan", "expected": 10},
            ],
        })
        check("validate_api_response supera las aserciones",
              not r["isError"] and r["data"]["passed"] is True, r["raw"][:300])
        check("informa del recuento de aserciones", r["data"]["total"] == 6,
              str(r["data"]["total"]))

        r = c.tool("validate_json_schema", {"schema": {
            "type": "object", "required": ["status", "tools"],
            "properties": {"status": {"type": "string", "enum": ["ok"]},
                           "tools": {"type": "integer", "minimum": 1}},
        }})
        check("validate_json_schema valida el contrato",
              not r["isError"] and r["data"]["valid"] is True, r["raw"][:300])

        r = c.tool("validate_json_schema", {"schema": {
            "type": "object", "properties": {"tools": {"type": "string"}}}})
        check("validate_json_schema detecta incumplimientos",
              not r["isError"] and r["data"]["valid"] is False
              and r["data"]["errorCount"] >= 1)

        r = c.tool("extract_response_data",
                   {"jsonPath": "$.tools", "variableName": "totalTools"})
        check("extract_response_data guarda la variable de sesión",
              not r["isError"] and r["data"]["savedAs"] == "totalTools")

        r = c.tool("generate_test_report", {
            "suiteName": "Humo MCP", "environment": "local",
            "includeResponseBody": True, "outputPath": "informes/humo.md",
        })
        check("generate_test_report produce Markdown BDD",
              not r["isError"] and "**Dado** que" in r["data"]["markdown"]
              and "**Cuando**" in r["data"]["markdown"]
              and "**Entonces**" in r["data"]["markdown"], r["raw"][:300])
        check("el informe se escribe en el workspace",
              (workspace / "informes" / "humo.md").is_file())
        check("el informe refleja el veredicto", r["data"]["verdict"] == "RECHAZADO")

        r = c.tool("get_api_session", {})
        check("get_api_session expone el estado de la sesión",
              not r["isError"] and r["data"]["stats"]["requests"] >= 1)
        c.tool("get_api_session", {"reset": True})

        r = c.tool("build_and_send_request", {"method": "GET", "url": "http://127.0.0.1:1/x",
                                              "timeoutSeconds": 2})
        check("un fallo de red devuelve isError sin tumbar el servidor", r["isError"])
        check("el servidor sigue vivo tras las llamadas de API testing",
              "result" in c.call("ping"))

        # ---------------- Endpoints auxiliares --------------------------- #
        print("\n\033[1m[8] Endpoints HTTP auxiliares\033[0m")
        status, body = http_json(f"{base}/", None, method="GET")
        check("GET / devuelve la ficha del servidor", status == 200 and body["toolCount"] >= 10)
        status, body = http_json(f"{base}/health", None, method="GET")
        check("GET /health -> status ok", status == 200 and body["status"] == "ok")
        status, body = http_json(f"{base}/tools", None, method="GET")
        check("GET /tools devuelve el catálogo", status == 200 and len(body["tools"]) >= 10)

        # POST /mcp con Accept solo SSE debe responder un frame text/event-stream
        req = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 500, "method": "ping"}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = resp.headers.get("content-type", "")
            frame = resp.read().decode()
        check(
            "POST /mcp responde SSE cuando el cliente solo acepta event-stream",
            "text/event-stream" in ctype and frame.startswith("event: message"),
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workspace, ignore_errors=True)

    total = PASSED + FAILED
    print("\n" + "=" * 60)
    if FAILED == 0:
        print(f"\033[0;32m\033[1m  ✅  {PASSED}/{total} PRUEBAS SUPERADAS\033[0m")
    else:
        print(f"\033[0;31m\033[1m  ❌  {FAILED} de {total} pruebas fallaron\033[0m")
    print("=" * 60 + "\n")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

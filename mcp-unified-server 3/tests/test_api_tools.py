#!/usr/bin/env python3
"""
Suite de pruebas del grupo de herramientas de API Testing & QA (Módulo C).

A diferencia de un mock puro, estas pruebas levantan un **servidor HTTP real en
loopback** (http.server en un hilo) que hace de API bajo prueba: eco de método,
cabeceras, query, cuerpo JSON/form/multipart, códigos de estado a demanda,
latencia simulada, redirecciones y un endpoint OAuth2 de token. Así se ejercita
de verdad httpx, la construcción del multipart, la autenticación, el motor de
aserciones, el validador de JSON Schema, el motor JSONPath y el runner de
colecciones Postman (incluido el shim de `pm` sobre Node).

Uso:  python3 tests/test_api_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKDIR = tempfile.mkdtemp(prefix="mcp-api-tests-")
os.environ["MCP_WORKSPACE_ROOT"] = WORKDIR
os.environ.setdefault("MCP_ENABLE_API_TESTING", "true")

from server.api.jsonpath import jsonpath  # noqa: E402
from server.api.schema import validate_schema  # noqa: E402
from server.api.state import session  # noqa: E402
from server.core.registry import ToolError, registry  # noqa: E402
import server.tools.apitesting  # noqa: E402,F401

PASSED = 0
FAILED: List[str] = []
BASE = ""


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
    try:
        await registry.call(_tool, kwargs)
    except ToolError:
        check(_label, True)
    except Exception as exc:  # noqa: BLE001
        check(_label, False, f"se esperaba ToolError y llegó {type(exc).__name__}: {exc}")
    else:
        check(_label, False, "no lanzó ningún error")


def set_setting(field: str, value: Any) -> Any:
    """`settings` es un dataclass congelado; para las pruebas se fuerza el valor."""
    from server.config import settings as cfg

    previous = getattr(cfg, field)
    object.__setattr__(cfg, field, value)
    return previous


# --------------------------------------------------------------------------- #
# API bajo prueba
# --------------------------------------------------------------------------- #
USERS = [
    {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com", "active": True, "score": 97.5},
    {"id": 2, "name": "Alan Turing", "email": "alan@example.com", "active": True, "score": 91.0},
    {"id": 3, "name": "Grace Hopper", "email": "grace@example.com", "active": False, "score": 88.25},
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silencio
        pass

    # -- utilidades ---------------------------------------------------------- #
    def _send(self, code: int, payload: Any, headers: Dict[str, str] | None = None) -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
            ctype = "application/json"
        else:
            body = str(payload).encode("utf-8")
            ctype = "text/plain; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", "req-abc-123")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _echo(self) -> Dict[str, Any]:
        parsed = urlparse(self.path)
        raw = self._read()
        ctype = self.headers.get("Content-Type", "")
        body: Any = raw.decode("utf-8", "replace")
        if "json" in ctype and raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return {
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "contentType": ctype,
            "body": body,
            "bodyRaw": raw.decode("utf-8", "replace"),
        }

    # -- rutas --------------------------------------------------------------- #
    def _route(self) -> None:
        path = urlparse(self.path).path
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        if path == "/users":
            if self.command == "POST":
                data = self._echo()
                created = {"id": 99, "created": True}
                if isinstance(data["body"], dict):
                    created.update(data["body"])
                self._send(201, {"data": created, "echo": data}, {"Location": "/users/99"})
                return
            self._send(200, {"data": USERS, "total": len(USERS), "page": 1})
            return

        if path.startswith("/users/"):
            uid = path.rsplit("/", 1)[-1]
            for u in USERS:
                if str(u["id"]) == uid:
                    self._send(200, {"data": u})
                    return
            self._send(404, {"error": "not found", "id": uid})
            return

        if path == "/status":
            code = int(query.get("code", "200"))
            self._send(code, {"requested": code})
            return

        if path == "/slow":
            time.sleep(float(query.get("ms", "300")) / 1000)
            self._send(200, {"slow": True})
            return

        if path == "/whoami":
            self._send(200, {
                "authorization": self.headers.get("Authorization"),
                "apiKey": self.headers.get("X-API-Key"),
                "custom": self.headers.get("X-Custom"),
            })
            return

        if path == "/echo":
            self._send(200, self._echo())
            return

        if path == "/token":
            self._send(200, {
                "access_token": "tok-oauth-777",
                "token_type": "bearer",
                "expires_in": 3600,
            })
            return

        if path == "/text":
            self._send(200, "hola mundo: codigo=ABC-42 fin")
            return

        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/users/1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._send(404, {"error": "unknown route", "path": path})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route  # type: ignore[assignment]


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address[0], srv.server_address[1]
    return srv, f"http://{host}:{port}"


# --------------------------------------------------------------------------- #
# [1] Motores internos: JSONPath y JSON Schema
# --------------------------------------------------------------------------- #
async def test_engines() -> None:
    print("\n\033[1m[1] Motores internos (JSONPath / JSON Schema)\033[0m")
    data = {
        "data": {"items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": ["c"]}],
                 "total": 2},
        "meta": {"ok": True},
    }
    check("JSONPath campo simple", jsonpath(data, "$.data.total") == [2])
    check("JSONPath índice de array", jsonpath(data, "$.data.items[1].id") == [2])
    check("JSONPath índice negativo", jsonpath(data, "$.data.items[-1].id") == [2])
    check("JSONPath wildcard", jsonpath(data, "$.data.items[*].id") == [1, 2])
    check("JSONPath slice", jsonpath(data, "$.data.items[0:1]") == [data["data"]["items"][0]])
    check("JSONPath descenso recursivo", sorted(jsonpath(data, "$..id")) == [1, 2])
    check("JSONPath notación corchete", jsonpath(data, "$['meta']['ok']") == [True])
    check("JSONPath filtro", jsonpath(data, "$.data.items[?(@.id==2)].id") == [2])
    check("JSONPath sin coincidencias devuelve []", jsonpath(data, "$.nope.nada") == [])

    schema = {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["total", "items"],
                "properties": {
                    "total": {"type": "integer", "minimum": 0},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "integer"}},
                        },
                    },
                },
            }
        },
    }
    check("JSON Schema válido -> sin errores", validate_schema(data, schema) == [])
    bad = {"data": {"total": -1, "items": []}}
    errs = validate_schema(bad, schema)
    check("JSON Schema detecta minimum y minItems", len(errs) >= 2, f"errores={errs}")
    check(
        "JSON Schema reporta instancePath",
        any("data.total" in e["instancePath"] for e in errs),
        str(errs),
    )
    check("JSON Schema tipo incorrecto", validate_schema({"data": "x"}, schema) != [])
    check(
        "JSON Schema enum",
        validate_schema("z", {"enum": ["a", "b"]}) != []
        and validate_schema("a", {"enum": ["a", "b"]}) == [],
    )
    check(
        "JSON Schema $ref interno",
        validate_schema(
            {"u": {"id": 1}},
            {"type": "object", "properties": {"u": {"$ref": "#/definitions/U"}},
             "definitions": {"U": {"type": "object", "required": ["id"]}}},
        ) == [],
    )


# --------------------------------------------------------------------------- #
# [2] set_api_auth
# --------------------------------------------------------------------------- #
async def test_auth() -> None:
    print("\n\033[1m[2] set_api_auth\033[0m")
    r = await call("set_api_auth", type="bearer", token="abc123secreto")
    check("bearer configurado", r["auth"]["type"] == "bearer")
    check("token enmascarado en la respuesta", r["auth"]["token"].startswith("***"), str(r["auth"]))
    check("Authorization derivado", session.auth_headers()["Authorization"] == "Bearer abc123secreto")
    check("appliedHeaders redactado", r["appliedHeaders"]["Authorization"].startswith("***"))

    r = await call("set_api_auth", type="basic", username="user", password="pass")
    check("basic -> Basic base64", session.auth_headers()["Authorization"] == "Basic dXNlcjpwYXNz")

    r = await call("set_api_auth", type="apiKey", token="key-9", headerName="X-API-Key")
    check("apiKey usa headerName", session.auth_headers() == {"X-API-Key": "key-9"})

    r = await call("set_api_auth", type="apiKey", token="key-9")
    check("apiKey por defecto X-API-Key", "X-API-Key" in session.auth_headers())

    # OAuth2 real contra el endpoint /token del servidor local
    r = await call(
        "set_api_auth", type="oauth2", tokenUrl=f"{BASE}/token",
        username="client-id", password="client-secret",
    )
    check("oauth2 obtiene access_token", r["oauth2"]["expiresIn"] == 3600, json.dumps(r["oauth2"]))
    check("oauth2 token aplicado", session.auth_headers()["Authorization"] == "Bearer tok-oauth-777")
    check("oauth2 token enmascarado", r["oauth2"]["accessToken"].startswith("***"))

    r = await call("set_api_auth", type="none")
    check("type=none limpia la auth", session.auth_headers() == {} and r["status"] == "cleared")

    await expect_error("tipo de auth inválido -> ToolError", "set_api_auth", type="jwt")
    await expect_error("bearer sin token -> ToolError", "set_api_auth", type="bearer")
    await expect_error("oauth2 con tokenUrl y sin client -> ToolError",
                       "set_api_auth", type="oauth2", tokenUrl=f"{BASE}/token")


# --------------------------------------------------------------------------- #
# [3] set_session_variable
# --------------------------------------------------------------------------- #
async def test_variables() -> None:
    print("\n\033[1m[3] set_session_variable\033[0m")
    r = await call("set_session_variable", key="userId", value=2)
    check("crea variable numérica", r["status"] == "created" and session.get_var("userId") == 2)
    r = await call("set_session_variable", key="userId", value=3)
    check("actualiza variable", r["status"] == "updated" and r["previousValue"] == 2)
    r = await call("set_session_variable", key="payload", value={"a": [1, 2]})
    check("acepta objetos", session.get_var("payload") == {"a": [1, 2]})
    r = await call("set_session_variable", key="flag", value=True)
    check("acepta booleanos", session.get_var("flag") is True and r["type"] == "bool")
    r = await call("set_session_variable", key="secreto", value="supersecreto", secret=True)
    check("secret=true enmascara el valor mostrado", r["value"].startswith("***"))
    check("interpolación de tipo exacto", session.interpolate("{{userId}}") == 3)
    check("interpolación embebida", session.interpolate("/users/{{userId}}/x") == "/users/3/x")
    check("interpolación en dict", session.interpolate({"k": "{{flag}}"}) == {"k": True})
    check("variable inexistente se conserva", session.interpolate("{{nope}}") == "{{nope}}")
    check("detección de no resueltas", session.unresolved("{{nope}}/{{userId}}") == ["nope"])
    r = await call("set_session_variable", key="flag", unset=True)
    check("unset elimina", r["status"] == "unset" and "flag" not in session.variables)
    r = await call("set_session_variable", key="flag", unset=True)
    check("unset de inexistente -> not_found", r["status"] == "not_found")
    await expect_error("key vacía -> ToolError", "set_session_variable", key="   ")


# --------------------------------------------------------------------------- #
# [4] build_and_send_request
# --------------------------------------------------------------------------- #
async def test_requests() -> None:
    print("\n\033[1m[4] build_and_send_request\033[0m")
    await call("get_api_session", reset=True)

    r = await call("build_and_send_request", method="GET", url=f"{BASE}/users")
    check("GET 200", r["statusCode"] == 200 and r["ok"] is True)
    check("cuerpo JSON parseado", r["jsonParsed"] and r["responseBody"]["total"] == 3)
    check("tiempo de respuesta medido", isinstance(r["responseTimeMs"], float) and r["responseTimeMs"] > 0)
    check("cabeceras de respuesta devueltas", r["responseHeaders"]["x-request-id"] == "req-abc-123")
    check("índice de petición", r["requestIndex"] == 1)

    r = await call(
        "build_and_send_request", method="POST", url=f"{BASE}/users",
        bodyType="json", body={"name": "Nuevo", "score": 10},
        headers={"X-Custom": "si"}, name="Crear usuario",
    )
    check("POST 201", r["statusCode"] == 201)
    check("body JSON enviado", r["responseBody"]["echo"]["body"] == {"name": "Nuevo", "score": 10})
    check("Content-Type json automático",
          "application/json" in r["responseBody"]["echo"]["contentType"])
    check("cabecera personalizada enviada", r["responseBody"]["echo"]["headers"]["x-custom"] == "si")

    r = await call(
        "build_and_send_request", method="GET", url=f"{BASE}/echo",
        queryParams={"page": 2, "q": "ada"},
    )
    check("query params", r["responseBody"]["query"] == {"page": "2", "q": "ada"})

    r = await call(
        "build_and_send_request", method="POST", url=f"{BASE}/echo",
        bodyType="x-www-form-urlencoded", body={"a": "1", "b": "dos"},
    )
    check("form urlencoded", r["responseBody"]["bodyRaw"] == "a=1&b=dos", r["responseBody"]["bodyRaw"])
    check("Content-Type urlencoded",
          "urlencoded" in r["responseBody"]["contentType"], r["responseBody"]["contentType"])

    r = await call(
        "build_and_send_request", method="POST", url=f"{BASE}/echo",
        bodyType="raw", body="texto plano <xml/>",
    )
    check("raw", r["responseBody"]["bodyRaw"] == "texto plano <xml/>")

    # multipart real con archivo del workspace
    upload = Path(WORKDIR) / "adjunto.txt"
    upload.write_text("contenido del adjunto", encoding="utf-8")
    r = await call(
        "build_and_send_request", method="POST", url=f"{BASE}/echo",
        bodyType="form-data", body={"campo": "valor"},
        files=[{"fieldName": "archivo", "filePath": "adjunto.txt"}],
    )
    raw = r["responseBody"]["bodyRaw"]
    check("multipart content-type", "multipart/form-data; boundary=" in r["responseBody"]["contentType"])
    check("multipart incluye el campo", 'name="campo"' in raw and "valor" in raw)
    check("multipart incluye el archivo",
          'name="archivo"; filename="adjunto.txt"' in raw and "contenido del adjunto" in raw, raw[:300])

    # auth aplicada automáticamente
    await call("set_api_auth", type="bearer", token="tk-auto")
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/whoami")
    check("auth global aplicada", r["responseBody"]["authorization"] == "Bearer tk-auto")
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/whoami",
                   headers={"Authorization": "Bearer manual"})
    check("cabecera explícita gana a la auth global",
          r["responseBody"]["authorization"] == "Bearer manual")
    await call("set_api_auth", type="none")

    # interpolación de variables en URL y body
    await call("set_session_variable", key="uid", value=1)
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/users/{{{{uid}}}}")
    check("interpolación en URL", r["responseBody"]["data"]["name"] == "Ada Lovelace")
    r = await call("build_and_send_request", method="POST", url=f"{BASE}/echo",
                   body={"ref": "{{uid}}"})
    check("interpolación en body preserva tipo", r["responseBody"]["body"] == {"ref": 1})

    r = await call("build_and_send_request", method="GET", url=f"{BASE}/users/{{{{falta}}}}")
    check("avisa de variables sin resolver", r.get("unresolvedVariables") == ["falta"])

    # estados y redirección
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/status",
                   queryParams={"code": 500})
    check("500 no lanza error, se registra", r["statusCode"] == 500 and r["ok"] is False)
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/redirect")
    check("sigue redirecciones", r["statusCode"] == 200 and r["redirected"] is True)
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/redirect",
                   followRedirects=False)
    check("followRedirects=false", r["statusCode"] == 302)

    r = await call("build_and_send_request", method="GET", url=f"{BASE}/text")
    check("respuesta no JSON se guarda como texto",
          r["jsonParsed"] is False and "hola mundo" in r["responseBody"])

    # errores
    await expect_error("método inválido -> ToolError", "build_and_send_request",
                       method="FETCH", url=f"{BASE}/users")
    await expect_error("bodyType inválido -> ToolError", "build_and_send_request",
                       method="POST", url=f"{BASE}/echo", bodyType="xml", body="x")
    await expect_error("json malformado -> ToolError", "build_and_send_request",
                       method="POST", url=f"{BASE}/echo", bodyType="json", body="{no-json")
    await expect_error("archivo inexistente -> ToolError", "build_and_send_request",
                       method="POST", url=f"{BASE}/echo",
                       files=[{"fieldName": "f", "filePath": "no-existe.txt"}])
    await expect_error("host inalcanzable -> ToolError", "build_and_send_request",
                       method="GET", url="http://127.0.0.1:1/nope", timeoutSeconds=2)
    check("el fallo de red queda en el historial",
          session.history[-1].error is not None and session.history[-1].status_code == 0)

    # allowlist de hosts
    original = set_setting("api_host_allowlist", "^api\\.empresa\\.com$")
    await expect_error("allowlist bloquea host no permitido", "build_and_send_request",
                       method="GET", url=f"{BASE}/users")
    set_setting("api_host_allowlist", original)
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/users")
    check("allowlist vacía permite todo", r["statusCode"] == 200)


# --------------------------------------------------------------------------- #
# [5] validate_api_response
# --------------------------------------------------------------------------- #
async def test_validations() -> None:
    print("\n\033[1m[5] validate_api_response\033[0m")
    await call("get_api_session", reset=True)
    await call("build_and_send_request", method="GET", url=f"{BASE}/users", name="Listar usuarios")

    r = await call(
        "validate_api_response",
        expectedStatus=200,
        maxResponseTimeMs=5000,
        requiredFields=["data", "total", "$.data[0].email"],
        valueAssertions=[
            {"jsonPath": "$.total", "operator": "equals", "expected": 3},
            {"jsonPath": "$.data[0].name", "operator": "contains", "expected": "Ada"},
            {"jsonPath": "$.data[0].id", "operator": "notNull"},
            {"jsonPath": "$.data[0].score", "operator": "greaterThan", "expected": 90},
            {"jsonPath": "$.data[2].score", "operator": "lessThan", "expected": 90},
            {"jsonPath": "$.data", "operator": "length", "expected": 3},
            {"jsonPath": "$.data[0].email", "operator": "matches", "expected": "^[^@]+@example\\.com$"},
            {"jsonPath": "$.page", "operator": "in", "expected": [1, 2]},
            {"jsonPath": "$.data[0].active", "operator": "type", "expected": "boolean"},
            {"jsonPath": "$.data", "operator": "notEmpty"},
        ],
        expectedHeaders={"content-type": "application/json", "x-request-id": "*"},
        bodyContains="Lovelace",
    )
    check("todas las aserciones pasan", r["passed"] is True, json.dumps(r["failures"])[:400])
    check("cuenta total de aserciones", r["total"] == 18, str(r["total"]))
    check("cada aserción tiene nombre legible",
          all(a["name"] for a in r["assertions"]))
    check("estadísticas de sesión incluidas", r["sessionStats"]["assertions"] == 18,
          json.dumps(r["sessionStats"]))

    r = await call("validate_api_response", expectedStatus=404,
                   valueAssertions=[{"jsonPath": "$.total", "operator": "equals", "expected": 99}])
    check("aserciones fallidas se reportan", r["passed"] is False and r["failedCount"] == 2)
    check("detalle del fallo incluye el valor real",
          any("3" in str(f["actual"]) for f in r["failures"]), json.dumps(r["failures"]))

    await expect_error("failFast lanza ToolError", "validate_api_response",
                       expectedStatus=418, failFast=True)

    # SLA
    await call("build_and_send_request", method="GET", url=f"{BASE}/slow",
               queryParams={"ms": 250}, name="Endpoint lento")
    r = await call("validate_api_response", maxResponseTimeMs=10)
    check("SLA excedido se detecta", r["passed"] is False)
    check("mensaje de SLA indica el exceso", "Excedido" in r["failures"][0]["detail"])
    r = await call("validate_api_response", maxResponseTimeMs=30000)
    check("SLA cumplido", r["passed"] is True)

    # campo ausente
    r = await call("validate_api_response", requiredFields=["$.noExiste"])
    check("campo ausente falla", r["passed"] is False)

    await expect_error("sin ninguna validación -> ToolError", "validate_api_response")
    await expect_error("operador desconocido -> ToolError", "validate_api_response",
                       valueAssertions=[{"jsonPath": "$.x", "operator": "casiIgual", "expected": 1}])
    await expect_error("requestIndex inexistente -> ToolError", "validate_api_response",
                       expectedStatus=200, requestIndex=999)

    r = await call("validate_api_response", expectedStatus=200, requestIndex=1)
    check("valida una petición histórica concreta", r["requestIndex"] == 1 and r["passed"] is True)


# --------------------------------------------------------------------------- #
# [6] validate_json_schema
# --------------------------------------------------------------------------- #
async def test_schema_tool() -> None:
    print("\n\033[1m[6] validate_json_schema\033[0m")
    await call("get_api_session", reset=True)
    await call("build_and_send_request", method="GET", url=f"{BASE}/users/1")

    schema = {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["id", "name", "email", "active"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "name": {"type": "string", "minLength": 3},
                    "email": {"type": "string", "format": "email"},
                    "active": {"type": "boolean"},
                    "score": {"type": "number"},
                },
                "additionalProperties": False,
            }
        },
    }
    r = await call("validate_json_schema", schema=schema)
    check("esquema cumplido", r["valid"] is True, json.dumps(r["errors"])[:400])

    r = await call("validate_json_schema", schema=json.dumps(schema))
    check("esquema como string JSON", r["valid"] is True)

    r = await call("validate_json_schema", schema={"type": "object", "required": ["id"]},
                   jsonPath="$.data")
    check("jsonPath acota el ámbito de validación", r["valid"] is True and r["validatedPath"] == "$.data")

    bad = {"type": "object", "properties": {"data": {"type": "object",
           "required": ["telefono"], "properties": {"id": {"type": "string"}}}}}
    r = await call("validate_json_schema", schema=bad)
    check("incumplimientos detectados", r["valid"] is False and r["errorCount"] >= 2)
    check("errores con instancePath y keyword",
          all({"instancePath", "keyword", "message"} <= set(e) for e in r["errors"]))

    # esquema desde archivo
    sp = Path(WORKDIR) / "user.schema.json"
    sp.write_text(json.dumps(schema), encoding="utf-8")
    r = await call("validate_json_schema", schemaPath="user.schema.json")
    check("esquema cargado desde archivo", r["valid"] is True and "user.schema.json" in r["schemaSource"])

    await expect_error("failFast en esquema incumplido", "validate_json_schema",
                       schema=bad, failFast=True)
    await expect_error("sin schema ni schemaPath -> ToolError", "validate_json_schema")
    await expect_error("schema no JSON -> ToolError", "validate_json_schema", schema="{roto")

    await call("build_and_send_request", method="GET", url=f"{BASE}/text")
    await expect_error("respuesta no JSON -> ToolError", "validate_json_schema",
                       schema={"type": "object"})


# --------------------------------------------------------------------------- #
# [7] extract_response_data
# --------------------------------------------------------------------------- #
async def test_extraction() -> None:
    print("\n\033[1m[7] extract_response_data\033[0m")
    await call("get_api_session", reset=True)
    await call("build_and_send_request", method="GET", url=f"{BASE}/users")

    r = await call("extract_response_data", jsonPath="$.data[0].id", variableName="primerId")
    check("extrae por JSONPath", r["value"] == 1 and r["savedAs"] == "primerId")
    check("valor guardado en sesión", session.get_var("primerId") == 1)
    check("sugerencia de uso", r["usage"] == "{{primerId}}")

    r = await call("extract_response_data", jsonPath="data[1].email", variableName="mail")
    check("acepta ruta con puntos sin '$'", r["value"] == "alan@example.com")

    r = await call("extract_response_data", jsonPath="$.data[*].name", all=True, variableName="nombres")
    check("all=true devuelve array", len(r["value"]) == 3 and r["type"] == "array")

    r = await call("extract_response_data", header="x-request-id", variableName="reqId")
    check("extrae de cabecera", r["value"] == "req-abc-123" and r["method"] == "header")

    r = await call("extract_response_data", jsonPath="$.noExiste", defaultValue="fallback")
    check("defaultValue evita el error", r["value"] == "fallback")

    r = await call("extract_response_data", jsonPath="$.total")
    check("sin variableName no guarda", r["savedAs"] is None and r["value"] == 3)

    await call("build_and_send_request", method="GET", url=f"{BASE}/text")
    r = await call("extract_response_data", regex="codigo=([A-Z0-9-]+)", variableName="codigo")
    check("extrae por regex con grupo", r["value"] == "ABC-42" and r["method"] == "regex")

    await expect_error("sin coincidencias -> ToolError", "extract_response_data",
                       jsonPath="$.nada.de.nada")
    await expect_error("sin expresión -> ToolError", "extract_response_data", variableName="x")
    await expect_error("regex inválida -> ToolError", "extract_response_data", regex="([")

    # encadenamiento real: extraer y reutilizar
    await call("build_and_send_request", method="GET", url=f"{BASE}/users")
    await call("extract_response_data", jsonPath="$.data[2].id", variableName="targetId")
    r = await call("build_and_send_request", method="GET", url=f"{BASE}/users/{{{{targetId}}}}")
    check("encadenamiento extract -> request",
          r["responseBody"]["data"]["name"] == "Grace Hopper")


# --------------------------------------------------------------------------- #
# [8] run_postman_collection
# --------------------------------------------------------------------------- #
def build_collection(base: str) -> Dict[str, Any]:
    return {
        "info": {"name": "Suite de regresión demo", "schema":
                 "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "baseUrl", "value": base}],
        "item": [
            {
                "name": "Listar usuarios",
                "request": {
                    "method": "GET",
                    "url": {"raw": "{{baseUrl}}/users", "host": ["{{baseUrl}}"], "path": ["users"]},
                    "header": [{"key": "Accept", "value": "application/json"}],
                },
                "event": [{
                    "listen": "test",
                    "script": {"exec": [
                        "pm.test('estado 200', function () { pm.response.to.have.status(200); });",
                        "var j = pm.response.json();",
                        "pm.test('hay 3 usuarios', function () { pm.expect(j.data).to.have.lengthOf(3); });",
                        "pm.environment.set('idExtraido', j.data[0].id);",
                    ]}
                }],
            },
            {
                "name": "Carpeta anidada",
                "item": [{
                    "name": "Detalle de usuario",
                    "request": {
                        "method": "GET",
                        "url": {"raw": "{{baseUrl}}/users/{{idExtraido}}"},
                    },
                    "event": [{
                        "listen": "test",
                        "script": {"exec": [
                            "pm.test('nombre correcto', function () {",
                            "  pm.expect(pm.response.json().data.name).to.eql('Ada Lovelace');",
                            "});",
                            "pm.test('SLA 5s', function () { pm.expect(pm.response.responseTime).to.be.below(5000); });",
                        ]}
                    }],
                }],
            },
            {
                "name": "Crear usuario",
                "request": {
                    "method": "POST",
                    "url": {"raw": "{{baseUrl}}/users"},
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": {"mode": "raw", "raw": '{"name": "{{nombreNuevo}}"}',
                             "options": {"raw": {"language": "json"}}},
                },
                "event": [{
                    "listen": "test",
                    "script": {"exec": [
                        "pm.test('creado 201', function () { pm.response.to.have.status(201); });",
                        "pm.test('eco del nombre', function () {",
                        "  pm.expect(pm.response.json().data.name).to.eql('Marie Curie');",
                        "});",
                    ]}
                }],
            },
            {
                "name": "Debe fallar",
                "request": {"method": "GET", "url": {"raw": "{{baseUrl}}/status?code=500"}},
                "event": [{
                    "listen": "test",
                    "script": {"exec": [
                        "pm.test('espera 200 pero llega 500', function () { pm.response.to.have.status(200); });",
                    ]}
                }],
            },
        ],
    }


async def test_collections() -> None:
    print("\n\033[1m[8] run_postman_collection\033[0m")
    from server.api import postman as pm

    await call("get_api_session", reset=True)

    coll = Path(WORKDIR) / "coleccion.json"
    coll.write_text(json.dumps(build_collection(BASE)), encoding="utf-8")
    env = Path(WORKDIR) / "entorno.json"
    env.write_text(json.dumps({
        "name": "QA",
        "values": [{"key": "nombreNuevo", "value": "Marie Curie", "enabled": True}],
    }), encoding="utf-8")

    node = pm.have_node()
    print(f"  \033[90m· Node detectado: {node or 'no'} · newman: {pm.find_newman() or 'no'}\033[0m")

    r = await call("run_postman_collection", collectionPath="coleccion.json",
                   environmentPath="entorno.json")
    check("runner reportado", r["runner"] in ("newman", "native", "python"), r["runner"])
    check("ejecuta las 4 peticiones", r["requests"]["total"] == 4, json.dumps(r["requests"]))
    check("recorre carpetas anidadas",
          any("Carpeta anidada/Detalle de usuario" == e["name"] for e in r["executions"]),
          str([e["name"] for e in r["executions"]]))
    check("resuelve {{baseUrl}} de las variables de colección",
          all(e["url"].startswith(BASE) for e in r["executions"]))
    check("usa el environment para {{nombreNuevo}}",
          any(e["name"] == "Crear usuario" and e["statusCode"] == 201 for e in r["executions"]))
    check("detecta el fallo esperado", r["passed"] is False and r["assertions"]["failed"] >= 1)
    check("lista de failures poblada", len(r["failures"]) >= 1, json.dumps(r["failures"])[:300])
    check("tiempo medio calculado", r["avgResponseTimeMs"] is not None)
    check("nombre de la colección", r["collection"]["name"] == "Suite de regresión demo")

    if node:
        check("ejecuta scripts pm.* en Node", r["runner"] == "native")
        check("cuenta las aserciones de los scripts", r["assertions"]["total"] >= 7,
              str(r["assertions"]))
        check("pm.environment.set encadena entre peticiones",
              "idExtraido" in r["variablesAfterRun"])
        check("pm.expect(...).to.eql funciona",
              any(a["name"] == "nombre correcto" and a["passed"]
                  for e in r["executions"] for a in e["assertions"]),
              json.dumps([a for e in r["executions"] for a in e["assertions"]])[:400])
        check("sin errores de script", r["scriptErrors"] == [], json.dumps(r["scriptErrors"])[:300])

        # el shim aislado
        import subprocess
        shimfile = Path(WORKDIR) / "pm-shim.js"
        shimfile.write_text(pm.NODE_SHIM, encoding="utf-8")
        syntax = subprocess.run([node, "--check", str(shimfile)],
                                capture_output=True, text=True)
        check("shim: sintaxis JavaScript válida", syntax.returncode == 0,
              syntax.stderr[:300])

        out = pm.run_scripts_node(node, "pm.test('x', function(){ pm.expect(1).to.eql(1); });",
                                  {}, {"code": 200, "text": "{}", "responseTime": 5, "headers": {}})
        check("shim: test que pasa", out["tests"] == [{"name": "x", "passed": True, "error": None}]
              or (out["tests"][0]["passed"] is True), json.dumps(out))
        out = pm.run_scripts_node(node, "pm.test('y', function(){ pm.expect(1).to.eql(2); });",
                                  {}, {"code": 200, "text": "{}", "responseTime": 5, "headers": {}})
        check("shim: test que falla", out["tests"][0]["passed"] is False)
        out = pm.run_scripts_node(node, "pm.environment.set('a', 42);", {}, None)
        check("shim: variables de vuelta", out["vars"].get("a") == 42, json.dumps(out))
        out = pm.run_scripts_node(node, "esto no es javascript válido {{{", {}, None)
        check("shim: error de sintaxis reportado", bool(out["error"]))

    r = await call("run_postman_collection", collectionPath="coleccion.json",
                   environmentPath="entorno.json", folder="Carpeta anidada")
    check("filtra por carpeta", r["requests"]["total"] == 1)

    r = await call("run_postman_collection", collectionPath="coleccion.json",
                   environmentPath="entorno.json", bail=True)
    check("bail detiene tras el primer fallo", r["stoppedEarly"] is True)

    # iteración con datos
    data = Path(WORKDIR) / "datos.csv"
    data.write_text("nombreNuevo\nMarie Curie\nMarie Curie\n", encoding="utf-8")
    r = await call("run_postman_collection", collectionPath="coleccion.json",
                   environmentPath="entorno.json", iterationData="datos.csv",
                   folder="Carpeta anidada")
    check("iterationData CSV produce 2 iteraciones", r["iterations"]["total"] == 2)

    await expect_error("colección inexistente -> ToolError", "run_postman_collection",
                       collectionPath="no-existe.json")
    await expect_error("carpeta inexistente -> ToolError", "run_postman_collection",
                       collectionPath="coleccion.json", folder="Fantasma")
    if not pm.find_newman():
        await expect_error("runner=newman sin binario -> ToolError", "run_postman_collection",
                           collectionPath="coleccion.json", runner="newman")

    roto = Path(WORKDIR) / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    await expect_error("colección con JSON inválido -> ToolError", "run_postman_collection",
                       collectionPath="roto.json")

    # runner con newman simulado
    await test_newman_mock()


async def test_newman_mock() -> None:
    """Valida la rama 'newman' con un binario simulado en el PATH."""
    from server.api import postman as pm

    if pm.find_newman():
        return  # hay newman de verdad; nada que simular

    binroot = Path(WORKDIR) / "mockbin"
    binroot.mkdir(exist_ok=True)
    fake = binroot / "newman"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--reporter-json-export') + 1]\n"
        "report = {'run': {\n"
        "  'stats': {'requests': {'total': 2, 'pending': 0, 'failed': 1},\n"
        "            'assertions': {'total': 3, 'pending': 0, 'failed': 1},\n"
        "            'iterations': {'total': 1, 'pending': 0, 'failed': 0}},\n"
        "  'timings': {'started': 0, 'completed': 120, 'responseAverage': 42},\n"
        "  'executions': [{'item': {'name': 'Listar usuarios'},\n"
        "     'request': {'method': 'GET', 'url': 'http://local/users'},\n"
        "     'response': {'code': 200, 'status': 'OK', 'responseTime': 42, 'responseSize': 100},\n"
        "     'assertions': [{'assertion': 'estado 200'}]}],\n"
        "  'failures': [{'source': {'name': 'Debe fallar'},\n"
        "     'error': {'test': 'espera 200', 'message': 'expected 500 to equal 200'}}]}}\n"
        "json.dump(report, open(out, 'w'))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binroot}:{old_path}"
    try:
        check("newman simulado detectado en PATH", pm.find_newman() is not None)
        r = await call("run_postman_collection", collectionPath="coleccion.json",
                       runner="newman")
        check("runner=newman utilizado", r["runner"] == "newman")
        check("consolidado de newman parseado",
              r["assertions"] == {"total": 3, "pending": 0, "failed": 1}, json.dumps(r["assertions"]))
        check("ejecuciones de newman mapeadas",
              r["executions"][0]["name"] == "Listar usuarios"
              and r["executions"][0]["statusCode"] == 200)
        check("failures de newman mapeadas",
              r["failures"][0]["message"] == "expected 500 to equal 200")
        check("veredicto newman correcto", r["passed"] is False)
    finally:
        os.environ["PATH"] = old_path


# --------------------------------------------------------------------------- #
# [9] generate_test_report
# --------------------------------------------------------------------------- #
async def test_report() -> None:
    print("\n\033[1m[9] generate_test_report\033[0m")
    await call("get_api_session", reset=True)

    await call("set_api_auth", type="bearer", token="token-informe")
    await call("build_and_send_request", method="GET", url=f"{BASE}/users",
               name="Consultar el listado de usuarios")
    await call("validate_api_response", expectedStatus=200, maxResponseTimeMs=5000,
               requiredFields=["data"],
               valueAssertions=[{"jsonPath": "$.total", "operator": "equals", "expected": 3}])
    await call("extract_response_data", jsonPath="$.data[0].id", variableName="idUsuario")
    await call("build_and_send_request", method="GET", url=f"{BASE}/users/{{{{idUsuario}}}}",
               name="Consultar el detalle del usuario")
    await call("validate_json_schema", schema={"type": "object", "required": ["data"]})
    await call("build_and_send_request", method="GET", url=f"{BASE}/status",
               queryParams={"code": 503}, name="Escenario negativo")
    await call("validate_api_response", expectedStatus=200)

    r = await call("generate_test_report", suiteName="Regresión de Clientes",
                   environment="qa", includeResponseBody=True,
                   outputPath="informes/regresion.md")
    md = r["markdown"]
    check("veredicto RECHAZADO con fallos", r["verdict"] == "RECHAZADO" and r["passed"] is False)
    check("título con el nombre de la suite", "# ❌ Informe de Pruebas de API — Regresión de Clientes" in md)
    check("tabla de resumen ejecutivo", "| Peticiones ejecutadas | 3 |" in md, md[:600])
    check("entorno reflejado", "| Entorno | qa |" in md)
    check("sección de rendimiento con percentiles", "| p95 |" in md)
    check("formato BDD Given", "- **Dado** que" in md)
    check("formato BDD When", "- **Cuando** se ejecuta" in md)
    check("formato BDD Then", "- **Entonces** el servicio responde" in md)
    check("aserciones anidadas con Y", "**Y**" in md)
    check("nombres de escenario personalizados",
          "Consultar el listado de usuarios" in md and "Escenario negativo" in md)
    check("cuerpo de respuesta incluido", "<details><summary>Cuerpo de la respuesta</summary>" in md)
    check("sección de fallos poblada", "## Detalle de fallos" in md and "| 1 |" in md)
    check("variables de sesión listadas", "## Variables de sesión" in md and "`idUsuario`" in md)
    from server.config import settings as cfg
    check("pie con versión del servidor", f"v{cfg.server_version}" in md and "Informe generado por" in md,
          md[-300:])

    saved = Path(WORKDIR) / "informes" / "regresion.md"
    check("informe escrito en disco", saved.exists() and saved.read_text(encoding="utf-8") == md)
    check("ruta devuelta", r["savedTo"] is not None and "regresion.md" in r["savedTo"])
    check("escenarios contabilizados", r["scenarios"] == 3)
    check("fallos contabilizados", r["failedAssertions"] >= 1)

    # informe todo verde
    await call("get_api_session", reset=True)
    await call("build_and_send_request", method="GET", url=f"{BASE}/users")
    await call("validate_api_response", expectedStatus=200)
    r = await call("generate_test_report", suiteName="Humo")
    check("veredicto APROBADO sin fallos", r["verdict"] == "APROBADO" and r["passed"] is True)
    check("mensaje de sin fallos", "_Sin fallos: todas las aserciones se superaron._" in r["markdown"])
    check("sin outputPath no escribe", r["savedTo"] is None)

    r = await call("generate_test_report", suiteName="Con reset", reset=True)
    check("reset vacía el historial", session.history == [] and r["sessionReset"] is True)

    await expect_error("sin historial -> ToolError", "generate_test_report", suiteName="Vacío")
    await call("build_and_send_request", method="GET", url=f"{BASE}/users")
    await expect_error("suiteName vacío -> ToolError", "generate_test_report", suiteName="   ")


# --------------------------------------------------------------------------- #
# [10] get_api_session y registro
# --------------------------------------------------------------------------- #
async def test_session_and_registry() -> None:
    print("\n\033[1m[10] Sesión, registro y salvaguardas\033[0m")
    expected = [
        "set_api_auth", "set_session_variable", "build_and_send_request",
        "validate_api_response", "validate_json_schema", "extract_response_data",
        "run_postman_collection", "generate_test_report", "get_api_session",
    ]
    listed = {t["name"] for t in registry.list_tools()}
    for name in expected:
        check(f"tool '{name}' registrada", name in listed)

    for tool in registry.list_tools():
        if tool["name"] in expected:
            schema = tool["inputSchema"]
            check(f"'{tool['name']}' tiene inputSchema objeto",
                  schema.get("type") == "object" and "properties" in schema)
            check(f"'{tool['name']}' tiene descripción útil",
                  len(tool.get("description", "")) > 40)

    await call("get_api_session", reset=True)
    await call("set_session_variable", key="authToken", value="valor-secreto-largo")
    await call("set_api_auth", type="bearer", token="bearer-secreto")
    await call("build_and_send_request", method="GET", url=f"{BASE}/users")

    r = await call("get_api_session")
    check("variables sensibles enmascaradas", r["variables"]["authToken"].startswith("***"))
    check("auth enmascarada", r["auth"]["token"].startswith("***"))
    check("historial resumido", len(r["history"]) == 1 and r["history"][0]["statusCode"] == 200)
    check("última respuesta expuesta", r["lastResponse"]["method"] == "GET")
    check("estado de los runners informado", "newman" in r["runners"] and "node" in r["runners"])
    check("configuración efectiva expuesta", r["config"]["timeoutSeconds"] > 0)

    r = await call("get_api_session", reset=True, keepVariables=True, keepAuth=True)
    check("reset conservando variables/auth",
          session.variables.get("authToken") == "valor-secreto-largo"
          and session.auth.get("type") == "bearer" and session.history == [])

    r = await call("get_api_session", reset=True)
    check("reset completo", session.variables == {} and session.auth == {})

    # cabeceras sensibles redactadas en el historial
    await call("build_and_send_request", method="GET", url=f"{BASE}/whoami",
               headers={"Authorization": "Bearer no-debe-verse"})
    rec = session.history[-1].to_dict()
    check("Authorization redactada en el historial",
          rec["requestHeaders"]["Authorization"].startswith("***"),
          str(rec["requestHeaders"]))

    # interruptor de desactivación
    set_setting("enable_api_testing", False)
    await expect_error("grupo deshabilitado -> ToolError", "build_and_send_request",
                       method="GET", url=f"{BASE}/users")
    set_setting("enable_api_testing", True)
    r = await call("get_api_session")
    check("reactivación del grupo", isinstance(r, dict))


# --------------------------------------------------------------------------- #
async def main() -> int:
    global BASE
    srv, BASE = start_server()
    print("\033[1m╔══════════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1m║  Pruebas de API Testing & QA (servidor HTTP real en loopback) ║\033[0m")
    print("\033[1m╚══════════════════════════════════════════════════════════════╝\033[0m")
    print(f"  \033[90m· API bajo prueba: {BASE}\033[0m")
    print(f"  \033[90m· Workspace: {WORKDIR}\033[0m")
    try:
        for test in (
            test_engines, test_auth, test_variables, test_requests, test_validations,
            test_schema_tool, test_extraction, test_collections, test_report,
            test_session_and_registry,
        ):
            await test()
    finally:
        srv.shutdown()

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

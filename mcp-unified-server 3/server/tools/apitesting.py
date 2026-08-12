"""
Grupo de herramientas 3: API Testing & QA (estilo Postman / Serenity REST).

Ocho tools que cubren el ciclo completo de una prueba de API:

    set_api_auth            -> credenciales globales del escenario
    set_session_variable    -> variables de entorno (pm.environment.set)
    build_and_send_request  -> ejecución HTTP con control total del contrato
    validate_api_response   -> aserciones de estado, SLA, campos y valores
    validate_json_schema    -> validación estructural contra JSON Schema
    extract_response_data   -> JSONPath / regex -> variable de sesión
    run_postman_collection  -> runner de colecciones (newman o nativo)
    generate_test_report    -> informe ejecutivo BDD en Markdown

Todas comparten la misma `ApiSession`, de modo que se encadenan sin repetir
contexto: lo que extrae una tool queda disponible como {{variable}} para las
siguientes.
"""
from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..api import postman as pm
from ..api.jsonpath import JsonPathError, jsonpath, pretty
from ..api.schema import SchemaError, validate_schema
from ..api.state import (
    Assertion,
    RequestRecord,
    mask,
    parse_body_text,
    redact_headers,
    session,
    truncate_body,
)
from ..config import settings
from ..core.registry import ToolError, registry
from ..core.security import display_path, resolve_path

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
BODY_TYPES = ("json", "form-data", "x-www-form-urlencoded", "raw")
AUTH_TYPES = ("bearer", "basic", "apiKey", "oauth2", "none")
OPERATORS = (
    "equals",
    "notEquals",
    "contains",
    "notContains",
    "notNull",
    "isNull",
    "greaterThan",
    "lessThan",
    "greaterOrEqual",
    "lessOrEqual",
    "matches",
    "in",
    "type",
    "length",
    "empty",
    "notEmpty",
)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _require_enabled() -> None:
    if not settings.enable_api_testing:
        raise ToolError(
            "El grupo de API Testing está deshabilitado. "
            "Arranca el servidor con MCP_ENABLE_API_TESTING=true."
        )


def _httpx():
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ToolError(
            "Falta la dependencia 'httpx', necesaria para las tools de API Testing.\n"
            "Instálala con:  pip install -r requirements.txt\n"
            "(o directamente:  pip install 'httpx>=0.27')"
        ) from exc
    return httpx


_TLS_WARNING: List[str] = []


def _new_client(httpx: Any, **kwargs: Any):
    """
    Crea un AsyncClient tolerando bundles de CA rotos.

    httpx construye el contexto SSL de forma anticipada, incluso para URLs
    http://. En entornos donde SSL_CERT_FILE/REQUESTS_CA_BUNDLE apuntan a un
    fichero inexistente o sin permisos, eso aborta la petición aunque no haya
    TLS de por medio. Se reintenta sin verificación dejando constancia.
    """
    try:
        return httpx.AsyncClient(**kwargs)
    except (OSError, ValueError) as exc:
        if kwargs.get("verify") is False:
            raise
        _TLS_WARNING.append(
            f"No se pudo cargar el almacén de certificados del sistema ({exc}); "
            "se continúa sin verificación TLS. Revisa SSL_CERT_FILE / "
            "REQUESTS_CA_BUNDLE si necesitas validar certificados."
        )
        return httpx.AsyncClient(**{**kwargs, "verify": False})


def _absolute_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        raise ToolError("El parámetro 'url' no puede estar vacío.")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        return url
    base = settings.api_base_url.strip()
    if base:
        return base.rstrip("/") + "/" + url.lstrip("/")
    if url.startswith("//"):
        return "https:" + url
    if re.match(r"^(localhost|127\.0\.0\.1|\[?::1\]?)(:\d+)?(/|$)", url):
        return "http://" + url
    return "https://" + url


def _assert_host_allowed(url: str) -> None:
    patterns = [p.strip() for p in settings.api_host_allowlist.split(",") if p.strip()]
    if not patterns:
        return
    host = urlparse(url).hostname or ""
    for pattern in patterns:
        try:
            if re.search(pattern, host):
                return
        except re.error:
            if pattern.lower() == host.lower():
                return
    raise ToolError(
        f"Host bloqueado: '{host}' no está en MCP_API_HOST_ALLOWLIST "
        f"({settings.api_host_allowlist})."
    )


def _as_dict(value: Any, label: str) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolError(f"'{label}' no es un objeto JSON válido: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"'{label}' debe ser un objeto (clave/valor).")
    return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _trim_history() -> None:
    limit = max(1, settings.api_max_history)
    if len(session.history) > limit:
        del session.history[: len(session.history) - limit]


def _record_assertion(
    name: str,
    passed: bool,
    *,
    category: str,
    expected: Any = None,
    actual: Any = None,
    detail: str = "",
) -> Dict[str, Any]:
    a = Assertion(
        name=name,
        passed=passed,
        category=category,
        expected=expected,
        actual=actual,
        detail=detail,
    )
    session.add_assertion(a)
    return a.to_dict()


# --------------------------------------------------------------------------- #
# 1. set_api_auth
# --------------------------------------------------------------------------- #
@registry.tool(
    name="set_api_auth",
    title="Configurar autenticación de API",
    description=(
        "Configura las credenciales de autenticación globales de la sesión o "
        "escenario. Se aplican automáticamente a todas las peticiones "
        "posteriores de 'build_and_send_request'. Soporta bearer, basic, apiKey "
        "y oauth2 (con obtención automática del token vía client_credentials si "
        "se indica 'tokenUrl'). Los valores admiten {{variables}} de sesión. "
        "Usa type='none' para limpiar la autenticación."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": list(AUTH_TYPES),
                "description": "Esquema de autenticación a aplicar.",
            },
            "token": {
                "type": "string",
                "description": "Token para 'bearer', 'oauth2' o valor de la clave en 'apiKey'.",
            },
            "username": {
                "type": "string",
                "description": "Usuario para 'basic' o client_id para 'oauth2'.",
            },
            "password": {
                "type": "string",
                "description": "Contraseña para 'basic' o client_secret para 'oauth2'.",
            },
            "headerName": {
                "type": "string",
                "description": "Nombre de la cabecera para 'apiKey' (por defecto X-API-Key).",
            },
            "tokenUrl": {
                "type": "string",
                "description": (
                    "Endpoint de token para 'oauth2'. Si se indica, se solicita el "
                    "access_token con client_credentials usando username/password "
                    "como client_id/client_secret."
                ),
            },
            "scope": {
                "type": "string",
                "description": "Scope OAuth2 opcional para la petición de token.",
            },
            "prefix": {
                "type": "string",
                "description": "Prefijo del header Authorization (por defecto 'Bearer').",
            },
        },
        "required": ["type"],
        "additionalProperties": False,
    },
)
async def set_api_auth(
    type: str,
    token: str = "",
    username: str = "",
    password: str = "",
    headerName: str = "",
    tokenUrl: str = "",
    scope: str = "",
    prefix: str = "",
) -> Dict[str, Any]:
    _require_enabled()
    kind = str(type or "").strip()
    if kind not in AUTH_TYPES:
        raise ToolError(
            f"Tipo de autenticación no soportado: '{type}'. "
            f"Usa uno de: {', '.join(AUTH_TYPES)}."
        )

    if kind == "none":
        session.auth = {}
        return {"status": "cleared", "auth": session.auth_public()}

    if kind == "bearer" and not token:
        raise ToolError("La autenticación 'bearer' requiere el parámetro 'token'.")
    if kind == "basic" and not username:
        raise ToolError("La autenticación 'basic' requiere 'username' (y normalmente 'password').")
    if kind == "apiKey" and not token:
        raise ToolError(
            "La autenticación 'apiKey' requiere 'token' (el valor de la clave) y "
            "opcionalmente 'headerName'."
        )

    auth: Dict[str, Any] = {"type": kind}
    if token:
        auth["token"] = token
    if username:
        auth["username"] = username
    if password:
        auth["password"] = password
    if headerName:
        auth["headerName"] = headerName
    if prefix:
        auth["prefix"] = prefix
    if scope:
        auth["scope"] = scope
    if tokenUrl:
        auth["tokenUrl"] = tokenUrl

    fetched: Optional[Dict[str, Any]] = None

    # OAuth2 client_credentials: se obtiene el token en el momento.
    if kind == "oauth2" and tokenUrl and not token:
        if not username or not password:
            raise ToolError(
                "Para obtener el token OAuth2 con 'tokenUrl' hacen falta "
                "'username' (client_id) y 'password' (client_secret)."
            )
        httpx = _httpx()
        url = _absolute_url(session.interpolate(tokenUrl))
        _assert_host_allowed(url)
        form = {
            "grant_type": "client_credentials",
            "client_id": session.interpolate(username),
            "client_secret": session.interpolate(password),
        }
        if scope:
            form["scope"] = session.interpolate(scope)
        try:
            async with _new_client(
                httpx,
                timeout=settings.api_timeout_seconds,
                verify=settings.api_verify_tls,
            ) as client:
                resp = await client.post(url, data=form)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"No se pudo obtener el token OAuth2 de '{url}': "
                f"{type_name(exc)}: {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise ToolError(
                f"El endpoint de token devolvió {resp.status_code}. "
                f"Respuesta: {resp.text[:500]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ToolError(
                f"El endpoint de token no devolvió JSON: {resp.text[:300]}"
            ) from exc
        access = payload.get("access_token") or payload.get("accessToken")
        if not access:
            raise ToolError(
                "La respuesta del token no contiene 'access_token'. "
                f"Claves recibidas: {list(payload)[:10]}"
            )
        auth["token"] = access
        if payload.get("token_type"):
            auth["prefix"] = str(payload["token_type"]).capitalize()
        fetched = {
            "tokenUrl": url,
            "statusCode": resp.status_code,
            "expiresIn": payload.get("expires_in"),
            "tokenType": payload.get("token_type"),
            "accessToken": mask(access),
        }

    session.auth = auth
    headers = session.auth_headers()

    return {
        "status": "configured",
        "auth": session.auth_public(),
        "appliedHeaders": redact_headers(headers),
        "oauth2": fetched,
        "note": (
            "Estas credenciales se añaden automáticamente a cada "
            "'build_and_send_request'. Las cabeceras explícitas de una petición "
            "tienen prioridad sobre las de autenticación."
        ),
    }


def type_name(exc: Exception) -> str:
    return exc.__class__.__name__


# --------------------------------------------------------------------------- #
# 2. set_session_variable
# --------------------------------------------------------------------------- #
@registry.tool(
    name="set_session_variable",
    title="Definir variable de sesión",
    description=(
        "Define o actualiza una variable de entorno/sesión (equivalente a "
        "pm.environment.set en Postman). Las variables se interpolan con la "
        "sintaxis {{clave}} en url, headers, queryParams y body de las "
        "peticiones posteriores. El valor admite cualquier tipo JSON. "
        "Usa 'unset': true para eliminarla."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Nombre de la variable."},
            "value": {
                "description": "Valor (string, número, booleano, objeto o array).",
            },
            "unset": {
                "type": "boolean",
                "description": "Si es true elimina la variable en lugar de asignarla.",
            },
            "secret": {
                "type": "boolean",
                "description": "Si es true el valor se enmascara al mostrarlo.",
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    },
)
def set_session_variable(
    key: str,
    value: Any = None,
    unset: bool = False,
    secret: bool = False,
) -> Dict[str, Any]:
    _require_enabled()
    name = str(key or "").strip()
    if not name:
        raise ToolError("El parámetro 'key' no puede estar vacío.")

    if unset:
        existed = name in session.variables
        session.variables.pop(name, None)
        return {
            "status": "unset" if existed else "not_found",
            "key": name,
            "variables": sorted(session.variables),
        }

    previous = session.variables.get(name, None)
    had_previous = name in session.variables
    session.set_var(name, value)

    shown = mask(value) if secret else value
    return {
        "status": "updated" if had_previous else "created",
        "key": name,
        "value": shown,
        "previousValue": (mask(previous) if secret else previous) if had_previous else None,
        "type": type(value).__name__,
        "totalVariables": len(session.variables),
        "variables": sorted(session.variables),
        "usage": f"Úsala como {{{{{name}}}}} en url, headers, queryParams o body.",
    }


# --------------------------------------------------------------------------- #
# 3. build_and_send_request
# --------------------------------------------------------------------------- #
@registry.tool(
    name="build_and_send_request",
    title="Construir y enviar petición HTTP",
    description=(
        "Ejecuta una petición HTTP completa controlando método, URL, cabeceras, "
        "query params, tipo de cuerpo y adjuntos. Aplica automáticamente la "
        "autenticación configurada con 'set_api_auth' e interpola las "
        "{{variables}} de sesión. La respuesta queda guardada como 'última "
        "respuesta' para 'validate_api_response', 'validate_json_schema' y "
        "'extract_response_data'. Devuelve statusCode, responseTimeMs, "
        "responseHeaders y responseBody."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": list(METHODS),
                "description": "Método HTTP.",
            },
            "url": {
                "type": "string",
                "description": (
                    "URL absoluta o relativa a MCP_API_BASE_URL. Admite {{variables}}."
                ),
            },
            "headers": {
                "type": "object",
                "description": "Cabeceras adicionales (clave/valor).",
            },
            "queryParams": {
                "type": "object",
                "description": "Parámetros de query string (clave/valor).",
            },
            "bodyType": {
                "type": "string",
                "enum": list(BODY_TYPES),
                "description": (
                    "json | form-data | x-www-form-urlencoded | raw. "
                    "Si se omite y hay body, se asume 'json'."
                ),
            },
            "body": {
                "description": (
                    "Cuerpo de la petición: objeto/array para 'json', objeto de "
                    "campos para los formularios, o string para 'raw'."
                )
            },
            "files": {
                "type": "array",
                "description": (
                    "Adjuntos multipart. Cada elemento: "
                    "{ fieldName, filePath, fileName?, contentType? }. "
                    "Fuerza bodyType='form-data'."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "fieldName": {"type": "string"},
                        "filePath": {"type": "string"},
                        "fileName": {"type": "string"},
                        "contentType": {"type": "string"},
                    },
                    "required": ["fieldName", "filePath"],
                },
            },
            "name": {
                "type": "string",
                "description": "Nombre descriptivo del paso (aparece en el informe BDD).",
            },
            "timeoutSeconds": {
                "type": "integer",
                "description": "Timeout de esta petición (por defecto MCP_API_TIMEOUT).",
            },
            "followRedirects": {
                "type": "boolean",
                "description": "Seguir redirecciones 3xx (por defecto según config).",
            },
            "verifyTls": {
                "type": "boolean",
                "description": "Verificar certificado TLS (por defecto según config).",
            },
        },
        "required": ["method", "url"],
        "additionalProperties": False,
    },
)
async def build_and_send_request(
    method: str,
    url: str,
    headers: Any = None,
    queryParams: Any = None,
    bodyType: str = "",
    body: Any = None,
    files: Any = None,
    name: str = "",
    timeoutSeconds: int = 0,
    followRedirects: Optional[bool] = None,
    verifyTls: Optional[bool] = None,
) -> Dict[str, Any]:
    _require_enabled()
    httpx = _httpx()

    verb = str(method or "").strip().upper()
    if verb not in METHODS:
        raise ToolError(
            f"Método HTTP no soportado: '{method}'. Usa uno de: {', '.join(METHODS)}."
        )

    raw_headers = _as_dict(headers, "headers")
    raw_query = _as_dict(queryParams, "queryParams")

    # --- interpolación de variables ------------------------------------- #
    final_url = _absolute_url(str(session.interpolate(url)))
    _assert_host_allowed(final_url)
    hdrs = {str(k): _stringify(v) for k, v in session.interpolate(raw_headers).items()}
    query = {
        str(k): _stringify(v) for k, v in session.interpolate(raw_query).items()
    }
    body_value = session.interpolate(body)

    pending = session.unresolved(final_url, raw_headers, raw_query, body if body is not None else "")

    # --- autenticación --------------------------------------------------- #
    auth_headers = session.auth_headers()
    lower = {k.lower() for k in hdrs}
    for k, v in auth_headers.items():
        if k.lower() not in lower:  # las cabeceras explícitas mandan
            hdrs[k] = v

    # --- cuerpo ----------------------------------------------------------- #
    file_specs = _normalize_files(files)
    kind = str(bodyType or "").strip().lower()
    if file_specs:
        kind = "form-data"
    elif not kind and body_value is not None:
        kind = "json"
    if kind and kind not in BODY_TYPES:
        raise ToolError(
            f"bodyType no soportado: '{bodyType}'. Usa uno de: {', '.join(BODY_TYPES)}."
        )
    if verb in ("GET", "HEAD") and body_value is not None and not file_specs:
        # Se permite (algunas APIs lo usan) pero se avisa.
        pass

    kwargs: Dict[str, Any] = {}
    opened: List[Any] = []
    sent_body: Any = None

    try:
        if kind == "json" and body_value is not None:
            payload = body_value
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ToolError(
                        "bodyType='json' pero el body no es JSON válido "
                        f"(línea {exc.lineno}, col {exc.colno}): {exc.msg}. "
                        "Usa bodyType='raw' si quieres enviar texto sin parsear."
                    ) from exc
            kwargs["json"] = payload
            sent_body = payload
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"

        elif kind == "x-www-form-urlencoded":
            form = _as_dict(body_value, "body")
            data = {str(k): _stringify(v) for k, v in form.items()}
            kwargs["data"] = data
            sent_body = data

        elif kind == "form-data":
            form = _as_dict(body_value, "body") if body_value is not None else {}
            data = {str(k): _stringify(v) for k, v in form.items()}
            multipart: Dict[str, Any] = {}
            attached: List[Dict[str, Any]] = []
            for spec in file_specs:
                target = resolve_path(spec["filePath"], must_exist=True)
                if target.is_dir():
                    raise ToolError(
                        f"'{display_path(target)}' es un directorio; "
                        "'filePath' debe apuntar a un archivo."
                    )
                fh = target.open("rb")
                opened.append(fh)
                ctype = spec.get("contentType") or (
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                fname = spec.get("fileName") or target.name
                multipart[spec["fieldName"]] = (fname, fh, ctype)
                attached.append(
                    {
                        "fieldName": spec["fieldName"],
                        "fileName": fname,
                        "path": display_path(target),
                        "sizeBytes": target.stat().st_size,
                        "contentType": ctype,
                    }
                )
            if multipart:
                kwargs["files"] = multipart
            if data:
                kwargs["data"] = data
            sent_body = {"fields": data, "files": attached}
            # httpx pone el boundary; una cabecera manual lo rompería.
            hdrs = {k: v for k, v in hdrs.items() if k.lower() != "content-type"}

        elif kind == "raw" and body_value is not None:
            text = body_value if isinstance(body_value, str) else _stringify(body_value)
            kwargs["content"] = text.encode("utf-8")
            sent_body = text
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "text/plain; charset=utf-8"

        timeout = timeoutSeconds if timeoutSeconds and timeoutSeconds > 0 else settings.api_timeout_seconds
        follow = settings.api_follow_redirects if followRedirects is None else bool(followRedirects)
        verify = settings.api_verify_tls if verifyTls is None else bool(verifyTls)

        started = time.perf_counter()
        error: Optional[str] = None
        try:
            async with _new_client(
                httpx,
                timeout=timeout,
                verify=verify,
                follow_redirects=follow,
                max_redirects=settings.api_max_redirects,
            ) as client:
                resp = await client.request(
                    verb,
                    final_url,
                    headers=hdrs or None,
                    params=query or None,
                    **kwargs,
                )
            elapsed = (time.perf_counter() - started) * 1000
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - started) * 1000
            record = RequestRecord(
                index=session.next_index(),
                method=verb,
                url=final_url,
                request_headers=hdrs,
                query_params=query,
                body_type=kind or None,
                request_body=truncate_body(sent_body, settings.api_max_body_chars),
                files=[{"fieldName": f["fieldName"], "filePath": f["filePath"]} for f in file_specs],
                status_code=0,
                status_text="",
                response_time_ms=round(elapsed, 2),
                response_headers={},
                response_body=None,
                response_text="",
                response_size_bytes=0,
                json_parsed=False,
                error=f"{type_name(exc)}: {exc}",
                name=name,
            )
            session.record(record)
            _trim_history()
            raise ToolError(
                f"Fallo de red al ejecutar {verb} {final_url}: "
                f"{type_name(exc)}: {exc}\n"
                f"Comprueba conectividad, DNS, proxy y el valor de MCP_API_VERIFY_TLS."
            ) from exc
    finally:
        for fh in opened:
            try:
                fh.close()
            except OSError:  # pragma: no cover
                pass

    text = resp.text
    content_type = resp.headers.get("content-type", "")
    parsed, is_json = parse_body_text(text, content_type)

    record = RequestRecord(
        index=session.next_index(),
        method=verb,
        url=str(resp.request.url),
        request_headers=hdrs,
        query_params=query,
        body_type=kind or None,
        request_body=truncate_body(sent_body, settings.api_max_body_chars),
        files=[{"fieldName": f["fieldName"], "filePath": f["filePath"]} for f in file_specs],
        status_code=resp.status_code,
        status_text=resp.reason_phrase or "",
        response_time_ms=round(elapsed, 2),
        response_headers=dict(resp.headers),
        response_body=parsed,
        response_text=text,
        response_size_bytes=len(resp.content),
        json_parsed=is_json,
        name=name,
    )
    session.record(record)
    _trim_history()

    result: Dict[str, Any] = {
        "requestIndex": record.index,
        "method": verb,
        "url": record.url,
        "statusCode": resp.status_code,
        "statusText": record.status_text,
        "ok": 200 <= resp.status_code < 300,
        "responseTimeMs": record.response_time_ms,
        "responseHeaders": dict(resp.headers),
        "responseBody": truncate_body(parsed, settings.api_max_body_chars),
        "responseSizeBytes": record.response_size_bytes,
        "jsonParsed": is_json,
        "requestHeadersSent": redact_headers(hdrs),
        "redirected": len(resp.history) > 0,
    }
    if pending:
        result["unresolvedVariables"] = pending
        result["warning"] = (
            "Hay {{variables}} sin definir: " + ", ".join(pending) +
            ". Defínelas con 'set_session_variable' o 'extract_response_data'."
        )
    return result


def _normalize_files(files: Any) -> List[Dict[str, str]]:
    if files in (None, "", []):
        return []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError as exc:
            raise ToolError(f"'files' no es JSON válido: {exc.msg}") from exc
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list):
        raise ToolError(
            "'files' debe ser un array de objetos { fieldName, filePath }."
        )
    out: List[Dict[str, str]] = []
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ToolError(f"files[{i}] debe ser un objeto con fieldName y filePath.")
        field = entry.get("fieldName") or entry.get("field") or entry.get("name")
        path = entry.get("filePath") or entry.get("path") or entry.get("src")
        if not field or not path:
            raise ToolError(
                f"files[{i}] necesita 'fieldName' y 'filePath'. Recibido: {list(entry)}"
            )
        item = {"fieldName": str(field), "filePath": str(session.interpolate(str(path)))}
        if entry.get("fileName"):
            item["fileName"] = str(entry["fileName"])
        if entry.get("contentType"):
            item["contentType"] = str(entry["contentType"])
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 4. validate_api_response
# --------------------------------------------------------------------------- #
@registry.tool(
    name="validate_api_response",
    title="Validar respuesta de API",
    description=(
        "Motor de aserciones estilo Serenity REST sobre la última respuesta: "
        "código de estado, SLA de tiempo de respuesta, presencia de campos "
        "obligatorios y aserciones de valor por JSONPath. Devuelve el detalle "
        "de cada aserción (passed/failed) y el veredicto global. Operadores: "
        "equals, notEquals, contains, notContains, notNull, isNull, greaterThan, "
        "lessThan, greaterOrEqual, lessOrEqual, matches, in, type, length, "
        "empty, notEmpty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expectedStatus": {
                "type": "integer",
                "description": "Código HTTP esperado (p. ej. 200, 201, 404).",
            },
            "maxResponseTimeMs": {
                "type": "integer",
                "description": "SLA: tiempo máximo de respuesta admisible en ms.",
            },
            "requiredFields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Campos que deben existir y no ser null. Acepta JSONPath "
                    "($.data.id) o ruta con puntos (data.id)."
                ),
            },
            "valueAssertions": {
                "type": "array",
                "description": (
                    "Aserciones de valor: "
                    "{ jsonPath, operator, expected, description? }."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "jsonPath": {"type": "string"},
                        "operator": {"type": "string", "enum": list(OPERATORS)},
                        "expected": {},
                        "description": {"type": "string"},
                    },
                    "required": ["jsonPath", "operator"],
                },
            },
            "expectedHeaders": {
                "type": "object",
                "description": (
                    "Cabeceras esperadas en la respuesta. Valor '*' = basta con "
                    "que exista."
                ),
            },
            "bodyContains": {
                "type": "string",
                "description": "Subcadena que debe aparecer en el cuerpo crudo.",
            },
            "requestIndex": {
                "type": "integer",
                "description": (
                    "Validar una petición concreta del historial en lugar de la "
                    "última (1 = la primera)."
                ),
            },
            "failFast": {
                "type": "boolean",
                "description": "Si es true, lanza error en la primera aserción fallida.",
            },
        },
        "additionalProperties": False,
    },
)
def validate_api_response(
    expectedStatus: Optional[int] = None,
    maxResponseTimeMs: Optional[int] = None,
    requiredFields: Any = None,
    valueAssertions: Any = None,
    expectedHeaders: Any = None,
    bodyContains: str = "",
    requestIndex: Optional[int] = None,
    failFast: bool = False,
) -> Dict[str, Any]:
    _require_enabled()
    record = _pick_record(requestIndex)
    results: List[Dict[str, Any]] = []

    def add(name: str, passed: bool, category: str, expected: Any, actual: Any, detail: str = "") -> None:
        a = Assertion(
            name=name,
            passed=passed,
            category=category,
            expected=expected,
            actual=actual,
            detail=detail,
            request_index=record.index,
        )
        session.add_assertion(a)
        results.append(a.to_dict())

    # --- estado ----------------------------------------------------------- #
    if expectedStatus is not None:
        if not isinstance(expectedStatus, int) or isinstance(expectedStatus, bool):
            raise ToolError("'expectedStatus' debe ser un número entero.")
        ok = record.status_code == expectedStatus
        add(
            f"El código de estado es {expectedStatus}",
            ok,
            "status",
            expectedStatus,
            record.status_code,
            "" if ok else f"Se recibió {record.status_code} {record.status_text}".strip(),
        )

    # --- SLA -------------------------------------------------------------- #
    if maxResponseTimeMs is not None:
        if not isinstance(maxResponseTimeMs, int) or isinstance(maxResponseTimeMs, bool):
            raise ToolError("'maxResponseTimeMs' debe ser un número entero de milisegundos.")
        ok = record.response_time_ms <= maxResponseTimeMs
        add(
            f"El tiempo de respuesta es <= {maxResponseTimeMs} ms (SLA)",
            ok,
            "sla",
            maxResponseTimeMs,
            record.response_time_ms,
            ""
            if ok
            else f"Excedido en {round(record.response_time_ms - maxResponseTimeMs, 2)} ms",
        )

    # --- campos obligatorios ---------------------------------------------- #
    fields = _as_list(requiredFields, "requiredFields")
    for raw in fields:
        path = str(raw)
        try:
            hits = jsonpath(record.response_body, _norm_path(path))
        except JsonPathError as exc:
            raise ToolError(f"requiredFields: expresión inválida '{path}': {exc}") from exc
        present = bool(hits) and not all(h is None for h in hits)
        add(
            f"El campo '{path}' está presente y no es null",
            present,
            "field",
            "presente",
            pretty(hits[0]) if hits else "ausente",
            "" if present else f"No se encontró '{path}' en el cuerpo de la respuesta.",
        )

    # --- aserciones de valor ---------------------------------------------- #
    for i, spec in enumerate(_as_list(valueAssertions, "valueAssertions")):
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError as exc:
                raise ToolError(f"valueAssertions[{i}] no es JSON válido: {exc.msg}") from exc
        if not isinstance(spec, dict):
            raise ToolError(
                f"valueAssertions[{i}] debe ser un objeto "
                "{ jsonPath, operator, expected }."
            )
        path = spec.get("jsonPath") or spec.get("path")
        operator = str(spec.get("operator") or "equals")
        if not path:
            raise ToolError(f"valueAssertions[{i}] necesita 'jsonPath'.")
        if operator not in OPERATORS:
            raise ToolError(
                f"valueAssertions[{i}]: operador '{operator}' no soportado. "
                f"Usa uno de: {', '.join(OPERATORS)}."
            )
        expected = session.interpolate(spec.get("expected"))
        try:
            hits = jsonpath(record.response_body, _norm_path(str(path)))
        except JsonPathError as exc:
            raise ToolError(
                f"valueAssertions[{i}]: JSONPath inválido '{path}': {exc}"
            ) from exc

        actual = hits[0] if len(hits) == 1 else (hits if hits else None)
        found = bool(hits)
        passed, detail = _apply_operator(operator, actual, expected, found)
        label = spec.get("description") or _describe(path, operator, expected)
        add(label, passed, "value", expected, truncate_body(actual, 500), detail)

    # --- cabeceras --------------------------------------------------------- #
    for key, expected in _as_dict(expectedHeaders, "expectedHeaders").items():
        actual = None
        for hk, hv in record.response_headers.items():
            if hk.lower() == str(key).lower():
                actual = hv
                break
        if expected in ("*", "", None):
            ok = actual is not None
            detail = "" if ok else f"La respuesta no incluye la cabecera '{key}'."
        else:
            ok = actual is not None and str(expected).lower() in str(actual).lower()
            detail = "" if ok else f"Cabecera '{key}' = {actual!r}"
        add(f"La cabecera '{key}' cumple lo esperado", ok, "header", expected, actual, detail)

    # --- cuerpo contiene ---------------------------------------------------- #
    if bodyContains:
        needle = str(session.interpolate(bodyContains))
        ok = needle in record.response_text
        add(
            f"El cuerpo contiene '{needle}'",
            ok,
            "value",
            needle,
            None,
            "" if ok else "La subcadena no aparece en el cuerpo de la respuesta.",
        )

    failed = [r for r in results if not r["passed"]]
    if not results:
        raise ToolError(
            "No se indicó ninguna validación. Usa al menos uno de: expectedStatus, "
            "maxResponseTimeMs, requiredFields, valueAssertions, expectedHeaders, "
            "bodyContains."
        )

    summary = {
        "passed": len(failed) == 0,
        "requestIndex": record.index,
        "request": f"{record.method} {record.url}",
        "statusCode": record.status_code,
        "responseTimeMs": record.response_time_ms,
        "total": len(results),
        "passedCount": len(results) - len(failed),
        "failedCount": len(failed),
        "assertions": results,
        "failures": failed,
        "sessionStats": session.stats(),
    }

    if failed and failFast:
        lines = [f"  ✗ {f['name']} — esperado {pretty(f['expected'])}, "
                 f"real {pretty(f['actual'])}" + (f" ({f['detail']})" if f["detail"] else "")
                 for f in failed]
        raise ToolError(
            f"{len(failed)} de {len(results)} aserciones fallaron en "
            f"{record.method} {record.url}:\n" + "\n".join(lines)
        )
    return summary


def _pick_record(index: Optional[int]) -> RequestRecord:
    if index is None:
        return session.last
    for rec in session.history:
        if rec.index == index:
            return rec
    available = [r.index for r in session.history]
    raise ToolError(
        f"No hay ninguna petición con requestIndex={index}. "
        f"Índices disponibles: {available or 'ninguno'}."
    )


def _as_list(value: Any, label: str) -> List[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        raise ToolError(f"'{label}' debe ser un array.")
    return value


def _norm_path(path: str) -> str:
    p = path.strip()
    if not p:
        raise ToolError("La expresión JSONPath no puede estar vacía.")
    if p.startswith("$") or p.startswith("["):
        return p
    return "$." + p.lstrip(".")


def _describe(path: str, operator: str, expected: Any) -> str:
    human = {
        "equals": "es igual a",
        "notEquals": "es distinto de",
        "contains": "contiene",
        "notContains": "no contiene",
        "notNull": "no es null",
        "isNull": "es null",
        "greaterThan": "es mayor que",
        "lessThan": "es menor que",
        "greaterOrEqual": "es mayor o igual que",
        "lessOrEqual": "es menor o igual que",
        "matches": "casa con la expresión regular",
        "in": "está dentro de",
        "type": "es del tipo",
        "length": "tiene longitud",
        "empty": "está vacío",
        "notEmpty": "no está vacío",
    }.get(operator, operator)
    if operator in ("notNull", "isNull", "empty", "notEmpty"):
        return f"'{path}' {human}"
    return f"'{path}' {human} {pretty(expected, 80)}"


def _apply_operator(operator: str, actual: Any, expected: Any, found: bool) -> tuple[bool, str]:
    if operator == "notNull":
        ok = found and actual is not None
        return ok, "" if ok else ("El campo no existe." if not found else "El valor es null.")
    if operator == "isNull":
        ok = (not found) or actual is None
        return ok, "" if ok else f"El valor es {pretty(actual, 120)}."
    if not found:
        return False, "El JSONPath no encontró ninguna coincidencia en la respuesta."

    if operator == "equals":
        ok = _loose_eq(actual, expected)
        return ok, "" if ok else f"Se obtuvo {pretty(actual, 200)}."
    if operator == "notEquals":
        ok = not _loose_eq(actual, expected)
        return ok, "" if ok else "El valor coincide con el que debía ser distinto."
    if operator == "contains":
        ok = _contains(actual, expected)
        return ok, "" if ok else f"{pretty(actual, 200)} no contiene {pretty(expected, 80)}."
    if operator == "notContains":
        ok = not _contains(actual, expected)
        return ok, "" if ok else f"{pretty(actual, 200)} sí contiene {pretty(expected, 80)}."
    if operator in ("greaterThan", "lessThan", "greaterOrEqual", "lessOrEqual"):
        try:
            a, b = float(actual), float(expected)
        except (TypeError, ValueError):
            return False, (
                f"Comparación numérica imposible entre {pretty(actual, 80)} y "
                f"{pretty(expected, 80)}."
            )
        ok = {
            "greaterThan": a > b,
            "lessThan": a < b,
            "greaterOrEqual": a >= b,
            "lessOrEqual": a <= b,
        }[operator]
        return ok, "" if ok else f"Se obtuvo {a}."
    if operator == "matches":
        try:
            ok = bool(re.search(str(expected), str(actual)))
        except re.error as exc:
            raise ToolError(f"Expresión regular inválida '{expected}': {exc}") from exc
        return ok, "" if ok else f"{pretty(actual, 200)} no casa con /{expected}/."
    if operator == "in":
        options = expected if isinstance(expected, list) else [expected]
        ok = any(_loose_eq(actual, o) for o in options)
        return ok, "" if ok else f"{pretty(actual, 120)} no está en {pretty(options, 120)}."
    if operator == "type":
        ok = _type_name(actual) == str(expected).lower()
        return ok, "" if ok else f"El tipo real es '{_type_name(actual)}'."
    if operator == "length":
        try:
            length = len(actual)  # type: ignore[arg-type]
        except TypeError:
            return False, f"{pretty(actual, 120)} no tiene longitud."
        ok = length == int(expected)
        return ok, "" if ok else f"La longitud real es {length}."
    if operator == "empty":
        ok = actual in (None, "", [], {})
        return ok, "" if ok else f"Contiene {pretty(actual, 120)}."
    if operator == "notEmpty":
        ok = actual not in (None, "", [], {})
        return ok, "" if ok else "El valor está vacío."
    return False, f"Operador no implementado: {operator}"


def _loose_eq(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    # '200' == 200, 'true' == True — habitual al venir de query params o CSV.
    if isinstance(expected, bool) or isinstance(actual, bool):
        return False
    if isinstance(expected, (int, float)) and isinstance(actual, str):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    if isinstance(actual, (int, float)) and isinstance(expected, str):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return False


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected) in actual
    if isinstance(actual, list):
        return any(_loose_eq(x, expected) for x in actual)
    if isinstance(actual, dict):
        if isinstance(expected, dict):
            return all(_loose_eq(actual.get(k), v) for k, v in expected.items())
        return str(expected) in actual
    return False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# --------------------------------------------------------------------------- #
# 5. validate_json_schema
# --------------------------------------------------------------------------- #
@registry.tool(
    name="validate_json_schema",
    title="Validar contra JSON Schema",
    description=(
        "Valida el contrato estructural de la última respuesta HTTP frente a un "
        "JSON Schema (Draft-07). El esquema puede pasarse como objeto, como "
        "string JSON o mediante 'schemaPath' apuntando a un archivo .json del "
        "workspace. Devuelve la lista completa de incumplimientos con la ruta "
        "exacta de cada uno, no sólo el primero."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "schema": {
                "description": "JSON Schema como objeto o como string JSON.",
            },
            "schemaPath": {
                "type": "string",
                "description": "Alternativa a 'schema': ruta a un archivo .json con el esquema.",
            },
            "jsonPath": {
                "type": "string",
                "description": (
                    "Validar sólo una parte de la respuesta (p. ej. '$.data'). "
                    "Por defecto se valida el cuerpo completo."
                ),
            },
            "requestIndex": {
                "type": "integer",
                "description": "Validar una petición concreta del historial.",
            },
            "failFast": {
                "type": "boolean",
                "description": "Si es true, lanza error cuando el esquema no se cumple.",
            },
        },
        "additionalProperties": False,
    },
)
def validate_json_schema(
    schema: Any = None,
    schemaPath: str = "",
    jsonPath: str = "",
    requestIndex: Optional[int] = None,
    failFast: bool = False,
) -> Dict[str, Any]:
    _require_enabled()

    if schema in (None, "") and not schemaPath:
        raise ToolError(
            "Debes indicar 'schema' (objeto o string JSON) o 'schemaPath' "
            "(ruta a un archivo .json)."
        )

    source = "inline"
    if schema in (None, "") and schemaPath:
        target = resolve_path(schemaPath, must_exist=True)
        if target.is_dir():
            raise ToolError(f"'{display_path(target)}' es un directorio.")
        try:
            schema = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"El esquema '{display_path(target)}' no es JSON válido "
                f"(línea {exc.lineno}): {exc.msg}"
            ) from exc
        source = display_path(target)

    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"'schema' no es JSON válido (línea {exc.lineno}, col {exc.colno}): {exc.msg}"
            ) from exc

    record = _pick_record(requestIndex)
    if not record.json_parsed and not isinstance(record.response_body, (dict, list)):
        raise ToolError(
            "La última respuesta no es JSON, no se puede validar contra un esquema. "
            f"Content-Type: {record.response_headers.get('content-type', 'desconocido')}. "
            f"Cuerpo (inicio): {record.response_text[:200]!r}"
        )

    data = record.response_body
    scoped = "$"
    if jsonPath:
        scoped = _norm_path(jsonPath)
        try:
            hits = jsonpath(data, scoped)
        except JsonPathError as exc:
            raise ToolError(f"jsonPath inválido '{jsonPath}': {exc}") from exc
        if not hits:
            raise ToolError(
                f"El jsonPath '{jsonPath}' no encontró nada en la respuesta; "
                "no hay nada que validar."
            )
        data = hits[0] if len(hits) == 1 else hits

    try:
        errors = validate_schema(data, schema)
    except SchemaError as exc:
        raise ToolError(f"El JSON Schema es inválido: {exc}") from exc

    valid = not errors
    _record_assertion(
        f"La respuesta cumple el JSON Schema ({source})",
        valid,
        category="schema",
        expected="conforme al esquema",
        actual=f"{len(errors)} incumplimiento(s)" if errors else "conforme",
        detail="; ".join(f"{e['instancePath']}: {e['message']}" for e in errors[:5]),
    )

    result = {
        "valid": valid,
        "requestIndex": record.index,
        "request": f"{record.method} {record.url}",
        "schemaSource": source,
        "validatedPath": scoped,
        "errorCount": len(errors),
        "errors": errors[:100],
        "checkedKeywords": sorted(
            k for k in (schema if isinstance(schema, dict) else {})
            if not k.startswith("$")
        ),
    }
    if len(errors) > 100:
        result["note"] = f"Se muestran los primeros 100 de {len(errors)} errores."

    if errors and failFast:
        detail = "\n".join(
            f"  ✗ {e['instancePath']} [{e['keyword']}]: {e['message']}" for e in errors[:20]
        )
        raise ToolError(
            f"La respuesta NO cumple el contrato ({len(errors)} error(es)):\n{detail}"
        )
    return result


# --------------------------------------------------------------------------- #
# 6. extract_response_data
# --------------------------------------------------------------------------- #
@registry.tool(
    name="extract_response_data",
    title="Extraer datos de la respuesta",
    description=(
        "Extrae un valor de la respuesta previa mediante JSONPath o expresión "
        "regular y lo guarda como variable de sesión, lista para encadenar en la "
        "siguiente petición como {{variable}}. Sin 'variableName' sólo devuelve "
        "el valor sin guardarlo. Puede extraer también de una cabecera con "
        "'header'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "jsonPath": {
                "type": "string",
                "description": "Expresión JSONPath, p. ej. '$.data.token' o 'data.items[0].id'.",
            },
            "variableName": {
                "type": "string",
                "description": "Nombre de la variable de sesión donde guardar el valor.",
            },
            "regex": {
                "type": "string",
                "description": (
                    "Alternativa a jsonPath: expresión regular sobre el cuerpo crudo. "
                    "Si tiene grupo de captura se usa el grupo 1."
                ),
            },
            "header": {
                "type": "string",
                "description": "Alternativa: extraer el valor de una cabecera de respuesta.",
            },
            "all": {
                "type": "boolean",
                "description": "Devolver todas las coincidencias como array en lugar de la primera.",
            },
            "defaultValue": {
                "description": "Valor a usar si no hay coincidencias (evita el error).",
            },
            "requestIndex": {
                "type": "integer",
                "description": "Extraer de una petición concreta del historial.",
            },
        },
        "additionalProperties": False,
    },
)
def extract_response_data(
    jsonPath: str = "",
    variableName: str = "",
    regex: str = "",
    header: str = "",
    all: bool = False,
    defaultValue: Any = None,
    requestIndex: Optional[int] = None,
) -> Dict[str, Any]:
    _require_enabled()
    if not jsonPath and not regex and not header:
        raise ToolError(
            "Indica 'jsonPath', 'regex' o 'header' para saber qué extraer."
        )

    record = _pick_record(requestIndex)
    matches: List[Any] = []
    method_used = ""

    if jsonPath:
        method_used = "jsonPath"
        try:
            matches = jsonpath(record.response_body, _norm_path(jsonPath))
        except JsonPathError as exc:
            raise ToolError(f"JSONPath inválido '{jsonPath}': {exc}") from exc
    elif regex:
        method_used = "regex"
        try:
            rx = re.compile(regex, re.MULTILINE | re.DOTALL)
        except re.error as exc:
            raise ToolError(f"Expresión regular inválida '{regex}': {exc}") from exc
        for m in rx.finditer(record.response_text):
            matches.append(m.group(1) if m.groups() else m.group(0))
    else:
        method_used = "header"
        for hk, hv in record.response_headers.items():
            if hk.lower() == header.lower():
                matches.append(hv)
                break

    if not matches:
        if defaultValue is not None:
            matches = [defaultValue]
        else:
            hint = ""
            if method_used == "jsonPath" and isinstance(record.response_body, dict):
                hint = f" Claves de primer nivel disponibles: {list(record.response_body)[:15]}."
            raise ToolError(
                f"Sin coincidencias para {method_used} "
                f"'{jsonPath or regex or header}' en la respuesta "
                f"#{record.index} ({record.method} {record.url})."
                + hint
                + " Usa 'defaultValue' si la ausencia es aceptable."
            )

    value: Any = matches if all else matches[0]

    saved = None
    if variableName:
        session.set_var(variableName, value)
        saved = variableName

    return {
        "status": "extracted",
        "method": method_used,
        "expression": jsonPath or regex or header,
        "requestIndex": record.index,
        "value": truncate_body(value, settings.api_max_body_chars),
        "type": _type_name(value),
        "matchCount": len(matches),
        "savedAs": saved,
        "usage": f"{{{{{saved}}}}}" if saved else None,
        "sessionVariables": sorted(session.variables),
    }


# --------------------------------------------------------------------------- #
# 7. run_postman_collection
# --------------------------------------------------------------------------- #
@registry.tool(
    name="run_postman_collection",
    title="Ejecutar colección Postman",
    description=(
        "Ejecuta una colección de Postman (.json, Collection v2.1) y devuelve el "
        "consolidado de aserciones. Si el binario 'newman' está instalado se usa "
        "como runner (soporte completo de scripts). Si no lo está pero hay Node, "
        "se usa un runner nativo que ejecuta las peticiones y evalúa los scripts "
        "pre-request/test con un shim de 'pm'. Sin Node, ejecuta las peticiones y "
        "aplica la aserción implícita de estado 2xx. El campo 'runner' indica "
        "siempre el modo real utilizado."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "collectionPath": {
                "type": "string",
                "description": "Ruta al archivo .json de la colección.",
            },
            "environmentPath": {
                "type": "string",
                "description": "Ruta opcional al environment .json de Postman.",
            },
            "iterationData": {
                "type": "string",
                "description": "Ruta opcional a datos de iteración (.json o .csv).",
            },
            "runner": {
                "type": "string",
                "enum": ["auto", "newman", "native"],
                "description": "Forzar runner concreto. Por defecto 'auto'.",
            },
            "folder": {
                "type": "string",
                "description": "Ejecutar sólo la carpeta indicada de la colección.",
            },
            "bail": {
                "type": "boolean",
                "description": "Detener la ejecución en el primer fallo.",
            },
            "delayMs": {
                "type": "integer",
                "description": "Pausa en ms entre peticiones.",
            },
            "timeoutSeconds": {
                "type": "integer",
                "description": "Timeout global de la ejecución.",
            },
        },
        "required": ["collectionPath"],
        "additionalProperties": False,
    },
)
async def run_postman_collection(
    collectionPath: str,
    environmentPath: str = "",
    iterationData: str = "",
    runner: str = "auto",
    folder: str = "",
    bail: bool = False,
    delayMs: int = 0,
    timeoutSeconds: int = 0,
) -> Dict[str, Any]:
    _require_enabled()

    mode = str(runner or "auto").lower()
    if mode not in ("auto", "newman", "native"):
        raise ToolError("El parámetro 'runner' debe ser 'auto', 'newman' o 'native'.")

    coll_path = resolve_path(collectionPath, must_exist=True)
    if coll_path.is_dir():
        raise ToolError(f"'{display_path(coll_path)}' es un directorio, no una colección.")
    collection = pm.load_collection(coll_path)

    env_path = resolve_path(environmentPath, must_exist=True) if environmentPath else None
    data_path = resolve_path(iterationData, must_exist=True) if iterationData else None

    newman_bin = pm.find_newman()
    if mode == "newman" and not newman_bin:
        raise ToolError(
            "Se forzó runner='newman' pero el binario no está disponible.\n"
            "Instálalo con:  npm install -g newman\n"
            "O usa runner='native' (no requiere newman)."
        )

    timeout = timeoutSeconds if timeoutSeconds > 0 else settings.api_collection_timeout

    if newman_bin and mode in ("auto", "newman"):
        result = _run_with_newman(
            newman_bin, coll_path, env_path, data_path, folder, bail, delayMs, timeout
        )
    else:
        result = await _run_native(
            collection, coll_path, env_path, data_path, folder, bail, delayMs
        )

    result["collection"] = {
        "path": display_path(coll_path),
        "name": (collection.get("info") or {}).get("name") or coll_path.stem,
    }
    if env_path:
        result["environment"] = display_path(env_path)
    if data_path:
        result["iterationData"] = display_path(data_path)

    session.collection_runs.append(result)

    a = result.get("assertions") or {}
    total, failed = a.get("total", 0), a.get("failed", 0)
    _record_assertion(
        f"Colección '{result['collection']['name']}': {total - failed}/{total} aserciones",
        failed == 0 and total > 0,
        category="collection",
        expected="0 fallos",
        actual=f"{failed} fallo(s)",
        detail=f"runner={result.get('runner')}",
    )
    return result


def _run_with_newman(
    newman_bin: str,
    collection: Path,
    env: Optional[Path],
    data: Optional[Path],
    folder: str,
    bail: bool,
    delay_ms: int,
    timeout: int,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mcp-newman-") as tmp:
        report = Path(tmp) / "report.json"
        cmd = [
            newman_bin, "run", str(collection),
            "--reporters", "json",
            "--reporter-json-export", str(report),
        ]
        if env:
            cmd += ["--environment", str(env)]
        if data:
            cmd += ["--iteration-data", str(data)]
        if folder:
            cmd += ["--folder", folder]
        if bail:
            cmd += ["--bail"]
        if delay_ms > 0:
            cmd += ["--delay-request", str(delay_ms)]
        if not settings.api_verify_tls:
            cmd += ["--insecure"]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"La ejecución de newman superó el timeout de {timeout}s."
            ) from exc
        except OSError as exc:
            raise ToolError(f"No se pudo ejecutar newman: {exc}") from exc

        if not report.exists():
            raise ToolError(
                "newman terminó sin generar informe.\n"
                f"Código de salida: {proc.returncode}\n"
                f"stderr: {(proc.stderr or '')[:1500]}"
            )
        try:
            raw = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolError(f"El informe de newman no es JSON válido: {exc}") from exc

    parsed = pm.parse_newman_json(raw)
    parsed["runner"] = "newman"
    parsed["runnerPath"] = newman_bin
    parsed["exitCode"] = proc.returncode
    parsed["passed"] = parsed["assertions"]["failed"] == 0 and proc.returncode == 0
    return parsed


async def _run_native(
    collection: Dict[str, Any],
    coll_path: Path,
    env: Optional[Path],
    data: Optional[Path],
    folder: str,
    bail: bool,
    delay_ms: int,
) -> Dict[str, Any]:
    import asyncio  # noqa: PLC0415

    httpx = _httpx()
    node_bin = pm.have_node()

    variables: Dict[str, Any] = dict(pm.collection_variables(collection))
    if env:
        variables.update(pm.load_environment(env))
    variables.update(session.variables)

    root: Any = collection
    if folder:
        found = _find_folder(collection, folder)
        if found is None:
            raise ToolError(
                f"No existe la carpeta '{folder}' en la colección. "
                f"Carpetas: {_folder_names(collection) or 'ninguna'}."
            )
        root = found

    items = pm.flatten_items(root)
    if not items:
        raise ToolError("La colección no contiene ninguna petición ejecutable.")

    iterations: List[Dict[str, Any]] = (
        pm.load_iteration_data(data) if data else [{}]
    )
    if not iterations:
        iterations = [{}]

    executions: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    script_errors: List[Dict[str, Any]] = []
    total_assertions = failed_assertions = 0
    failed_requests = 0
    times: List[float] = []
    stopped = False
    info = {"collectionName": (collection.get("info") or {}).get("name")}

    started_all = time.perf_counter()

    async with _new_client(
        httpx,
        timeout=settings.api_timeout_seconds,
        verify=settings.api_verify_tls,
        follow_redirects=settings.api_follow_redirects,
    ) as client:
        for it_index, it_data in enumerate(iterations, start=1):
            if stopped:
                break
            for item in items:
                if stopped:
                    break
                label = f"{item['folder']}/{item['name']}" if item["folder"] else item["name"]
                pre_script, test_script = pm.scripts_of(item)
                ctx = {**variables, **{str(k): v for k, v in it_data.items()}}

                # --- pre-request ------------------------------------------ #
                if pre_script and node_bin:
                    out = pm.run_scripts_node(
                        node_bin, pre_script, ctx, None,
                        iteration_data=it_data, info=info,
                        timeout=settings.api_script_timeout,
                    )
                    if out.get("error"):
                        script_errors.append(
                            {"request": label, "phase": "prerequest", "error": out["error"]}
                        )
                    ctx.update(out.get("vars") or {})
                    variables.update(out.get("vars") or {})

                spec = pm.build_request_spec(item["request"])
                url = _interp(spec["url"], ctx)
                if not url:
                    failures.append({"request": label, "assertion": "URL", "message": "El item no tiene URL."})
                    failed_requests += 1
                    continue
                url = _absolute_url(url)
                try:
                    _assert_host_allowed(url)
                except ToolError as exc:
                    failures.append({"request": label, "assertion": "host", "message": str(exc)})
                    failed_requests += 1
                    continue

                hdrs = {k: _stringify(_interp(v, ctx)) for k, v in spec["headers"].items()}
                params = {k: _stringify(_interp(v, ctx)) for k, v in spec["queryParams"].items()}

                item_auth = spec.get("auth")
                if item_auth:
                    saved_auth = session.auth
                    session.auth = {k: _interp(v, ctx) if isinstance(v, str) else v
                                    for k, v in item_auth.items()}
                    extra = session.auth_headers()
                    session.auth = saved_auth
                else:
                    extra = session.auth_headers()
                low = {k.lower() for k in hdrs}
                for k, v in extra.items():
                    if k.lower() not in low:
                        hdrs[k] = v

                kwargs: Dict[str, Any] = {}
                btype = spec["bodyType"]
                if btype == "json" and spec["body"] is not None:
                    raw = _interp(spec["body"], ctx)
                    try:
                        kwargs["json"] = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        kwargs["content"] = str(raw).encode("utf-8")
                    if not any(k.lower() == "content-type" for k in hdrs):
                        hdrs["Content-Type"] = "application/json"
                elif btype == "x-www-form-urlencoded":
                    kwargs["data"] = {
                        k: _stringify(_interp(v, ctx)) for k, v in (spec["body"] or {}).items()
                    }
                elif btype == "form-data":
                    kwargs["data"] = {
                        k: _stringify(_interp(v, ctx)) for k, v in (spec["body"] or {}).items()
                    }
                elif btype == "raw" and spec["body"] is not None:
                    kwargs["content"] = str(_interp(spec["body"], ctx)).encode("utf-8")

                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)

                started = time.perf_counter()
                asserts: List[Dict[str, Any]] = []
                try:
                    resp = await client.request(
                        spec["method"], url, headers=hdrs or None,
                        params=params or None, **kwargs
                    )
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    times.append(elapsed)
                    body_text = resp.text
                    status = resp.status_code
                    status_text = resp.reason_phrase or ""
                    size = len(resp.content)
                    resp_headers = dict(resp.headers)
                    net_error = None
                except Exception as exc:  # noqa: BLE001
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    body_text, status, status_text, size = "", 0, "", 0
                    resp_headers = {}
                    net_error = f"{type_name(exc)}: {exc}"
                    failed_requests += 1

                if net_error:
                    asserts.append({"name": "Petición completada", "passed": False, "error": net_error})
                    failures.append({"request": label, "assertion": "network", "message": net_error})
                elif test_script and node_bin:
                    out = pm.run_scripts_node(
                        node_bin, test_script, ctx,
                        {
                            "code": status, "status": status_text, "text": body_text,
                            "responseTime": elapsed, "responseSize": size,
                            "headers": resp_headers,
                        },
                        request={"method": spec["method"], "url": url},
                        iteration_data=it_data, info=info,
                        timeout=settings.api_script_timeout,
                    )
                    if out.get("error"):
                        script_errors.append(
                            {"request": label, "phase": "test", "error": out["error"]}
                        )
                        asserts.append(
                            {"name": "Script de test ejecutable", "passed": False, "error": out["error"]}
                        )
                        failures.append(
                            {"request": label, "assertion": "script", "message": out["error"]}
                        )
                    for t in out.get("tests") or []:
                        asserts.append(t)
                        if not t.get("passed"):
                            failures.append(
                                {"request": label, "assertion": t.get("name"),
                                 "message": t.get("error")}
                            )
                    variables.update(out.get("vars") or {})
                    ctx.update(out.get("vars") or {})
                else:
                    ok = 200 <= status < 300
                    asserts.append(
                        {
                            "name": "El código de estado es 2xx (aserción implícita)",
                            "passed": ok,
                            "error": None if ok else f"Se recibió {status}",
                        }
                    )
                    if not ok:
                        failures.append(
                            {"request": label, "assertion": "status 2xx",
                             "message": f"Se recibió {status}"}
                        )
                    if test_script and not node_bin:
                        script_errors.append(
                            {
                                "request": label, "phase": "test",
                                "error": "Script omitido: no hay Node ni newman instalados.",
                            }
                        )

                total_assertions += len(asserts)
                failed_now = sum(1 for a in asserts if not a.get("passed"))
                failed_assertions += failed_now

                executions.append(
                    {
                        "iteration": it_index,
                        "name": label,
                        "method": spec["method"],
                        "url": url,
                        "statusCode": status,
                        "statusText": status_text,
                        "responseTimeMs": elapsed,
                        "responseSizeBytes": size,
                        "assertions": asserts,
                        "error": net_error,
                    }
                )

                if bail and failed_now:
                    stopped = True

    session.variables.update(variables)
    total_ms = round((time.perf_counter() - started_all) * 1000, 2)

    return {
        "runner": "native" if node_bin else "python",
        "runnerDetail": (
            f"Peticiones con httpx; scripts evaluados en Node ({node_bin}) con shim de pm."
            if node_bin
            else "Peticiones con httpx; scripts de Postman OMITIDOS (no hay Node ni newman). "
                 "Sólo se aplicó la aserción implícita de estado 2xx."
        ),
        "passed": failed_assertions == 0 and failed_requests == 0,
        "requests": {
            "total": len(executions),
            "failed": failed_requests,
            "pending": 0,
        },
        "assertions": {
            "total": total_assertions,
            "failed": failed_assertions,
            "pending": 0,
        },
        "iterations": {"total": len(iterations), "failed": 0, "pending": 0},
        "totalTimeMs": total_ms,
        "avgResponseTimeMs": round(sum(times) / len(times), 2) if times else None,
        "executions": executions,
        "failures": failures,
        "scriptErrors": script_errors,
        "stoppedEarly": stopped,
        "variablesAfterRun": sorted(variables),
    }


def _interp(value: Any, ctx: Dict[str, Any]) -> Any:
    """Interpola {{vars}} usando el contexto de la colección + la sesión."""
    saved = session.variables
    session.variables = {**saved, **ctx}
    try:
        return session.interpolate(value)
    finally:
        session.variables = saved


def _find_folder(node: Any, name: str) -> Optional[Dict[str, Any]]:
    for item in (node.get("item") if isinstance(node, dict) else node) or []:
        if not isinstance(item, dict):
            continue
        if "item" in item:
            if str(item.get("name")) == name:
                return item
            found = _find_folder(item, name)
            if found:
                return found
    return None


def _folder_names(node: Any, acc: Optional[List[str]] = None) -> List[str]:
    if acc is None:
        acc = []
    for item in (node.get("item") if isinstance(node, dict) else node) or []:
        if isinstance(item, dict) and "item" in item:
            acc.append(str(item.get("name")))
            _folder_names(item, acc)
    return acc


# --------------------------------------------------------------------------- #
# 8. generate_test_report
# --------------------------------------------------------------------------- #
@registry.tool(
    name="generate_test_report",
    title="Generar informe de pruebas",
    description=(
        "Compila el historial de peticiones, tiempos y SLA, validaciones de "
        "esquema y aserciones de la sesión en un informe ejecutivo en Markdown "
        "con formato BDD (Given/When/Then). Devuelve el Markdown y, si se indica "
        "'outputPath', también lo guarda en disco."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "suiteName": {
                "type": "string",
                "description": "Nombre de la suite de pruebas para la cabecera del informe.",
            },
            "includeResponseBody": {
                "type": "boolean",
                "description": "Incluir el cuerpo de las respuestas en el detalle de cada escenario.",
            },
            "environment": {
                "type": "string",
                "description": "Entorno ejecutado (dev, qa, staging, prod…).",
            },
            "outputPath": {
                "type": "string",
                "description": "Ruta donde guardar el .md. Si se omite sólo se devuelve el texto.",
            },
            "maxBodyChars": {
                "type": "integer",
                "description": "Límite de caracteres por cuerpo incluido (por defecto 2000).",
            },
            "reset": {
                "type": "boolean",
                "description": "Vaciar el historial de la sesión tras generar el informe.",
            },
        },
        "required": ["suiteName"],
        "additionalProperties": False,
    },
)
def generate_test_report(
    suiteName: str,
    includeResponseBody: bool = False,
    environment: str = "",
    outputPath: str = "",
    maxBodyChars: int = 2000,
    reset: bool = False,
) -> Dict[str, Any]:
    _require_enabled()
    suite = str(suiteName or "").strip()
    if not suite:
        raise ToolError("El parámetro 'suiteName' no puede estar vacío.")
    if not session.history and not session.collection_runs:
        raise ToolError(
            "No hay nada que informar: la sesión no tiene peticiones ni ejecuciones "
            "de colección. Ejecuta antes 'build_and_send_request' o "
            "'run_postman_collection'."
        )

    stats = session.stats()
    now = datetime.now(timezone.utc)
    verdict = "APROBADO" if stats["assertionsFailed"] == 0 else "RECHAZADO"
    icon = "✅" if verdict == "APROBADO" else "❌"

    L: List[str] = []
    L.append(f"# {icon} Informe de Pruebas de API — {suite}")
    L.append("")
    L.append("## Resumen ejecutivo")
    L.append("")
    L.append("| Métrica | Valor |")
    L.append("| --- | --- |")
    L.append(f"| Suite | {suite} |")
    L.append(f"| Entorno | {environment or 'no especificado'} |")
    L.append(f"| Fecha (UTC) | {now.strftime('%Y-%m-%d %H:%M:%S')} |")
    L.append(f"| Veredicto | **{icon} {verdict}** |")
    L.append(f"| Peticiones ejecutadas | {stats['requests']} |")
    L.append(f"| Peticiones con error de red | {stats['failedRequests']} |")
    L.append(f"| Aserciones totales | {stats['assertions']} |")
    L.append(f"| Aserciones superadas | {stats['assertionsPassed']} |")
    L.append(f"| Aserciones fallidas | {stats['assertionsFailed']} |")
    rate = stats["successRate"]
    L.append(f"| Tasa de éxito | {rate if rate is not None else 'n/a'}{'%' if rate is not None else ''} |")
    L.append(f"| Colecciones ejecutadas | {stats['collectionRuns']} |")
    L.append("")

    rt = stats["responseTimeMs"]
    if rt["avg"] is not None:
        L.append("## Rendimiento (SLA)")
        L.append("")
        L.append("| Métrica | ms |")
        L.append("| --- | --- |")
        L.append(f"| Mínimo | {rt['min']} |")
        L.append(f"| Media | {rt['avg']} |")
        L.append(f"| p90 | {rt['p90']} |")
        L.append(f"| p95 | {rt['p95']} |")
        L.append(f"| Máximo | {rt['max']} |")
        L.append("")

    # --- escenarios BDD ---------------------------------------------------- #
    if session.history:
        L.append("## Escenarios (Given / When / Then)")
        L.append("")
        for rec in session.history:
            passed = all(a.passed for a in rec.assertions)
            mark = "✅" if passed else ("⚠️" if not rec.assertions else "❌")
            title = rec.name or f"{rec.method} {_short_url(rec.url)}"
            L.append(f"### {mark} Escenario {rec.index}: {title}")
            L.append("")

            given: List[str] = []
            if session.auth:
                given.append(f"autenticación **{session.auth.get('type')}** configurada")
            if environment:
                given.append(f"el entorno **{environment}**")
            if rec.request_headers:
                given.append(f"{len(rec.request_headers)} cabecera(s) de petición")
            if not given:
                given.append("una sesión de API sin autenticación previa")
            L.append(f"- **Dado** que {', y '.join(given)}")

            when = f"se ejecuta `{rec.method} {rec.url}`"
            if rec.query_params:
                when += f" con query `{json.dumps(rec.query_params, ensure_ascii=False)}`"
            if rec.body_type:
                when += f" y cuerpo `{rec.body_type}`"
            L.append(f"- **Cuando** {when}")

            if rec.error:
                L.append(f"- **Entonces** ❌ la petición falla: `{rec.error}`")
            else:
                L.append(
                    f"- **Entonces** el servicio responde **{rec.status_code} "
                    f"{rec.status_text}** en **{rec.response_time_ms} ms** "
                    f"({rec.response_size_bytes} bytes)"
                )
            for a in rec.assertions:
                tick = "✔" if a.passed else "✘"
                line = f"  - {tick} **Y** {a.name}"
                if not a.passed:
                    line += (
                        f" — esperado `{pretty(a.expected, 100)}`, "
                        f"real `{pretty(a.actual, 100)}`"
                    )
                    if a.detail:
                        line += f" ({a.detail})"
                L.append(line)
            if not rec.assertions:
                L.append("  - ⚠️ **Y** no se ejecutó ninguna aserción sobre esta petición")
            L.append("")

            if includeResponseBody and rec.response_body is not None:
                body = rec.response_body
                text = (
                    json.dumps(body, indent=2, ensure_ascii=False, default=str)
                    if isinstance(body, (dict, list))
                    else str(body)
                )
                if len(text) > maxBodyChars:
                    text = text[:maxBodyChars] + f"\n… [truncado, {len(text)} caracteres]"
                lang = "json" if rec.json_parsed else "text"
                L.append("<details><summary>Cuerpo de la respuesta</summary>")
                L.append("")
                L.append(f"```{lang}")
                L.append(text)
                L.append("```")
                L.append("")
                L.append("</details>")
                L.append("")

    # --- colecciones -------------------------------------------------------- #
    if session.collection_runs:
        L.append("## Ejecuciones de colecciones Postman")
        L.append("")
        L.append("| Colección | Runner | Peticiones | Aserciones | Fallidas | Tiempo medio |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for run in session.collection_runs:
            c = run.get("collection") or {}
            rq = run.get("requests") or {}
            asr = run.get("assertions") or {}
            L.append(
                f"| {c.get('name', '?')} | {run.get('runner', '?')} | "
                f"{rq.get('total', 0)} | {asr.get('total', 0)} | "
                f"{asr.get('failed', 0)} | {run.get('avgResponseTimeMs', 'n/a')} ms |"
            )
        L.append("")
        for run in session.collection_runs:
            if run.get("failures"):
                L.append(f"**Fallos en '{(run.get('collection') or {}).get('name')}':**")
                L.append("")
                for f in run["failures"][:50]:
                    L.append(f"- `{f.get('request')}` → {f.get('assertion')}: {f.get('message')}")
                L.append("")

    # --- fallos ------------------------------------------------------------- #
    failed = [a for a in session.assertions if not a.passed]
    L.append("## Detalle de fallos")
    L.append("")
    if not failed:
        L.append("_Sin fallos: todas las aserciones se superaron._")
    else:
        L.append("| # | Petición | Categoría | Aserción | Esperado | Real |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for i, a in enumerate(failed, start=1):
            L.append(
                f"| {i} | #{a.request_index} | {a.category} | {_cell(a.name)} | "
                f"{_cell(pretty(a.expected, 80))} | {_cell(pretty(a.actual, 80))} |"
            )
    L.append("")

    # --- variables ----------------------------------------------------------- #
    if session.variables:
        L.append("## Variables de sesión")
        L.append("")
        L.append("| Variable | Tipo | Valor |")
        L.append("| --- | --- | --- |")
        for k in sorted(session.variables):
            v = session.variables[k]
            shown = mask(v) if re.search(r"token|secret|password|apikey|key", k, re.I) else pretty(v, 80)
            L.append(f"| `{k}` | {_type_name(v)} | {_cell(shown)} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append(
        f"_Informe generado por **unified-mcp-server** v{settings.server_version} "
        f"· {now.isoformat(timespec='seconds')} · sesión iniciada {stats['startedAt']}_"
    )
    L.append("")

    markdown = "\n".join(L)

    saved: Optional[str] = None
    if outputPath:
        raw = outputPath
        if settings.api_report_dir and not Path(raw).is_absolute():
            raw = str(Path(settings.api_report_dir) / raw)
        target = resolve_path(raw)
        if target.is_dir():
            target = target / f"{_slug(suite)}-{now.strftime('%Y%m%d-%H%M%S')}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        saved = display_path(target)

    result = {
        "suiteName": suite,
        "environment": environment or None,
        "verdict": verdict,
        "passed": verdict == "APROBADO",
        "generatedAt": now.isoformat(timespec="seconds"),
        "summary": stats,
        "scenarios": len(session.history),
        "failedAssertions": len(failed),
        "savedTo": saved,
        "markdown": markdown,
        "sizeChars": len(markdown),
    }

    if reset:
        session.reset(keep_variables=True, keep_auth=True)
        result["sessionReset"] = True
    return result


def _cell(text: Any) -> str:
    """Escapa un valor para que no rompa una tabla Markdown."""
    return str(text).replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _short_url(url: str, limit: int = 70) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower() or "informe"


# --------------------------------------------------------------------------- #
# Extra: inspección de la sesión (no estaba en el listado, pero es clave para QA)
# --------------------------------------------------------------------------- #
@registry.tool(
    name="get_api_session",
    title="Inspeccionar sesión de API",
    description=(
        "Devuelve el estado actual de la sesión de API testing: variables "
        "definidas, autenticación (enmascarada), estadísticas y resumen del "
        "historial de peticiones. Útil para depurar por qué una {{variable}} no "
        "se resuelve o qué respuesta está activa. Con 'reset': true limpia la "
        "sesión."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reset": {
                "type": "boolean",
                "description": "Limpiar historial, aserciones y (según flags) variables/auth.",
            },
            "keepVariables": {
                "type": "boolean",
                "description": "Al resetear, conservar las variables de sesión.",
            },
            "keepAuth": {
                "type": "boolean",
                "description": "Al resetear, conservar la autenticación configurada.",
            },
            "includeHistory": {
                "type": "boolean",
                "description": "Incluir el resumen de cada petición del historial.",
            },
        },
        "additionalProperties": False,
    },
)
def get_api_session(
    reset: bool = False,
    keepVariables: bool = False,
    keepAuth: bool = False,
    includeHistory: bool = True,
) -> Dict[str, Any]:
    _require_enabled()
    if reset:
        session.reset(keep_variables=keepVariables, keep_auth=keepAuth)
        return {
            "status": "reset",
            "keptVariables": keepVariables,
            "keptAuth": keepAuth,
            "stats": session.stats(),
        }

    out: Dict[str, Any] = {
        "auth": session.auth_public(),
        "variables": {
            k: (mask(v) if re.search(r"token|secret|password|apikey", k, re.I) else v)
            for k, v in session.variables.items()
        },
        "stats": session.stats(),
        "runners": {
            "newman": pm.find_newman() or "no instalado (npm install -g newman)",
            "node": pm.have_node() or "no instalado",
        },
        "config": {
            "baseUrl": settings.api_base_url or None,
            "timeoutSeconds": settings.api_timeout_seconds,
            "verifyTls": settings.api_verify_tls,
            "followRedirects": settings.api_follow_redirects,
            "hostAllowlist": settings.api_host_allowlist or "sin restricción",
        },
    }
    if _TLS_WARNING:
        out["warnings"] = list(dict.fromkeys(_TLS_WARNING))
    if includeHistory:
        out["history"] = [r.summary() for r in session.history]
        out["lastResponse"] = (
            session.history[-1].to_dict(include_body=False) if session.history else None
        )
    return out

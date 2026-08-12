"""
Estado de la sesión de API Testing.

Mantiene, igual que un "environment" de Postman más el contexto de un
escenario Serenity REST:

  * `variables`   : variables de sesión ({{placeholders}} interpolables)
  * `auth`        : credenciales globales aplicadas a cada petición
  * `history`     : historial completo de peticiones/respuestas
  * `assertions`  : resultado de cada aserción ejecutada
  * `last`        : última respuesta, sobre la que operan las validaciones

Es un singleton en proceso: todas las tools comparten la misma sesión, que es
justo lo que permite encadenar  build_and_send_request -> extract_response_data
-> validate_api_response  sin repetir contexto.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.registry import ToolError

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-\[\]]+)\s*\}\}")

# Cabeceras cuyo valor nunca se devuelve en claro en historial ni informes.
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "x-access-token",
}

MAX_BODY_CHARS = 20_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mask(value: Any, keep: int = 4) -> str:
    """Enmascara un secreto dejando visibles los últimos `keep` caracteres."""
    text = str(value or "")
    if len(text) <= keep:
        return "***"
    return "***" + text[-keep:]


def redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (headers or {}).items():
        out[k] = mask(v) if str(k).lower() in _SENSITIVE_HEADERS else v
    return out


# --------------------------------------------------------------------------- #
@dataclass
class Assertion:
    """Resultado de una única aserción."""

    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    category: str = "value"  # status | sla | field | value | schema | collection
    request_index: Optional[int] = None
    at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "category": self.category,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
            "requestIndex": self.request_index,
            "at": self.at,
        }


@dataclass
class RequestRecord:
    """Una petición ejecutada y su respuesta."""

    index: int
    method: str
    url: str
    request_headers: Dict[str, Any]
    query_params: Dict[str, Any]
    body_type: Optional[str]
    request_body: Any
    files: List[Dict[str, Any]]
    status_code: int
    status_text: str
    response_time_ms: float
    response_headers: Dict[str, Any]
    response_body: Any
    response_text: str
    response_size_bytes: int
    json_parsed: bool
    error: Optional[str] = None
    at: str = field(default_factory=_now_iso)
    assertions: List[Assertion] = field(default_factory=list)
    name: str = ""

    # -- vistas ------------------------------------------------------------ #
    def summary(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name or f"{self.method} {self.url}",
            "method": self.method,
            "url": self.url,
            "statusCode": self.status_code,
            "statusText": self.status_text,
            "responseTimeMs": self.response_time_ms,
            "responseSizeBytes": self.response_size_bytes,
            "at": self.at,
            "assertions": {
                "total": len(self.assertions),
                "passed": sum(1 for a in self.assertions if a.passed),
                "failed": sum(1 for a in self.assertions if not a.passed),
            },
        }

    def to_dict(self, include_body: bool = True) -> Dict[str, Any]:
        data = self.summary()
        data.update(
            {
                "requestHeaders": redact_headers(self.request_headers),
                "queryParams": self.query_params,
                "bodyType": self.body_type,
                "files": self.files,
                "responseHeaders": redact_headers(self.response_headers),
                "jsonParsed": self.json_parsed,
                "error": self.error,
            }
        )
        if include_body:
            data["requestBody"] = self.request_body
            data["responseBody"] = self.response_body
        return data


# --------------------------------------------------------------------------- #
class ApiSession:
    """Contexto compartido por todas las tools de API testing."""

    def __init__(self) -> None:
        self.variables: Dict[str, Any] = {}
        self.auth: Dict[str, Any] = {}
        self.history: List[RequestRecord] = []
        self.assertions: List[Assertion] = []
        self.collection_runs: List[Dict[str, Any]] = []
        self.started_at: str = _now_iso()
        self._counter: int = 0

    # -- reset -------------------------------------------------------------- #
    def reset(self, keep_variables: bool = False, keep_auth: bool = False) -> None:
        if not keep_variables:
            self.variables = {}
        if not keep_auth:
            self.auth = {}
        self.history = []
        self.assertions = []
        self.collection_runs = []
        self.started_at = _now_iso()
        self._counter = 0

    # -- variables ---------------------------------------------------------- #
    def set_var(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ToolError("El nombre de la variable no puede estar vacío.")
        self.variables[key.strip()] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def interpolate(self, value: Any, _depth: int = 0) -> Any:
        """
        Sustituye {{variable}} recursivamente en strings, dicts y listas.

        Si el string es EXACTAMENTE '{{var}}' se devuelve el valor con su tipo
        original (número, bool, objeto…), igual que hace Postman.
        """
        if _depth > 10:
            return value

        if isinstance(value, str):
            whole = _VAR_RE.fullmatch(value.strip())
            if whole:
                name = whole.group(1)
                if name in self.variables:
                    return self.interpolate(self.variables[name], _depth + 1)
                return value

            def _sub(m: re.Match[str]) -> str:
                name = m.group(1)
                if name not in self.variables:
                    return m.group(0)
                v = self.variables[name]
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False)
                if isinstance(v, bool):
                    return "true" if v else "false"
                return "" if v is None else str(v)

            return _VAR_RE.sub(_sub, value)

        if isinstance(value, dict):
            return {
                self.interpolate(k, _depth + 1): self.interpolate(v, _depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.interpolate(v, _depth + 1) for v in value]
        return value

    def unresolved(self, *values: Any) -> List[str]:
        """Nombres de {{variables}} que quedaron sin resolver."""
        found: List[str] = []
        for value in values:
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str
            )
            for name in _VAR_RE.findall(text):
                if name not in self.variables and name not in found:
                    found.append(name)
        return found

    # -- autenticación ------------------------------------------------------ #
    def auth_headers(self) -> Dict[str, str]:
        """Cabeceras derivadas de la configuración de auth global."""
        if not self.auth:
            return {}
        kind = self.auth.get("type")
        resolved = {k: self.interpolate(v) for k, v in self.auth.items()}

        if kind in ("bearer", "oauth2"):
            token = resolved.get("token") or ""
            if not token:
                return {}
            prefix = resolved.get("prefix") or "Bearer"
            return {"Authorization": f"{prefix} {token}"}

        if kind == "basic":
            user = resolved.get("username") or ""
            pwd = resolved.get("password") or ""
            raw = f"{user}:{pwd}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

        if kind == "apiKey":
            header = resolved.get("headerName") or "X-API-Key"
            token = resolved.get("token") or ""
            if not token:
                return {}
            return {header: token}

        return {}

    def auth_public(self) -> Dict[str, Any]:
        """Vista de la auth con los secretos enmascarados."""
        if not self.auth:
            return {"type": "none", "configured": False}
        out: Dict[str, Any] = {"configured": True}
        for k, v in self.auth.items():
            out[k] = mask(v) if k in ("token", "password", "clientSecret") else v
        return out

    # -- historial ---------------------------------------------------------- #
    def next_index(self) -> int:
        self._counter += 1
        return self._counter

    def record(self, rec: RequestRecord) -> None:
        self.history.append(rec)

    @property
    def last(self) -> RequestRecord:
        if not self.history:
            raise ToolError(
                "No hay ninguna respuesta previa en la sesión. "
                "Ejecuta primero 'build_and_send_request'."
            )
        return self.history[-1]

    def add_assertion(self, assertion: Assertion) -> Assertion:
        if assertion.request_index is None and self.history:
            assertion.request_index = self.history[-1].index
        self.assertions.append(assertion)
        if self.history and assertion.request_index == self.history[-1].index:
            self.history[-1].assertions.append(assertion)
        return assertion

    # -- métricas ----------------------------------------------------------- #
    def stats(self) -> Dict[str, Any]:
        times = [r.response_time_ms for r in self.history if r.error is None]
        passed = sum(1 for a in self.assertions if a.passed)
        failed = len(self.assertions) - passed
        ordered = sorted(times)

        def pct(p: float) -> Optional[float]:
            if not ordered:
                return None
            k = min(len(ordered) - 1, max(0, int(round(p / 100 * len(ordered))) - 1))
            return round(ordered[k], 2)

        return {
            "requests": len(self.history),
            "failedRequests": sum(1 for r in self.history if r.error is not None),
            "assertions": len(self.assertions),
            "assertionsPassed": passed,
            "assertionsFailed": failed,
            "successRate": round(passed / len(self.assertions) * 100, 2)
            if self.assertions
            else None,
            "responseTimeMs": {
                "min": round(min(times), 2) if times else None,
                "max": round(max(times), 2) if times else None,
                "avg": round(sum(times) / len(times), 2) if times else None,
                "p90": pct(90),
                "p95": pct(95),
            },
            "variables": len(self.variables),
            "collectionRuns": len(self.collection_runs),
            "startedAt": self.started_at,
        }


def parse_body_text(text: str, content_type: str) -> tuple[Any, bool]:
    """
    Intenta interpretar el cuerpo de la respuesta.

    Devuelve (valor, se_parseó_como_json).
    """
    stripped = (text or "").strip()
    looks_json = "json" in (content_type or "").lower() or stripped[:1] in "[{"
    if stripped and looks_json:
        try:
            return json.loads(stripped), True
        except json.JSONDecodeError:
            pass
    return text, False


def truncate_body(value: Any, limit: int = MAX_BODY_CHARS) -> Any:
    """Recorta cuerpos gigantes para no reventar la respuesta MCP."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n…[truncado: {len(value) - limit} caracteres más]"
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False, default=str)
        if len(raw) > limit:
            return {
                "_truncated": True,
                "_originalSizeChars": len(raw),
                "_preview": raw[:limit] + "…",
            }
    return value


session = ApiSession()

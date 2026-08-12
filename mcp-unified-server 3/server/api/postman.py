"""
Ejecución de colecciones Postman.

Estrategia en tres niveles, de mayor a menor fidelidad:

  1. `newman`  — si el binario está instalado se delega en él por completo
                 (`--reporters json`) y se consolida su informe. Es el modo
                 canónico: soporta el 100 % del lenguaje de scripts.
  2. `node`    — si hay Node pero no newman, las peticiones las ejecuta Python
                 (httpx) y los scripts pre-request/test se evalúan en Node con
                 un shim de `pm` (subconjunto de Postman + chai).
  3. `python`  — sin Node: se ejecutan las peticiones y se aplica la aserción
                 implícita de estado 2xx; los scripts se reportan como omitidos.

El modo realmente usado se devuelve siempre en el campo `runner`, para que
nadie confunda una ejecución degradada con una completa.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.registry import ToolError

__all__ = [
    "find_newman",
    "have_node",
    "load_collection",
    "load_environment",
    "load_iteration_data",
    "flatten_items",
    "build_request_spec",
    "run_scripts_node",
    "parse_newman_json",
    "NODE_SHIM",
]


# --------------------------------------------------------------------------- #
# Detección de runners
# --------------------------------------------------------------------------- #
def find_newman() -> Optional[str]:
    for candidate in ("newman", "newman.cmd"):
        path = shutil.which(candidate)
        if path:
            return path
    # newman instalado localmente en el proyecto
    local = Path.cwd() / "node_modules" / ".bin" / "newman"
    if local.exists():
        return str(local)
    return None


def have_node() -> Optional[str]:
    return shutil.which("node")


# --------------------------------------------------------------------------- #
# Carga de artefactos
# --------------------------------------------------------------------------- #
def _read_json(path: Path, label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"No se pudo leer {label} '{path}': {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{label} '{path}' no es JSON válido (línea {exc.lineno}, col {exc.colno}): {exc.msg}"
        ) from exc


def load_collection(path: Path) -> Dict[str, Any]:
    data = _read_json(path, "la colección")
    if not isinstance(data, dict):
        raise ToolError("La colección debe ser un objeto JSON de Postman.")
    if "item" not in data:
        raise ToolError(
            "El archivo no parece una colección Postman: falta la clave 'item'. "
            "Exporta la colección en formato Collection v2.1."
        )
    return data


def load_environment(path: Path) -> Dict[str, Any]:
    data = _read_json(path, "el environment")
    out: Dict[str, Any] = {}
    values = data.get("values") if isinstance(data, dict) else None
    if isinstance(values, list):  # formato export de Postman
        for entry in values:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False or entry.get("disabled") is True:
                continue
            key = entry.get("key")
            if key:
                out[str(key)] = entry.get("value")
    elif isinstance(data, dict):  # objeto plano clave/valor
        out = {str(k): v for k, v in data.items()}
    return out


def load_iteration_data(path: Path) -> List[Dict[str, Any]]:
    """Datos de iteración desde .json (array de objetos) o .csv."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        except OSError as exc:
            raise ToolError(f"No se pudo leer el CSV '{path}': {exc}") from exc
    data = _read_json(path, "los datos de iteración")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    raise ToolError(
        "Los datos de iteración deben ser un array de objetos JSON o un CSV."
    )


def collection_variables(collection: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for entry in collection.get("variable") or []:
        if isinstance(entry, dict) and entry.get("key"):
            if entry.get("disabled") is True:
                continue
            out[str(entry["key"])] = entry.get("value")
    return out


# --------------------------------------------------------------------------- #
# Aplanado de items
# --------------------------------------------------------------------------- #
def flatten_items(
    node: Any, folder: str = "", acc: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Recorre carpetas anidadas y devuelve la lista lineal de peticiones."""
    if acc is None:
        acc = []
    items = node.get("item") if isinstance(node, dict) else node
    if not isinstance(items, list):
        return acc

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "sin nombre")
        if "item" in item:  # es carpeta
            flatten_items(item, f"{folder}/{name}" if folder else name, acc)
        elif "request" in item:
            acc.append(
                {
                    "name": name,
                    "folder": folder,
                    "request": item["request"],
                    "event": item.get("event") or [],
                }
            )
    return acc


def _script_of(events: List[Any], listen: str) -> str:
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("listen") != listen:
            continue
        script = ev.get("script") or {}
        exec_ = script.get("exec")
        if isinstance(exec_, list):
            return "\n".join(str(x) for x in exec_)
        if isinstance(exec_, str):
            return exec_
    return ""


def scripts_of(item: Dict[str, Any]) -> Tuple[str, str]:
    """(pre-request, test) de un item."""
    events = item.get("event") or []
    return _script_of(events, "prerequest"), _script_of(events, "test")


# --------------------------------------------------------------------------- #
# Traducción de un request Postman -> spec neutro
# --------------------------------------------------------------------------- #
def _url_to_string(url: Any) -> str:
    if isinstance(url, str):
        return url
    if not isinstance(url, dict):
        return ""
    if url.get("raw"):
        return str(url["raw"])
    proto = url.get("protocol") or "https"
    host = url.get("host")
    host_s = ".".join(host) if isinstance(host, list) else str(host or "")
    port = f":{url['port']}" if url.get("port") else ""
    path = url.get("path")
    if isinstance(path, list):
        path_s = "/" + "/".join(str(p) for p in path)
    else:
        path_s = str(path or "")
    return f"{proto}://{host_s}{port}{path_s}"


def build_request_spec(request: Any) -> Dict[str, Any]:
    """Normaliza el `request` de Postman a la forma que consume el cliente HTTP."""
    if isinstance(request, str):
        return {
            "method": "GET",
            "url": request,
            "headers": {},
            "queryParams": {},
            "bodyType": None,
            "body": None,
        }
    if not isinstance(request, dict):
        raise ToolError("Item de colección sin 'request' válido.")

    method = str(request.get("method") or "GET").upper()
    url_node = request.get("url")
    url = _url_to_string(url_node)

    headers: Dict[str, Any] = {}
    for h in request.get("header") or []:
        if isinstance(h, dict) and h.get("key") and not h.get("disabled"):
            headers[str(h["key"])] = h.get("value", "")

    query: Dict[str, Any] = {}
    if isinstance(url_node, dict):
        for q in url_node.get("query") or []:
            if isinstance(q, dict) and q.get("key") and not q.get("disabled"):
                query[str(q["key"])] = q.get("value", "")

    body_type: Optional[str] = None
    body: Any = None
    files: List[Dict[str, str]] = []
    b = request.get("body") or {}
    if isinstance(b, dict):
        mode = b.get("mode")
        if mode == "raw":
            raw = b.get("raw") or ""
            lang = ((b.get("options") or {}).get("raw") or {}).get("language")
            if lang == "json" or (raw.strip()[:1] in "[{"):
                body_type, body = "json", raw
            else:
                body_type, body = "raw", raw
        elif mode == "urlencoded":
            body_type = "x-www-form-urlencoded"
            body = {
                str(x["key"]): x.get("value", "")
                for x in b.get("urlencoded") or []
                if isinstance(x, dict) and x.get("key") and not x.get("disabled")
            }
        elif mode == "formdata":
            body_type = "form-data"
            body = {}
            for x in b.get("formdata") or []:
                if not isinstance(x, dict) or not x.get("key") or x.get("disabled"):
                    continue
                if x.get("type") == "file":
                    src = x.get("src")
                    src = src[0] if isinstance(src, list) and src else src
                    if src:
                        files.append({"fieldName": str(x["key"]), "filePath": str(src)})
                else:
                    body[str(x["key"])] = x.get("value", "")
        elif mode == "graphql":
            body_type = "json"
            gql = b.get("graphql") or {}
            payload: Dict[str, Any] = {"query": gql.get("query", "")}
            variables = gql.get("variables")
            if variables:
                if isinstance(variables, str):
                    try:
                        variables = json.loads(variables)
                    except json.JSONDecodeError:
                        pass
                payload["variables"] = variables
            body = json.dumps(payload, ensure_ascii=False)

    spec: Dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers,
        "queryParams": query,
        "bodyType": body_type,
        "body": body,
    }
    if files:
        spec["files"] = files

    # Auth a nivel de request
    auth = request.get("auth")
    if isinstance(auth, dict):
        spec["auth"] = _translate_auth(auth)
    return spec


def _translate_auth(auth: Dict[str, Any]) -> Dict[str, Any]:
    kind = auth.get("type")
    entries = auth.get(kind) if isinstance(auth.get(kind), list) else []
    values = {
        str(e.get("key")): e.get("value")
        for e in entries
        if isinstance(e, dict) and e.get("key")
    }
    if kind == "bearer":
        return {"type": "bearer", "token": values.get("token", "")}
    if kind == "basic":
        return {
            "type": "basic",
            "username": values.get("username", ""),
            "password": values.get("password", ""),
        }
    if kind == "apikey":
        return {
            "type": "apiKey",
            "headerName": values.get("key", "X-API-Key"),
            "token": values.get("value", ""),
        }
    if kind == "oauth2":
        return {"type": "oauth2", "token": values.get("accessToken", "")}
    return {"type": str(kind or "none")}


# --------------------------------------------------------------------------- #
# Shim de `pm` para Node
# --------------------------------------------------------------------------- #
NODE_SHIM = r"""
'use strict';
// Shim de Postman `pm` + subconjunto de chai. Recibe un JSON por stdin y
// devuelve por stdout {tests:[...], vars:{...}, logs:[...], error:null}.
let __input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => { __input += d; });
process.stdin.on('end', () => {
  let out = { tests: [], vars: {}, logs: [], error: null, nextRequest: undefined };
  try { out = run(JSON.parse(__input), out); }
  catch (e) { out.error = String(e && e.stack || e); }
  process.stdout.write(JSON.stringify(out));
});

class AssertErr extends Error {}

function typeOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function show(v) {
  try { const s = JSON.stringify(v); return s === undefined ? String(v) : s; }
  catch (e) { return String(v); }
}

function makeExpect(actual, negated, label) {
  const fail = m => { throw new AssertErr('esperado ' + (negated ? 'NO ' : '') + m + ', valor real: ' + show(actual)); };
  const ok = c => { if (negated ? c : !c) fail.apply(null, [].slice.call(arguments, 1)); };
  const chk = (cond, msg) => { if (negated ? cond : !cond) fail(msg); };

  const api = {
    get to() { return api; }, get be() { return api; }, get been() { return api; },
    get is() { return api; }, get that() { return api; }, get which() { return api; },
    get and() { return api; }, get has() { return api; }, get have() { return api; },
    get with() { return api; }, get at() { return api; }, get of() { return api; },
    get same() { return api; }, get an() { return api; }, get a() { return aFn; },
    get not() { return makeExpect(actual, !negated, label); },
    get ok() { chk(!!actual, 'un valor truthy'); return api; },
    get true() { chk(actual === true, 'true'); return api; },
    get false() { chk(actual === false, 'false'); return api; },
    get null() { chk(actual === null, 'null'); return api; },
    get undefined() { chk(actual === undefined, 'undefined'); return api; },
    get empty() {
      const n = actual == null ? 0 : (typeof actual === 'object' && !Array.isArray(actual) ? Object.keys(actual).length : actual.length);
      chk(n === 0, 'vacío'); return api;
    },
    eql: v => { chk(eq(actual, v), 'igual a ' + show(v)); return api; },
    eqls: v => api.eql(v),
    deep: null,
    equal: v => { chk(actual === v || eq(actual, v), 'igual a ' + show(v)); return api; },
    equals: v => api.equal(v),
    above: v => { chk(Number(actual) > v, '> ' + v); return api; },
    greaterThan: v => api.above(v),
    least: v => { chk(Number(actual) >= v, '>= ' + v); return api; },
    below: v => { chk(Number(actual) < v, '< ' + v); return api; },
    lessThan: v => api.below(v),
    most: v => { chk(Number(actual) <= v, '<= ' + v); return api; },
    within: (a, b) => { chk(Number(actual) >= a && Number(actual) <= b, 'entre ' + a + ' y ' + b); return api; },
    lengthOf: v => { chk(actual != null && actual.length === v, 'longitud ' + v); return api; },
    match: rx => { chk(new RegExp(rx).test(String(actual)), 'que case con ' + rx); return api; },
    oneOf: arr => { chk(arr.some(x => eq(actual, x)), 'uno de ' + show(arr)); return api; },
    include: v => {
      let c;
      if (typeof actual === 'string') c = actual.indexOf(String(v)) >= 0;
      else if (Array.isArray(actual)) c = actual.some(x => eq(x, v));
      else if (actual && typeof actual === 'object' && v && typeof v === 'object')
        c = Object.keys(v).every(k => eq(actual[k], v[k]));
      else c = false;
      chk(!!c, 'que incluya ' + show(v)); return api;
    },
    includes: v => api.include(v),
    contain: v => api.include(v),
    contains: v => api.include(v),
    property: (k, v) => {
      const has = actual != null && Object.prototype.hasOwnProperty.call(actual, k);
      chk(has, "la propiedad '" + k + "'");
      if (v !== undefined && !negated) chk(eq(actual[k], v), "la propiedad '" + k + "' = " + show(v));
      return makeExpect(has ? actual[k] : undefined, false, k);
    },
    keys: function () {
      const want = Array.isArray(arguments[0]) ? arguments[0] : [].slice.call(arguments);
      const ks = actual ? Object.keys(actual) : [];
      chk(want.every(k => ks.indexOf(k) >= 0), 'las claves ' + show(want)); return api;
    },
    status: code => { chk(Number(actual) === Number(code), 'status ' + code); return api; },
    jsonBody: (p, v) => { chk(actual != null, 'cuerpo JSON'); return api; },
    instanceof: C => { chk(actual instanceof C, 'instancia de ' + C.name); return api; },
    exist: undefined
  };
  Object.defineProperty(api, 'exist', { get() { chk(actual !== null && actual !== undefined, 'que exista'); return api; } });
  Object.defineProperty(api, 'deep', { get() { return api; } });
  function aFn(t) { chk(typeOf(actual) === t, 'tipo ' + t); return api; }
  ['to','be','been','is','that','which','and','has','have','with','at','of','same','an'].forEach(k => {
    Object.defineProperty(aFn, k, { get() { return api[k]; } });
  });
  return api;
}

function run(input, out) {
  const vars = Object.assign({}, input.variables || {});
  const resp = input.response || null;
  const tests = out.tests;

  const expect = (v) => makeExpect(v, false, '');
  expect.fail = (m) => { throw new AssertErr(m || 'expect.fail()'); };

  const store = {
    get: k => vars[k],
    set: (k, v) => { vars[k] = v; },
    has: k => Object.prototype.hasOwnProperty.call(vars, k),
    unset: k => { delete vars[k]; },
    toObject: () => Object.assign({}, vars),
    clear: () => { for (const k of Object.keys(vars)) delete vars[k]; }
  };

  let parsed, parseErr = null;
  const response = resp ? {
    code: resp.code,
    status: resp.status,
    responseTime: resp.responseTime,
    responseSize: resp.responseSize,
    text: () => resp.text,
    json: () => {
      if (parsed === undefined) {
        try { parsed = JSON.parse(resp.text); } catch (e) { parseErr = e; parsed = null; }
      }
      if (parseErr) throw new AssertErr('la respuesta no es JSON válido: ' + parseErr.message);
      return parsed;
    },
    headers: {
      get: n => { const k = Object.keys(resp.headers || {}).find(h => h.toLowerCase() === String(n).toLowerCase()); return k ? resp.headers[k] : undefined; },
      has: n => Object.keys(resp.headers || {}).some(h => h.toLowerCase() === String(n).toLowerCase()),
      all: () => resp.headers || {}
    },
    to: null
  } : null;
  if (response) {
    Object.defineProperty(response, 'to', {
      get() {
        const codeExp = makeExpect(resp.code, false, 'code');
        return {
          get be() {
            return {
              get ok() { if (!(resp.code >= 200 && resp.code < 300)) throw new AssertErr('se esperaba 2xx, llegó ' + resp.code); return true; },
              get success() { return this.ok; },
              get error() { if (resp.code < 400) throw new AssertErr('se esperaba error, llegó ' + resp.code); return true; },
              get clientError() { if (!(resp.code >= 400 && resp.code < 500)) throw new AssertErr('se esperaba 4xx, llegó ' + resp.code); return true; },
              get serverError() { if (!(resp.code >= 500)) throw new AssertErr('se esperaba 5xx, llegó ' + resp.code); return true; },
              get json() { response.json(); return true; },
              get notFound() { if (resp.code !== 404) throw new AssertErr('se esperaba 404, llegó ' + resp.code); return true; },
              get unauthorized() { if (resp.code !== 401) throw new AssertErr('se esperaba 401, llegó ' + resp.code); return true; }
            };
          },
          get have() {
            return {
              status: s => {
                if (typeof s === 'number') { if (resp.code !== s) throw new AssertErr('se esperaba status ' + s + ', llegó ' + resp.code); }
                else if (String(resp.status).toLowerCase() !== String(s).toLowerCase()) throw new AssertErr('se esperaba status "' + s + '", llegó "' + resp.status + '"');
                return true;
              },
              header: (n, v) => {
                if (!response.headers.has(n)) throw new AssertErr("falta la cabecera '" + n + "'");
                if (v !== undefined && response.headers.get(n) !== v) throw new AssertErr("cabecera '" + n + "' = " + response.headers.get(n) + ', se esperaba ' + v);
                return true;
              },
              jsonBody: (p, v) => {
                const b = response.json();
                if (p === undefined) return true;
                const val = String(p).split('.').reduce((a, k) => (a == null ? a : a[k]), b);
                if (val === undefined) throw new AssertErr("el cuerpo no tiene '" + p + "'");
                if (v !== undefined && JSON.stringify(val) !== JSON.stringify(v)) throw new AssertErr("'" + p + "' = " + show(val) + ', se esperaba ' + show(v));
                return true;
              },
              body: v => { if (v !== undefined && resp.text !== v) throw new AssertErr('cuerpo distinto del esperado'); return true; }
            };
          },
          get not() { return { get be() { return { get ok() { if (resp.code >= 200 && resp.code < 300) throw new AssertErr('no se esperaba 2xx'); return true; } }; }, get have() { return { status: s => { if (resp.code === s) throw new AssertErr('no se esperaba ' + s); return true; } }; } }; }
        };
      }
    });
  }

  const pm = {
    environment: store, globals: store, variables: store, collectionVariables: store,
    iterationData: { get: k => (input.iterationData || {})[k], toObject: () => Object.assign({}, input.iterationData || {}) },
    response: response,
    request: input.request || {},
    info: input.info || {},
    expect: expect,
    sendRequest: () => { throw new AssertErr('pm.sendRequest no está soportado en el runner nativo; instala newman.'); },
    setNextRequest: n => { out.nextRequest = n; },
    test: (name, fn) => {
      try { fn(); tests.push({ name: String(name), passed: true, error: null }); }
      catch (e) { tests.push({ name: String(name), passed: false, error: String(e && e.message || e) }); }
    }
  };

  const console2 = { log: (...a) => out.logs.push(a.map(show).join(' ')), error: (...a) => out.logs.push('ERROR ' + a.map(show).join(' ')), warn: (...a) => out.logs.push('WARN ' + a.map(show).join(' ')), info: (...a) => out.logs.push(a.map(show).join(' ')) };

  const fn = new Function('pm', 'console', 'responseCode', 'responseBody', 'responseTime', 'tests', 'postman', 'require',
    '"use strict";\n' + (input.script || ''));

  const legacyTests = {};
  const postman = {
    setEnvironmentVariable: (k, v) => { vars[k] = v; },
    getEnvironmentVariable: k => vars[k],
    setGlobalVariable: (k, v) => { vars[k] = v; },
    getGlobalVariable: k => vars[k],
    setNextRequest: n => { out.nextRequest = n; }
  };
  const req = () => { throw new AssertErr("require() no está disponible en el runner nativo (sólo en newman)."); };

  fn(pm, console2,
     resp ? { code: resp.code, name: resp.status } : undefined,
     resp ? resp.text : undefined,
     resp ? resp.responseTime : undefined,
     legacyTests, postman, req);

  // Soporte del estilo antiguo: tests["nombre"] = booleano
  for (const k of Object.keys(legacyTests)) {
    tests.push({ name: k, passed: !!legacyTests[k], error: legacyTests[k] ? null : 'aserción legacy falsa' });
  }

  out.vars = vars;
  return out;
}
"""


def run_scripts_node(
    node_bin: str,
    script: str,
    variables: Dict[str, Any],
    response: Optional[Dict[str, Any]],
    request: Optional[Dict[str, Any]] = None,
    iteration_data: Optional[Dict[str, Any]] = None,
    info: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Evalúa un script de Postman en Node con el shim `pm`."""
    if not script.strip():
        return {"tests": [], "vars": dict(variables), "logs": [], "error": None}

    payload = json.dumps(
        {
            "script": script,
            "variables": variables,
            "response": response,
            "request": request or {},
            "iterationData": iteration_data or {},
            "info": info or {},
        },
        ensure_ascii=False,
        default=str,
    )

    with tempfile.TemporaryDirectory(prefix="mcp-pm-") as tmp:
        shim = Path(tmp) / "pm-shim.js"
        shim.write_text(NODE_SHIM, encoding="utf-8")
        try:
            proc = subprocess.run(
                [node_bin, str(shim)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "NODE_OPTIONS": ""},
            )
        except subprocess.TimeoutExpired:
            return {
                "tests": [],
                "vars": dict(variables),
                "logs": [],
                "error": f"El script excedió el timeout de {timeout}s.",
            }
        except OSError as exc:
            return {
                "tests": [],
                "vars": dict(variables),
                "logs": [],
                "error": f"No se pudo ejecutar Node: {exc}",
            }

    if proc.returncode != 0 and not proc.stdout.strip():
        return {
            "tests": [],
            "vars": dict(variables),
            "logs": [],
            "error": (proc.stderr or "Node terminó con error").strip()[:2000],
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "tests": [],
            "vars": dict(variables),
            "logs": [],
            "error": "Salida no interpretable del shim: "
            + (proc.stdout or proc.stderr)[:500],
        }


# --------------------------------------------------------------------------- #
# Informe de newman
# --------------------------------------------------------------------------- #
def parse_newman_json(report: Dict[str, Any]) -> Dict[str, Any]:
    """Consolida el JSON de `newman --reporters json`."""
    run = report.get("run") or {}
    stats = run.get("stats") or {}
    timings = run.get("timings") or {}

    def counts(key: str) -> Dict[str, int]:
        node = stats.get(key) or {}
        return {
            "total": node.get("total", 0),
            "pending": node.get("pending", 0),
            "failed": node.get("failed", 0),
        }

    executions: List[Dict[str, Any]] = []
    for ex in run.get("executions") or []:
        item = ex.get("item") or {}
        resp = ex.get("response") or {}
        req = ex.get("request") or {}
        url = req.get("url")
        if isinstance(url, dict):
            url = _url_to_string(url)
        asserts = []
        for a in ex.get("assertions") or []:
            err = a.get("error") or {}
            asserts.append(
                {
                    "name": a.get("assertion"),
                    "passed": not a.get("error"),
                    "error": err.get("message"),
                }
            )
        executions.append(
            {
                "name": item.get("name"),
                "method": req.get("method"),
                "url": url,
                "statusCode": resp.get("code"),
                "statusText": resp.get("status"),
                "responseTimeMs": resp.get("responseTime"),
                "responseSizeBytes": resp.get("responseSize"),
                "assertions": asserts,
            }
        )

    failures = []
    for f in run.get("failures") or []:
        err = f.get("error") or {}
        src = f.get("source") or {}
        failures.append(
            {
                "request": src.get("name"),
                "assertion": err.get("test") or err.get("name"),
                "message": err.get("message"),
            }
        )

    return {
        "requests": counts("requests"),
        "assertions": counts("assertions"),
        "testScripts": counts("testScripts"),
        "iterations": counts("iterations"),
        "totalTimeMs": timings.get("completed", 0) - timings.get("started", 0)
        if timings.get("completed")
        else None,
        "avgResponseTimeMs": round(timings["responseAverage"], 2)
        if timings.get("responseAverage")
        else None,
        "executions": executions,
        "failures": failures,
    }

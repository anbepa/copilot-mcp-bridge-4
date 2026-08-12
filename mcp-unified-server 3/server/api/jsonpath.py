"""
Motor JSONPath minimalista y sin dependencias externas.

Cubre el subconjunto que se usa en el 99 % de las validaciones de API:

    $.data.user.name          acceso por clave
    data.user.name            el '$' inicial es opcional
    $['data']['user']         notación con corchetes y comillas
    $.items[0].id             índice de array
    $.items[-1].id            índice negativo
    $.items[*].id             comodín sobre array
    $.items[1:3]              slice
    $.*                       comodín sobre objeto
    $..id                     descenso recursivo
    $.items[?(@.active==true)]        filtro simple
    $.items[?(@.price>100)]           filtro con comparador

Devuelve SIEMPRE una lista de coincidencias (vacía si no hay ninguna), de
forma que quien lo usa decide si exige 1, N o 0 resultados.
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

__all__ = ["jsonpath", "jsonpath_first", "JsonPathError"]


class JsonPathError(ValueError):
    """Expresión JSONPath malformada."""


# --------------------------------------------------------------------------- #
# Tokenizador
# --------------------------------------------------------------------------- #
# Cada token es una tupla (tipo, valor):
#   ("key", "nombre")      -> descender por clave
#   ("index", 3)           -> índice de array
#   ("slice", (a, b, c))   -> slice de array
#   ("wildcard", None)     -> todos los hijos
#   ("recursive", None)    -> descenso recursivo (aplica al siguiente token)
#   ("filter", expr)       -> filtro ?(...)

_FILTER_RE = re.compile(
    r"^@\.?(?P<field>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"\s*(?P<op>==|!=|>=|<=|>|<|=~)?\s*(?P<value>.*)$"
)


def _tokenize(expr: str) -> List[Tuple[str, Any]]:
    if not isinstance(expr, str) or not expr.strip():
        raise JsonPathError("La expresión JSONPath no puede estar vacía.")

    s = expr.strip()
    if s.startswith("$"):
        s = s[1:]

    tokens: List[Tuple[str, Any]] = []
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]

        # --- descenso recursivo '..' -------------------------------------- #
        if s.startswith("..", i):
            tokens.append(("recursive", None))
            i += 2
            continue

        # --- separador '.' ------------------------------------------------ #
        if ch == ".":
            i += 1
            continue

        # --- corchetes ---------------------------------------------------- #
        if ch == "[":
            close = _find_closing_bracket(s, i)
            inner = s[i + 1 : close].strip()
            i = close + 1

            if inner == "*":
                tokens.append(("wildcard", None))
            elif inner.startswith("?"):
                # ?(expr)  ó  ?expr
                f = inner[1:].strip()
                if f.startswith("(") and f.endswith(")"):
                    f = f[1:-1].strip()
                tokens.append(("filter", f))
            elif (inner.startswith("'") and inner.endswith("'")) or (
                inner.startswith('"') and inner.endswith('"')
            ):
                tokens.append(("key", inner[1:-1]))
            elif ":" in inner:
                parts = inner.split(":")
                if len(parts) > 3:
                    raise JsonPathError(f"Slice inválido: '[{inner}]'")
                vals: List[Any] = []
                for p in parts:
                    p = p.strip()
                    vals.append(int(p) if p else None)
                while len(vals) < 3:
                    vals.append(None)
                tokens.append(("slice", tuple(vals)))
            else:
                try:
                    tokens.append(("index", int(inner)))
                except ValueError:
                    # [clave] sin comillas
                    tokens.append(("key", inner))
            continue

        # --- comodín suelto '.*' ------------------------------------------ #
        if ch == "*":
            tokens.append(("wildcard", None))
            i += 1
            continue

        # --- clave simple -------------------------------------------------- #
        j = i
        while j < n and s[j] not in ".[":
            j += 1
        key = s[i:j].strip()
        if key:
            tokens.append(("key", key))
        i = j

    return tokens


def _find_closing_bracket(s: str, start: int) -> int:
    """Localiza el ']' que cierra el '[' en `start`, respetando comillas."""
    depth = 0
    quote: str | None = None
    for k in range(start, len(s)):
        c = s[k]
        if quote:
            if c == quote and s[k - 1] != "\\":
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return k
    raise JsonPathError(f"Falta cerrar '[' en la expresión: {s!r}")


# --------------------------------------------------------------------------- #
# Evaluación
# --------------------------------------------------------------------------- #
def _descend_all(node: Any) -> List[Any]:
    """Todos los descendientes (incluido el propio nodo) para '..'."""
    out = [node]
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for v in cur.values():
                out.append(v)
                stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                out.append(v)
                stack.append(v)
    return out


def _coerce_literal(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        return raw[1:-1]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _get_field(node: Any, dotted: str) -> Any:
    cur = node
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"

    def __bool__(self) -> bool:
        return False


_MISSING = _Missing()


def _eval_filter(node: Any, expr: str) -> bool:
    m = _FILTER_RE.match(expr.strip())
    if not m:
        return False
    field = m.group("field")
    op = m.group("op")
    value = _get_field(node, field)

    if op is None:
        # ?(@.campo) -> el campo existe y es "truthy"
        return value is not _MISSING and value not in (None, False, "", 0, [], {})

    if value is _MISSING:
        return op == "!="

    expected = _coerce_literal(m.group("value"))

    try:
        if op == "==":
            return value == expected
        if op == "!=":
            return value != expected
        if op == "=~":
            return bool(re.search(str(expected), str(value)))
        if op == ">":
            return float(value) > float(expected)
        if op == "<":
            return float(value) < float(expected)
        if op == ">=":
            return float(value) >= float(expected)
        if op == "<=":
            return float(value) <= float(expected)
    except (TypeError, ValueError):
        return False
    return False


def jsonpath(data: Any, expr: str) -> List[Any]:
    """Evalúa `expr` sobre `data` y devuelve la lista de coincidencias."""
    tokens = _tokenize(expr)
    current: List[Any] = [data]
    recursive = False

    for kind, value in tokens:
        if kind == "recursive":
            recursive = True
            continue

        pool: List[Any] = []
        for node in current:
            pool.extend(_descend_all(node) if recursive else [node])
        recursive = False

        nxt: List[Any] = []
        for node in pool:
            if kind == "key":
                if isinstance(node, dict) and value in node:
                    nxt.append(node[value])
            elif kind == "index":
                if isinstance(node, list):
                    idx = value if value >= 0 else len(node) + value
                    if 0 <= idx < len(node):
                        nxt.append(node[idx])
            elif kind == "slice":
                if isinstance(node, list):
                    nxt.extend(node[slice(*value)])
            elif kind == "wildcard":
                if isinstance(node, dict):
                    nxt.extend(node.values())
                elif isinstance(node, list):
                    nxt.extend(node)
            elif kind == "filter":
                if isinstance(node, list):
                    nxt.extend(x for x in node if _eval_filter(x, value))
                elif isinstance(node, dict):
                    if _eval_filter(node, value):
                        nxt.append(node)
        current = nxt

    if recursive:  # la expresión termina en '..'
        pool = []
        for node in current:
            pool.extend(_descend_all(node))
        current = pool

    return current


def jsonpath_first(data: Any, expr: str, default: Any = _MISSING) -> Any:
    """Primera coincidencia, o `default`. Si no hay default, lanza KeyError."""
    hits = jsonpath(data, expr)
    if hits:
        return hits[0]
    if isinstance(default, _Missing):
        raise KeyError(f"JSONPath sin coincidencias: {expr}")
    return default


def pretty(value: Any, limit: int = 300) -> str:
    """Representación compacta de un valor para mensajes de error."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"

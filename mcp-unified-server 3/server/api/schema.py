"""
Validador de JSON Schema (Draft-07) sin dependencias externas.

Implementa las palabras clave que se usan en validación de contratos de API:

  Estructura : type, properties, required, additionalProperties,
               patternProperties, items, additionalItems, propertyNames
  Composición: allOf, anyOf, oneOf, not, if/then/else
  Valores    : enum, const
  Números    : minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf
  Cadenas    : minLength, maxLength, pattern, format
  Arrays     : minItems, maxItems, uniqueItems, contains
  Objetos    : minProperties, maxProperties, dependentRequired
  Referencias: $ref hacia '#', '#/definitions/...' y '#/$defs/...'

Devuelve una lista de errores con la ruta exacta (`instancePath`) de cada
incumplimiento, en lugar de abortar en el primero.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

__all__ = ["validate_schema", "SchemaError"]


class SchemaError(ValueError):
    """El propio esquema es inválido (no el dato)."""


_FORMATS: Dict[str, re.Pattern[str]] = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "uri": re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:\S*$"),
    "url": re.compile(r"^https?://\S+$"),
    "uuid": re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "time": re.compile(r"^\d{2}:\d{2}:\d{2}"),
    "date-time": re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"),
    "ipv4": re.compile(r"^(\d{1,3}\.){3}\d{1,3}$"),
    "hostname": re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$"),
}


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_matches(value: Any, expected: str) -> bool:
    actual = _type_of(value)
    if expected == "number":
        return actual in ("integer", "number")
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class _Validator:
    def __init__(self, root: Any) -> None:
        self.root = root
        self.errors: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def fail(self, path: str, keyword: str, message: str) -> None:
        self.errors.append(
            {"instancePath": path or "$", "keyword": keyword, "message": message}
        )

    def resolve_ref(self, ref: str) -> Any:
        if ref == "#":
            return self.root
        if not ref.startswith("#/"):
            raise SchemaError(
                f"Sólo se soportan $ref internos ('#/...'); recibido: {ref!r}"
            )
        node = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit():
                node = node[int(part)]
            else:
                raise SchemaError(f"$ref no resoluble: {ref!r}")
        return node

    # ------------------------------------------------------------------ #
    def validate(self, data: Any, schema: Any, path: str = "$") -> None:
        # Esquemas booleanos (Draft-06+)
        if schema is True:
            return
        if schema is False:
            self.fail(path, "false", "el esquema prohíbe cualquier valor aquí")
            return
        if not isinstance(schema, dict):
            raise SchemaError(
                f"Un esquema debe ser objeto o booleano; en {path} llegó "
                f"{type(schema).__name__}"
            )

        if "$ref" in schema:
            self.validate(data, self.resolve_ref(schema["$ref"]), path)
            # Draft-07 ignora el resto de claves junto a $ref
            return

        self._check_type(data, schema, path)
        self._check_enum_const(data, schema, path)
        self._check_composition(data, schema, path)

        kind = _type_of(data)
        if kind in ("integer", "number"):
            self._check_number(data, schema, path)
        if kind == "string":
            self._check_string(data, schema, path)
        if kind == "array":
            self._check_array(data, schema, path)
        if kind == "object":
            self._check_object(data, schema, path)

    # ------------------------------------------------------------------ #
    def _check_type(self, data: Any, schema: Dict[str, Any], path: str) -> None:
        if "type" not in schema:
            return
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        # Soporte de 'nullable' estilo OpenAPI 3.0
        if schema.get("nullable") is True and "null" not in options:
            options = list(options) + ["null"]
        if not any(_type_matches(data, o) for o in options):
            self.fail(
                path,
                "type",
                f"se esperaba tipo {'|'.join(options)} pero llegó "
                f"{_type_of(data)} ({_short(data)})",
            )

    def _check_enum_const(self, data: Any, schema: Dict[str, Any], path: str) -> None:
        if "enum" in schema:
            allowed = schema["enum"]
            if not isinstance(allowed, list):
                raise SchemaError(f"'enum' debe ser un array (en {path})")
            if not any(_canonical(data) == _canonical(a) for a in allowed):
                self.fail(
                    path,
                    "enum",
                    f"{_short(data)} no está en enum {_short(allowed)}",
                )
        if "const" in schema:
            if _canonical(data) != _canonical(schema["const"]):
                self.fail(
                    path,
                    "const",
                    f"se esperaba exactamente {_short(schema['const'])}, "
                    f"llegó {_short(data)}",
                )

    def _check_composition(self, data: Any, schema: Dict[str, Any], path: str) -> None:
        if "allOf" in schema:
            for i, sub in enumerate(schema["allOf"]):
                before = len(self.errors)
                self.validate(data, sub, path)
                if len(self.errors) > before:
                    # se conservan los errores concretos; sólo se contextualiza
                    self.errors[before]["message"] = (
                        f"allOf[{i}]: " + self.errors[before]["message"]
                    )

        if "anyOf" in schema:
            if not any(self._matches(data, sub, path) for sub in schema["anyOf"]):
                self.fail(path, "anyOf", "no cumple ninguna de las alternativas de anyOf")

        if "oneOf" in schema:
            hits = sum(1 for sub in schema["oneOf"] if self._matches(data, sub, path))
            if hits != 1:
                self.fail(
                    path,
                    "oneOf",
                    f"debe cumplir exactamente 1 alternativa de oneOf, cumple {hits}",
                )

        if "not" in schema:
            if self._matches(data, schema["not"], path):
                self.fail(path, "not", "cumple un esquema que estaba prohibido ('not')")

        if "if" in schema:
            branch = "then" if self._matches(data, schema["if"], path) else "else"
            if branch in schema:
                self.validate(data, schema[branch], path)

    def _matches(self, data: Any, schema: Any, path: str) -> bool:
        probe = _Validator(self.root)
        try:
            probe.validate(data, schema, path)
        except SchemaError:
            raise
        return not probe.errors

    # ------------------------------------------------------------------ #
    def _check_number(self, data: Any, schema: Dict[str, Any], path: str) -> None:
        if "minimum" in schema and data < schema["minimum"]:
            self.fail(path, "minimum", f"{data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            self.fail(path, "maximum", f"{data} > maximum {schema['maximum']}")
        exmin = schema.get("exclusiveMinimum")
        if isinstance(exmin, (int, float)) and not isinstance(exmin, bool):
            if data <= exmin:
                self.fail(path, "exclusiveMinimum", f"{data} <= exclusiveMinimum {exmin}")
        exmax = schema.get("exclusiveMaximum")
        if isinstance(exmax, (int, float)) and not isinstance(exmax, bool):
            if data >= exmax:
                self.fail(path, "exclusiveMaximum", f"{data} >= exclusiveMaximum {exmax}")
        mult = schema.get("multipleOf")
        if isinstance(mult, (int, float)) and mult > 0:
            ratio = data / mult
            if abs(ratio - round(ratio)) > 1e-9:
                self.fail(path, "multipleOf", f"{data} no es múltiplo de {mult}")

    def _check_string(self, data: str, schema: Dict[str, Any], path: str) -> None:
        if "minLength" in schema and len(data) < schema["minLength"]:
            self.fail(
                path,
                "minLength",
                f"longitud {len(data)} < minLength {schema['minLength']}",
            )
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            self.fail(
                path,
                "maxLength",
                f"longitud {len(data)} > maxLength {schema['maxLength']}",
            )
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], data):
                    self.fail(
                        path,
                        "pattern",
                        f"{_short(data)} no casa con /{schema['pattern']}/",
                    )
            except re.error as exc:
                raise SchemaError(f"pattern inválido en {path}: {exc}") from exc
        fmt = schema.get("format")
        if fmt and fmt in _FORMATS and not _FORMATS[fmt].match(data):
            self.fail(path, "format", f"{_short(data)} no tiene formato '{fmt}'")

    def _check_array(self, data: List[Any], schema: Dict[str, Any], path: str) -> None:
        if "minItems" in schema and len(data) < schema["minItems"]:
            self.fail(
                path, "minItems", f"{len(data)} elementos < minItems {schema['minItems']}"
            )
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            self.fail(
                path, "maxItems", f"{len(data)} elementos > maxItems {schema['maxItems']}"
            )
        if schema.get("uniqueItems") is True:
            seen = {_canonical(x) for x in data}
            if len(seen) != len(data):
                self.fail(path, "uniqueItems", "el array contiene elementos repetidos")

        items = schema.get("items")
        if isinstance(items, list):  # tupla posicional
            for i, sub in enumerate(items):
                if i < len(data):
                    self.validate(data[i], sub, f"{path}[{i}]")
            extra = schema.get("additionalItems")
            if extra is False and len(data) > len(items):
                self.fail(
                    path,
                    "additionalItems",
                    f"el array tiene {len(data)} elementos y sólo se permiten {len(items)}",
                )
            elif isinstance(extra, dict):
                for i in range(len(items), len(data)):
                    self.validate(data[i], extra, f"{path}[{i}]")
        elif items is not None:
            for i, item in enumerate(data):
                self.validate(item, items, f"{path}[{i}]")

        if "contains" in schema:
            if not any(self._matches(x, schema["contains"], path) for x in data):
                self.fail(
                    path, "contains", "ningún elemento cumple el esquema 'contains'"
                )

    def _check_object(self, data: Dict[str, Any], schema: Dict[str, Any], path: str) -> None:
        props = schema.get("properties") or {}
        pattern_props = schema.get("patternProperties") or {}

        for name in schema.get("required", []) or []:
            if name not in data:
                self.fail(path, "required", f"falta la propiedad obligatoria '{name}'")

        if "minProperties" in schema and len(data) < schema["minProperties"]:
            self.fail(
                path,
                "minProperties",
                f"{len(data)} propiedades < minProperties {schema['minProperties']}",
            )
        if "maxProperties" in schema and len(data) > schema["maxProperties"]:
            self.fail(
                path,
                "maxProperties",
                f"{len(data)} propiedades > maxProperties {schema['maxProperties']}",
            )

        for name, sub in props.items():
            if name in data:
                self.validate(data[name], sub, f"{path}.{name}")

        matched_by_pattern = set()
        for pat, sub in pattern_props.items():
            try:
                rx = re.compile(pat)
            except re.error as exc:
                raise SchemaError(f"patternProperties inválido {pat!r}: {exc}") from exc
            for name, value in data.items():
                if rx.search(name):
                    matched_by_pattern.add(name)
                    self.validate(value, sub, f"{path}.{name}")

        extra = schema.get("additionalProperties")
        if extra is not None and extra is not True:
            unknown = [
                k for k in data if k not in props and k not in matched_by_pattern
            ]
            if extra is False:
                for k in unknown:
                    self.fail(
                        path,
                        "additionalProperties",
                        f"propiedad no permitida: '{k}'",
                    )
            elif isinstance(extra, dict):
                for k in unknown:
                    self.validate(data[k], extra, f"{path}.{k}")

        if "propertyNames" in schema:
            for k in data:
                self.validate(k, schema["propertyNames"], f"{path}.{k} (nombre)")

        for prop, needed in (schema.get("dependentRequired") or {}).items():
            if prop in data:
                for dep in needed:
                    if dep not in data:
                        self.fail(
                            path,
                            "dependentRequired",
                            f"'{prop}' exige también '{dep}'",
                        )


def _short(value: Any, limit: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_schema(data: Any, schema: Any) -> List[Dict[str, Any]]:
    """
    Valida `data` contra `schema`.

    Devuelve la lista de errores (vacía = válido).
    Lanza SchemaError si el esquema en sí está mal formado.
    """
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"El esquema no es JSON válido: {exc}") from exc
    v = _Validator(schema)
    v.validate(data, schema, "$")
    return v.errors

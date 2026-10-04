"""Hand-rolled JSON-Schema-subset validator for tool parameters.

Supported keywords: ``type`` (string or list of strings), ``properties``,
``required``, ``enum``, ``const``, ``items``, ``additionalProperties`` (bool or
schema), ``minimum``/``maximum`` (plus ``exclusiveMinimum``/``exclusiveMaximum``),
``minItems``/``maxItems``, ``minLength``/``maxLength``, ``pattern``.

Not supported on purpose (a tool schema that needs these is over-specified for a
small model): ``$ref``, ``allOf``/``anyOf``/``oneOf``/``not``, ``format``,
``prefixItems``, ``dependentSchemas``.

The trap this module exists to get right: ``isinstance(True, int)`` is ``True`` in
Python, so a naive ``integer`` check accepts booleans.  JSON Schema does not
consider ``true`` a number, and a model that sends ``{"timeout_s": true}`` should get
a validation error back rather than a silently-instant timeout.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from .errors import ValidationError

_JSON_TYPES: Dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # bool is a subclass of int; JSON Schema does not treat `true` as a number.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}

SUPPORTED_TYPES = tuple(sorted(_JSON_TYPES))


def is_number(value: Any) -> bool:
    """True for int/float but never for bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_integer(value: Any) -> bool:
    """True for int but never for bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def type_name(value: Any) -> str:
    """JSON-ish name for a Python value, used in error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return "{}[{}]".format(path, key)
    return "{}.{}".format(path, key) if path else str(key)


def _quote(value: Any, limit: int = 80) -> str:
    try:
        rendered = json.dumps(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        rendered = repr(value)
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


def validate(value: Any, schema: Mapping[str, Any], path: str = "$") -> List[str]:
    """Return a list of human-readable violations; an empty list means valid."""
    errors: List[str] = []
    _validate(value, schema, path, errors)
    return errors


def _validate(value: Any, schema: Mapping[str, Any], path: str, errors: List[str]) -> None:
    if not isinstance(schema, Mapping):
        return

    if "const" in schema and value != schema["const"]:
        errors.append("{}: expected {}, got {}".format(path, _quote(schema["const"]), _quote(value)))

    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, Sequence) and not isinstance(allowed, str):
            if not any(value == candidate and type(value) is type(candidate) for candidate in allowed):
                rendered = ", ".join(_quote(v) for v in allowed)
                errors.append("{}: {} is not one of [{}]".format(path, _quote(value), rendered))

    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        unknown = [n for n in names if n not in _JSON_TYPES]
        if unknown:
            errors.append("{}: schema declares unknown type(s) {}".format(path, unknown))
        elif not any(_JSON_TYPES[n](value) for n in names):
            errors.append(
                "{}: expected {}, got {} ({})".format(path, "|".join(names), type_name(value), _quote(value))
            )
            return  # a type mismatch makes every remaining keyword check noise

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("{}: string shorter than minLength={}".format(path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("{}: string longer than maxLength={}".format(path, schema["maxLength"]))
        if "pattern" in schema:
            try:
                if re.search(str(schema["pattern"]), value) is None:
                    errors.append("{}: does not match pattern {}".format(path, _quote(schema["pattern"])))
            except re.error as exc:
                errors.append("{}: schema pattern is invalid: {}".format(path, exc))

    if is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("{}: {} < minimum {}".format(path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("{}: {} > maximum {}".format(path, value, schema["maximum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append("{}: {} <= exclusiveMinimum {}".format(path, value, schema["exclusiveMinimum"]))
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append("{}: {} >= exclusiveMaximum {}".format(path, value, schema["exclusiveMaximum"]))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("{}: needs at least minItems={}".format(path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("{}: has more than maxItems={}".format(path, schema["maxItems"]))
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, _join(path, index), errors)

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append("{}: required property is missing".format(_join(path, name)))
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], _join(path, name), errors)
            elif additional is False:
                errors.append("{}: additional properties are not allowed".format(_join(path, name)))
            elif isinstance(additional, Mapping):
                _validate(item, additional, _join(path, name), errors)


def assert_valid(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Raise :class:`ValidationError` listing the violations when invalid."""
    errors = validate(value, schema, path)
    if errors:
        raise ValidationError("invalid arguments: " + "; ".join(errors[:8]))


def normalize(value: Any, schema: Mapping[str, Any], path: str = "$") -> Any:
    """Coerce then validate.  Raises :class:`ValidationError` when still invalid."""
    coerced = coerce(value, schema, path)
    assert_valid(coerced, schema, path)
    return coerced


def coerce(value: Any, schema: Mapping[str, Any], path: str = "$") -> Any:
    """Coerce the two argument mistakes models reliably make.  Never validates.

    * a JSON-encoded string where an array/object is declared (``"[]"`` for ``[]``);
    * ``"30"`` where an integer is declared.

    Kept deliberately tiny: every extra coercion is a silent behaviour change that is
    harder to debug than the validation error it replaced.
    """
    return _coerce(value, schema, path)


def _coerce(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    if not isinstance(schema, Mapping):
        return value
    declared = schema.get("type")
    names: Iterable[str] = [declared] if isinstance(declared, str) else (declared or ())
    names = list(names)

    if isinstance(value, str) and any(n in ("array", "object") for n in names):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if decoded is not None and not validate(decoded, schema, path):
            return _coerce(decoded, schema, path)
        return value

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {
            key: _coerce(item, properties[key], _join(path, key)) if key in properties else item
            for key, item in value.items()
        }

    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        return [_coerce(item, schema["items"], _join(path, i)) for i, item in enumerate(value)]

    if isinstance(value, str) and names and all(n == "integer" for n in names):
        try:
            return int(value)
        except ValueError:
            return value

    if isinstance(value, str) and names and all(n == "number" for n in names):
        try:
            return float(value)
        except ValueError:
            return value

    if isinstance(value, bool) and names and all(n in ("string",) for n in names):
        return value  # never stringify a bool: "false" would read as truthy

    return value


def summarize(schema: Mapping[str, Any], limit: int = 240) -> str:
    """One-line rendering of a tool schema, for validation error messages."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = []
    for name, spec in properties.items():
        kind = spec.get("type", "any") if isinstance(spec, Mapping) else "any"
        if isinstance(kind, list):
            kind = "|".join(str(k) for k in kind)
        mark = "" if name in required else "?"
        parts.append("{}{}:{}".format(name, mark, kind))
    rendered = "{" + ", ".join(parts) + "}"
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."

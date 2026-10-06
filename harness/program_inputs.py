"""Validated, ephemeral inputs for registered programs.

Values collected for one run are never written into the program library or session log.
On Pyto, file and folder values come from the system document picker so the user grants
access explicitly; desktop terminal runs can enter an existing path.
"""

from __future__ import annotations

import importlib
import math
import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

MAX_INPUT_FIELDS = 8
MAX_INPUT_NAME_CHARS = 48
MAX_INPUT_LABEL_CHARS = 100
MAX_TEXT_CHARS = 4000
MAX_CHOICE_COUNT = 20
MAX_CHOICE_CHARS = 100
INPUT_TYPES = frozenset(("text", "number", "choice", "file", "folder"))
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_EXTENSION_RE = re.compile(r"^\.[a-zA-Z0-9][a-zA-Z0-9._+-]{0,19}$")


class ProgramInputError(ValueError):
    """A saved program input schema or value is invalid."""


class InputsCancelled(Exception):
    """The user dismissed a form or file picker before starting the program."""


def normalize_schema(value: Any) -> List[Dict[str, Any]]:
    """Validate and normalize the small JSON schema supported by saved programs."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_INPUT_FIELDS:
        raise ProgramInputError("input_schema must be a list of at most {} fields".format(MAX_INPUT_FIELDS))

    result: List[Dict[str, Any]] = []
    seen = set()
    common = {"name", "label", "type", "required", "default"}
    type_fields = {
        "text": {"max_length"},
        "number": {"minimum", "maximum", "integer"},
        "choice": {"choices"},
        "file": {"extensions"},
        "folder": set(),
    }
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ProgramInputError("input_schema field {} must be an object".format(index + 1))
        field = dict(raw)
        kind = field.get("type")
        if kind not in INPUT_TYPES:
            raise ProgramInputError("input_schema field {} type must be one of: {}".format(index + 1, ", ".join(sorted(INPUT_TYPES))))
        unknown = set(field) - common - type_fields[kind]
        if unknown:
            raise ProgramInputError("input_schema field {} has unsupported key(s): {}".format(index + 1, ", ".join(sorted(unknown))))

        name = field.get("name")
        if not isinstance(name, str) or len(name) > MAX_INPUT_NAME_CHARS or not _NAME_RE.fullmatch(name):
            raise ProgramInputError("input_schema field {} name must start with a letter and contain only lowercase letters, digits, or underscores".format(index + 1))
        if name in seen:
            raise ProgramInputError("input_schema contains duplicate field name {!r}".format(name))
        seen.add(name)

        label = field.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_INPUT_LABEL_CHARS or "\x00" in label:
            raise ProgramInputError("input_schema field {!r} needs a short, non-empty label".format(name))
        required = field.get("required", True)
        if not isinstance(required, bool):
            raise ProgramInputError("input_schema field {!r} required must be true or false".format(name))

        normalized: Dict[str, Any] = {
            "name": name,
            "label": label.strip(),
            "type": kind,
            "required": required,
        }
        if kind == "text":
            maximum = field.get("max_length", MAX_TEXT_CHARS)
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_TEXT_CHARS:
                raise ProgramInputError("input_schema field {!r} max_length must be from 1 to {}".format(name, MAX_TEXT_CHARS))
            normalized["max_length"] = maximum
        elif kind == "number":
            integer = field.get("integer", False)
            if not isinstance(integer, bool):
                raise ProgramInputError("input_schema field {!r} integer must be true or false".format(name))
            normalized["integer"] = integer
            for bound in ("minimum", "maximum"):
                if bound in field:
                    number = field[bound]
                    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
                        raise ProgramInputError("input_schema field {!r} {} must be a finite number".format(name, bound))
                    normalized[bound] = int(number) if integer and float(number).is_integer() else float(number)
            if "minimum" in normalized and "maximum" in normalized and normalized["minimum"] > normalized["maximum"]:
                raise ProgramInputError("input_schema field {!r} minimum cannot exceed maximum".format(name))
        elif kind == "choice":
            choices = field.get("choices")
            if not isinstance(choices, list) or not 2 <= len(choices) <= MAX_CHOICE_COUNT:
                raise ProgramInputError("input_schema field {!r} choices must contain 2 to {} options".format(name, MAX_CHOICE_COUNT))
            clean_choices = []
            for choice in choices:
                if not isinstance(choice, str) or not choice.strip() or len(choice.strip()) > MAX_CHOICE_CHARS or "\x00" in choice:
                    raise ProgramInputError("input_schema field {!r} choices must be short, non-empty text".format(name))
                clean_choices.append(choice.strip())
            if len({item.casefold() for item in clean_choices}) != len(clean_choices):
                raise ProgramInputError("input_schema field {!r} choices must be unique".format(name))
            normalized["choices"] = clean_choices
        elif kind == "file":
            extensions = field.get("extensions", [])
            if isinstance(extensions, str):
                extensions = [extensions]
            if not isinstance(extensions, list) or len(extensions) > 20:
                raise ProgramInputError("input_schema field {!r} extensions must be a short list".format(name))
            normalized_extensions = []
            for extension in extensions:
                if not isinstance(extension, str) or not _EXTENSION_RE.fullmatch(extension):
                    raise ProgramInputError("input_schema field {!r} has an invalid file extension".format(name))
                normalized_extensions.append(extension.lower())
            normalized["extensions"] = sorted(set(normalized_extensions))

        if "default" in field:
            if kind != "choice":
                raise ProgramInputError(
                    "input_schema field {!r} may only store a static choice default; request text, number, file and folder values each run".format(name)
                )
            normalized["default"] = _validate_value(normalized, field["default"], check_path=False)
        result.append(normalized)
    return result


def _validate_value(field: Mapping[str, Any], value: Any, *, check_path: bool) -> Any:
    name = field["name"]
    kind = field["type"]
    if kind == "text":
        if not isinstance(value, str):
            raise ProgramInputError("{} must be text".format(field["label"]))
        if "\x00" in value or len(value) > field["max_length"]:
            raise ProgramInputError("{} must be at most {} characters and contain no NUL byte".format(field["label"], field["max_length"]))
        if field["required"] and not value.strip():
            raise ProgramInputError("{} is required".format(field["label"]))
        return value
    if kind == "number":
        if isinstance(value, bool):
            raise ProgramInputError("{} must be a number".format(field["label"]))
        try:
            number = float(value) if isinstance(value, str) else float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProgramInputError("{} must be a number".format(field["label"])) from exc
        if not math.isfinite(number):
            raise ProgramInputError("{} must be a finite number".format(field["label"]))
        if field.get("integer"):
            if not number.is_integer():
                raise ProgramInputError("{} must be a whole number".format(field["label"]))
            result: Any = int(number)
        else:
            result = number
        if "minimum" in field and result < field["minimum"]:
            raise ProgramInputError("{} must be at least {}".format(field["label"], field["minimum"]))
        if "maximum" in field and result > field["maximum"]:
            raise ProgramInputError("{} must be at most {}".format(field["label"], field["maximum"]))
        return result
    if kind == "choice":
        if not isinstance(value, str) or value not in field["choices"]:
            raise ProgramInputError("{} must be one of: {}".format(field["label"], ", ".join(field["choices"])))
        return value
    if kind in ("file", "folder"):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ProgramInputError("{} needs a selected {}".format(field["label"], kind))
        path = os.path.realpath(os.path.expanduser(value.strip()))
        if check_path:
            expected = os.path.isfile(path) if kind == "file" else os.path.isdir(path)
            if not expected:
                raise ProgramInputError("{} is not an accessible {}: {}".format(field["label"], kind, value))
            access_mode = os.R_OK if kind == "file" else os.R_OK | os.X_OK
            if not os.access(path, access_mode):
                raise ProgramInputError("{} cannot be read: {}".format(field["label"], value))
            if kind == "file" and field.get("extensions"):
                suffix = os.path.splitext(path)[1].lower()
                if suffix not in field["extensions"]:
                    raise ProgramInputError("{} must use one of these extensions: {}".format(field["label"], ", ".join(field["extensions"])))
        return path
    raise ProgramInputError("unsupported input type for {}".format(name))


def validate_values(schema: Any, values: Any) -> Dict[str, Any]:
    """Validate a submitted object and return typed values for ``main(inputs)``."""
    fields = normalize_schema(schema)
    if not isinstance(values, Mapping):
        raise ProgramInputError("program inputs must be an object keyed by field name")
    allowed = {field["name"] for field in fields}
    extra = set(values) - allowed
    if extra:
        raise ProgramInputError("unknown program input(s): {}".format(", ".join(sorted(str(item) for item in extra))))
    result: Dict[str, Any] = {}
    for field in fields:
        name = field["name"]
        if name not in values:
            if "default" in field:
                value = field["default"]
            elif field["required"]:
                raise ProgramInputError("{} is required".format(field["label"]))
            else:
                result[name] = None
                continue
        else:
            value = values[name]
            if value is None and not field["required"]:
                result[name] = None
                continue
        result[name] = _validate_value(field, value, check_path=True)
    return result


def _file_system_module() -> Any:
    try:
        return importlib.import_module("file_system")
    except (ImportError, AttributeError, ValueError):
        return None


def pick_value(field: Mapping[str, Any], file_system: Any = None) -> str:
    """Open Pyto's documented file/folder picker for one field."""
    module = file_system if file_system is not None else _file_system_module()
    if module is None:
        raise ProgramInputError("the Pyto file picker is unavailable; enter an accessible path in the terminal instead")
    try:
        if field["type"] == "folder":
            selected = module.pick_directory()
        elif field["type"] == "file":
            extensions = field.get("extensions") or None
            selected = module.import_file(multiple_selection=False, file_extension=extensions)
        else:
            raise ProgramInputError("only file and folder fields use a picker")
    except Exception as exc:
        cancellation = getattr(module, "FilePickerCancellation", None)
        if (isinstance(cancellation, type) and isinstance(exc, cancellation)) or type(exc).__name__ == "FilePickerCancellation":
            raise InputsCancelled("file selection cancelled") from exc
        raise ProgramInputError("{} picker failed: {}: {}".format(field["label"], type(exc).__name__, exc)) from exc
    if isinstance(selected, (list, tuple)):
        selected = selected[0] if selected else ""
    if not isinstance(selected, str) or not selected:
        raise InputsCancelled("file selection cancelled")
    # The caller validates that the selected item still exists before running the program.
    return selected


def collect_terminal_values(
    schema: Any,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
    file_system: Any = None,
) -> Dict[str, Any]:
    """Prompt in the terminal; use Pyto's picker for paths when it is available."""
    fields = normalize_schema(schema)
    collected: Dict[str, Any] = {}
    module = file_system if file_system is not None else _file_system_module()
    for field in fields:
        name, kind, label = field["name"], field["type"], field["label"]
        if kind in ("file", "folder") and module is not None:
            try:
                collected[name] = pick_value(field, module)
            except InputsCancelled:
                raise
            except ProgramInputError as exc:
                raise ProgramInputError(str(exc)) from exc
            continue
        while True:
            hint = ""
            if kind == "choice":
                hint = " [{}]".format(" / ".join(field["choices"]))
            elif "default" in field:
                hint = " [{}]".format(field["default"])
            elif not field["required"]:
                hint = " [optional]"
            try:
                raw = input_fn("{}{}: ".format(label, hint))
            except (EOFError, KeyboardInterrupt) as exc:
                raise InputsCancelled("input collection cancelled") from exc
            if raw == "" and "default" in field:
                raw = field["default"]
            elif raw == "" and not field["required"]:
                collected[name] = None
                break
            elif kind == "choice" and raw.isdigit():
                position = int(raw) - 1
                if 0 <= position < len(field["choices"]):
                    raw = field["choices"][position]
            try:
                collected[name] = _validate_value(field, raw, check_path=True)
                break
            except ProgramInputError as exc:
                output_fn(str(exc))
    return validate_values(fields, collected)


def parse_cli_values(
    schema: Any,
    assignments: Sequence[str],
    *,
    file_system: Any = None,
    allow_picker: bool = True,
) -> Dict[str, Any]:
    """Parse ``--input name=value`` arguments.

    ``@pick`` opens Pyto's native path picker for an interactive command. Callers that
    run without an approver or visible UI can disable it and require an accessible path
    value to be supplied explicitly.
    """
    fields = normalize_schema(schema)
    by_name = {field["name"]: field for field in fields}
    collected: Dict[str, Any] = {}
    module = file_system if file_system is not None else (_file_system_module() if allow_picker else None)
    for assignment in assignments:
        if "=" not in assignment:
            raise ProgramInputError("each --input must be NAME=VALUE")
        name, raw = assignment.split("=", 1)
        if name not in by_name:
            raise ProgramInputError("unknown program input {!r}".format(name))
        if name in collected:
            raise ProgramInputError("program input {!r} was supplied more than once".format(name))
        field = by_name[name]
        if raw == "@pick" and field["type"] in ("file", "folder"):
            if not allow_picker:
                raise ProgramInputError(
                    "Shortcuts cannot open Pyto's file/folder picker; pass an accessible path with --input {}=PATH instead"
                    .format(name)
                )
            if module is None:
                raise ProgramInputError("the Pyto file picker is unavailable for {!r}".format(name))
            collected[name] = pick_value(field, module)
        else:
            collected[name] = raw
    return validate_values(fields, collected)


def schema_summary(schema: Any) -> str:
    """Compact, user-facing description of a program's form fields."""
    fields = normalize_schema(schema)
    if not fields:
        return "No inputs."
    return "Inputs: " + "; ".join(
        "{} ({}){}".format(
            field["label"],
            field["type"],
            " — required" if field["required"] else " — optional",
        )
        for field in fields
    )

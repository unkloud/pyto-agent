"""Persistent user-created tools with explicit schemas and per-run approval.

Custom tools are stored as a small manifest plus a Python program in the workspace.  The
program is executed through the same ``run_program`` path as other generated programs;
this is convenience and discoverability, not a sandbox.  A registered custom tool remains
an unknown tool to the approval policy, so every invocation asks the attached user.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ToolError
from .schema import validate as validate_values
from .security import mkdir_private, open_private
from .tools import ToolDef, ToolRegistry, ToolResult

STORE_DIR = "custom-tools"
DISABLED_DIR = "disabled"
MANIFEST_VERSION = 1
MAX_CUSTOM_TOOLS = 40
MAX_SOURCE_CHARS = 3400
MAX_PURPOSE_CHARS = 200
MAX_SCHEMA_BYTES = 12 * 1024
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
_SCHEMA_KEYS = frozenset(
    {
        "type", "properties", "required", "items", "additionalProperties", "enum", "const",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems",
        "minLength", "maxLength", "pattern", "description",
    }
)
_TYPES = frozenset({"object", "array", "string", "boolean", "integer", "number", "null"})
_RUNTIME = '''\n\nif __name__ == "__main__":
    _pyto_inputs = _pyto_json.loads(_pyto_sys.argv[1]) if len(_pyto_sys.argv) > 1 else {}
    _pyto_result = run(_pyto_inputs)
    if _pyto_result is not None:
        if isinstance(_pyto_result, str):
            print(_pyto_result)
        else:
            print(_pyto_json.dumps(_pyto_result, ensure_ascii=False, sort_keys=True))
'''


def _slug(name: str) -> str:
    if not isinstance(name, str):
        raise ToolError("name must be text")
    value = (name or "").strip().lower()
    if not NAME_RE.fullmatch(value):
        raise ToolError("name must be a short lowercase name using letters, digits and underscores")
    return value


def _one_line(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ToolError("{} must be text".format(label))
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise ToolError("{} must be a non-empty, single-line value of at most {} characters".format(label, limit))
    return text


def _schema_copy(value: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise ToolError("input schema is too large")
        result = json.loads(encoded)
    except (TypeError, ValueError):
        raise ToolError("input schema must be valid JSON") from None
    if not isinstance(result, dict) or result.get("type") != "object":
        raise ToolError("input schema must have type='object'")
    if result.get("additionalProperties", False) is not False:
        raise ToolError("input schema must set additionalProperties to false")
    result["additionalProperties"] = False
    _validate_schema_node(result, depth=0)
    return result


def _validate_schema_node(schema: Any, *, depth: int) -> None:
    if depth > 5 or not isinstance(schema, dict):
        raise ToolError("input schema objects may be nested no more than five levels")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ToolError("unsupported input schema keyword(s): {}".format(", ".join(sorted(unknown))))
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _TYPES:
        raise ToolError("input schema types must be one of {}".format(", ".join(sorted(_TYPES))))
    description = schema.get("description")
    if description is not None:
        schema["description"] = _one_line(description, "field description", 160)
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 20:
            raise ToolError("each object schema may define at most 20 named properties")
        if any(not FIELD_RE.fullmatch(str(name)) for name in properties):
            raise ToolError("input field names must start with a letter and contain only letters, digits and underscores")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(name, str) or name not in properties for name in required):
            raise ToolError("required must list names declared in properties")
        if len(set(required)) != len(required):
            raise ToolError("required must not contain duplicate names")
        for child in properties.values():
            _validate_schema_node(child, depth=depth + 1)
        if schema.get("additionalProperties", False) is not False:
            raise ToolError("additionalProperties must be false for each object")
        schema["additionalProperties"] = False
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise ToolError("properties, required and additionalProperties are valid only on object schemas")
    if kind == "array":
        if "items" not in schema:
            raise ToolError("array fields need an items schema")
        _validate_schema_node(schema["items"], depth=depth + 1)
    elif "items" in schema:
        raise ToolError("items is valid only on array schemas")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        value = schema.get(key)
        if key in schema and (
            kind not in ("integer", "number")
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or isinstance(value, float) and not math.isfinite(value)
        ):
            raise ToolError("{} needs a finite number and an integer or number field".format(key))
    for low, high in (("minimum", "maximum"), ("exclusiveMinimum", "exclusiveMaximum")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ToolError("{} cannot be greater than {}".format(low, high))
    for key in ("minLength", "maxLength"):
        value = schema.get(key)
        if key in schema and (
            kind != "string" or not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ToolError("{} needs a non-negative integer and a string field".format(key))
    if "minLength" in schema and "maxLength" in schema and schema["minLength"] > schema["maxLength"]:
        raise ToolError("minLength cannot be greater than maxLength")
    for key in ("minItems", "maxItems"):
        value = schema.get(key)
        if key in schema and (
            kind != "array" or not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ToolError("{} needs a non-negative integer and an array field".format(key))
    if "minItems" in schema and "maxItems" in schema and schema["minItems"] > schema["maxItems"]:
        raise ToolError("minItems cannot be greater than maxItems")
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values or len(values) > 50:
            raise ToolError("enum must be a non-empty list of at most 50 values")
        if any(isinstance(item, (dict, list)) for item in values):
            raise ToolError("enum values must be simple JSON values")
        enum_schema = {key: value for key, value in schema.items() if key not in ("enum", "description", "const")}
        if any(validate_values(item, enum_schema) for item in values):
            raise ToolError("enum values must satisfy the field type and its constraints")
    if "const" in schema:
        const_schema = {key: value for key, value in schema.items() if key not in ("enum", "description", "const")}
        if isinstance(schema["const"], (dict, list)) or validate_values(schema["const"], const_schema):
            raise ToolError("const must be a simple value that satisfies the field type and constraints")
    pattern = schema.get("pattern")
    if pattern is not None and kind != "string":
        raise ToolError("pattern is valid only for string fields")
    if pattern is not None:
        if not isinstance(pattern, str) or len(pattern) > 160:
            raise ToolError("pattern must be a regular expression under 160 characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ToolError("invalid input pattern: {}".format(exc)) from None


def _source_program(source: str) -> str:
    if not isinstance(source, str):
        raise ToolError("tool source must be text")
    value = source.strip()
    if not value or len(value) > MAX_SOURCE_CHARS:
        raise ToolError("tool source must be between 1 and {} characters so its creation can be reviewed".format(MAX_SOURCE_CHARS))
    try:
        tree = ast.parse(value, mode="exec", feature_version=(3, 10))
    except (SyntaxError, ValueError) as exc:
        raise ToolError("tool source must parse as Python 3.10: {}".format(exc)) from None
    run_defs = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"]
    if len(run_defs) != 1 or isinstance(run_defs[0], ast.AsyncFunctionDef):
        raise ToolError("source must define exactly one synchronous def run(inputs): function")
    definition = run_defs[0]
    if (
        len(definition.args.args) != 1
        or definition.args.args[0].arg != "inputs"
        or definition.args.defaults
        or definition.args.vararg
        or definition.args.kwarg
        or definition.args.kwonlyargs
    ):
        raise ToolError("run must have exactly one required positional parameter named inputs")
    program = "import json as _pyto_json\nimport sys as _pyto_sys\n\n" + value + _RUNTIME
    try:
        ast.parse(program, mode="exec", feature_version=(3, 10))
    except SyntaxError as exc:  # pragma: no cover - wrapper is fixed; source was checked above.
        raise ToolError("tool wrapper could not be built: {}".format(exc)) from None
    return program


def _tool_paths(workspace: Any, slug: str, *, disabled: bool = False) -> Tuple[str, str]:
    relative_root = STORE_DIR + ("/" + DISABLED_DIR if disabled else "")
    source_rel = STORE_DIR + "/" + slug + ".py"
    manifest_rel = relative_root + "/" + slug + ".json"
    return workspace.resolve(source_rel), workspace.resolve(manifest_rel)


def _write_complete(path: str, content: str) -> None:
    temporary = path + ".tmp"
    with open_private(temporary, truncate=True) as handle:
        handle.write(content)
        handle.flush()
    os.replace(temporary, path)


def create_custom_tool(
    workspace: Any,
    *,
    name: str,
    purpose: str,
    parameters: Mapping[str, Any],
    source: str,
    required_commands: Optional[Sequence[str]] = None,
    required_modules: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Persist an approved custom tool and return its verified manifest."""
    slug = _slug(name)
    purpose = _one_line(purpose, "purpose", MAX_PURPOSE_CHARS)
    schema = _schema_copy(parameters)
    program = _source_program(source)
    commands = _requirement_list(required_commands or [], "required command")
    modules = _requirement_list(required_modules or [], "required module")
    source_path, manifest_path = _tool_paths(workspace, slug)
    if os.path.exists(source_path) or os.path.exists(manifest_path):
        raise ToolError("custom tool {!r} already exists; choose a new name".format(slug))
    directory = os.path.dirname(manifest_path)
    mkdir_private(directory)
    source_hash = hashlib.sha256(program.encode("utf-8")).hexdigest()
    manifest = {
        "version": MANIFEST_VERSION,
        "name": slug,
        "purpose": purpose,
        "parameters": schema,
        "required_commands": commands,
        "required_modules": modules,
        "source": STORE_DIR + "/" + slug + ".py",
        "source_sha256": source_hash,
        "created_at": int(time.time()),
        "enabled": True,
    }
    try:
        _write_complete(source_path, program)
        _write_complete(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        for candidate in (source_path + ".tmp", manifest_path + ".tmp"):
            try:
                os.remove(candidate)
            except OSError:
                pass
        try:
            os.remove(source_path)
        except OSError:
            pass
        raise ToolError("could not save custom tool: {}".format(exc)) from None
    return manifest


def _requirement_list(values: Sequence[str], label: str) -> List[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ToolError("at most 32 {}s may be listed".format(label))
    output = []
    for value in values:
        if not isinstance(value, str):
            raise ToolError("{} names must be text".format(label))
        item = _one_line(value, label, 64)
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", item):
            raise ToolError("{} names may contain letters, numbers, dot, underscore, plus or hyphen".format(label))
        if item not in output:
            output.append(item)
    return output


def _read_manifest(workspace: Any, manifest_path: str) -> Dict[str, Any]:
    try:
        if os.path.getsize(manifest_path) > MAX_SCHEMA_BYTES * 2:
            raise ToolError("manifest is too large")
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError("could not read custom tool manifest: {}".format(exc)) from None
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("version"), int)
        or isinstance(manifest.get("version"), bool)
        or manifest.get("version") != MANIFEST_VERSION
    ):
        raise ToolError("unsupported custom tool manifest version")
    slug = _slug(manifest.get("name", ""))
    if manifest.get("source") != STORE_DIR + "/" + slug + ".py":
        raise ToolError("manifest source path does not match its tool name")
    schema = _schema_copy(manifest.get("parameters"))
    _one_line(manifest.get("purpose", ""), "purpose", MAX_PURPOSE_CHARS)
    source_path, enabled_manifest = _tool_paths(workspace, slug)
    _, disabled_manifest = _tool_paths(workspace, slug, disabled=True)
    actual_manifest = os.path.realpath(manifest_path)
    if actual_manifest not in (os.path.realpath(enabled_manifest), os.path.realpath(disabled_manifest)):
        raise ToolError("manifest path is outside the custom tool store")
    if not isinstance(manifest.get("enabled"), bool):
        raise ToolError("manifest enabled state is invalid")
    if manifest["enabled"] != (actual_manifest == os.path.realpath(enabled_manifest)):
        raise ToolError("manifest enabled state does not match its folder")
    try:
        with open(source_path, "r", encoding="utf-8") as handle:
            program = handle.read(MAX_SOURCE_CHARS * 3 + 1)
    except OSError as exc:
        raise ToolError("custom tool source is missing: {}".format(exc)) from None
    if len(program) > MAX_SOURCE_CHARS * 3:
        raise ToolError("custom tool source exceeds the size limit")
    actual = hashlib.sha256(program.encode("utf-8")).hexdigest()
    digest = manifest.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or actual != digest:
        raise ToolError("custom tool source changed after approval; create and review a new tool")
    manifest["required_commands"] = _requirement_list(manifest.get("required_commands", []), "required command")
    manifest["required_modules"] = _requirement_list(manifest.get("required_modules", []), "required module")
    manifest["parameters"] = schema
    return manifest


def _tool_definition(workspace: Any, manifest: Mapping[str, Any], run_program: Callable[..., ToolResult]) -> ToolDef:
    slug = str(manifest["name"])
    name = "custom_" + slug
    description = (
        "Run your saved custom tool {!r}. Check custom_tool_list for its purpose and inputs. "
        "It runs Python inside Pyto with the app's permissions and needs approval each time.".format(slug)
    )

    def run_custom(**arguments: Any) -> ToolResult:
        _, manifest_path = _tool_paths(workspace, slug)
        current = _read_manifest(workspace, manifest_path)
        if (
            current.get("source_sha256") != manifest.get("source_sha256")
            or current.get("parameters") != manifest.get("parameters")
        ):
            raise ToolError("custom tool source or inputs changed after it was loaded; review it before using it")
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        relative = str(manifest["source"])
        return run_program(relative, args=[encoded])

    return ToolDef(
        name=name,
        description=description,
        parameters=dict(manifest["parameters"]),
        handler=run_custom,
        timeout=None,
        resource_writes=("workspace", "pyto_process"),
        manages_execution_lane=True,
    )


def register_custom_tools(workspace: Any, registry: ToolRegistry, run_program: Callable[..., ToolResult]) -> List[str]:
    """Load intact enabled manifests. Invalid files are reported, never executed."""
    root = workspace.resolve(STORE_DIR)
    mkdir_private(root)
    errors: List[str] = []
    loaded = 0
    for filename in sorted(os.listdir(root)):
        if not filename.endswith(".json"):
            continue
        try:
            path = workspace.resolve(STORE_DIR + "/" + filename, must_exist=True)
            manifest = _read_manifest(workspace, path)
            if not manifest.get("enabled", False):
                continue
            if loaded >= MAX_CUSTOM_TOOLS:
                raise ToolError("custom tool limit ({}) reached".format(MAX_CUSTOM_TOOLS))
            registry.register(_tool_definition(workspace, manifest, run_program))
            loaded += 1
        except (ToolError, ValueError, TypeError, OverflowError, OSError) as exc:
            errors.append("{}: {}".format(filename, exc))
    return errors


def register_custom_tool(
    workspace: Any,
    registry: ToolRegistry,
    run_program: Callable[..., ToolResult],
    manifest: Mapping[str, Any],
) -> ToolDef:
    """Register a just-created manifest after re-reading its on-disk hash and schema."""
    slug = _slug(str(manifest.get("name", "")))
    tool_name = "custom_" + slug
    if tool_name in registry:
        raise ToolError("tool name {!r} is already registered".format(tool_name))
    _, manifest_path = _tool_paths(workspace, slug)
    checked = _read_manifest(workspace, manifest_path)
    tool = _tool_definition(workspace, checked, run_program)
    return registry.register(tool)


def list_custom_tools(workspace: Any) -> Dict[str, Any]:
    root = workspace.resolve(STORE_DIR)
    mkdir_private(root)
    enabled: List[Dict[str, Any]] = []
    disabled: List[Dict[str, Any]] = []
    errors: List[str] = []
    disabled_root = workspace.resolve(STORE_DIR + "/" + DISABLED_DIR)
    for is_disabled, folder, target in (
        (False, root, enabled),
        (True, disabled_root, disabled),
    ):
        if not os.path.isdir(folder):
            continue
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith(".json"):
                continue
            try:
                path = workspace.resolve(
                    STORE_DIR + ("/" + DISABLED_DIR if is_disabled else "") + "/" + filename,
                    must_exist=True,
                )
                manifest = _read_manifest(workspace, path)
                target.append(
                    {
                        "tool": "custom_" + manifest["name"],
                        "purpose": manifest["purpose"],
                        "inputs": manifest["parameters"],
                        "required_commands": manifest.get("required_commands", []),
                        "required_modules": manifest.get("required_modules", []),
                        "source": manifest["source"],
                        "source_sha256": manifest["source_sha256"],
                        "status": "disabled" if is_disabled else "enabled",
                    }
                )
            except (ToolError, ValueError, TypeError, OverflowError, OSError) as exc:
                errors.append("{}: {}".format(filename, exc))
    return {"enabled": enabled, "disabled": disabled, "invalid": errors}


def disable_custom_tool(workspace: Any, name: str, registry: ToolRegistry) -> str:
    slug = _slug(name.removeprefix("custom_"))
    _, manifest_path = _tool_paths(workspace, slug)
    manifest = _read_manifest(workspace, manifest_path)
    manifest["enabled"] = False
    disabled_dir = workspace.resolve(STORE_DIR + "/" + DISABLED_DIR)
    mkdir_private(disabled_dir)
    destination = workspace.resolve(STORE_DIR + "/" + DISABLED_DIR + "/" + slug + ".json")
    if os.path.exists(destination):
        raise ToolError("a disabled custom tool named {!r} already exists".format(slug))
    _write_complete(destination, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    try:
        os.remove(manifest_path)
    except OSError:
        try:
            os.remove(destination)
        except OSError:
            pass
        raise
    registry.unregister("custom_" + slug)
    return "Disabled custom_{}; its source was kept at {}.".format(slug, manifest["source"])


def enable_custom_tool(workspace: Any, name: str, registry: ToolRegistry, run_program: Callable[..., ToolResult]) -> str:
    slug = _slug(name.removeprefix("custom_"))
    _, disabled_manifest = _tool_paths(workspace, slug, disabled=True)
    manifest = _read_manifest(workspace, disabled_manifest)
    tool_name = "custom_" + slug
    if tool_name in registry:
        raise ToolError("tool {!r} is already registered".format(tool_name))
    _, enabled_manifest = _tool_paths(workspace, slug)
    if os.path.exists(enabled_manifest):
        raise ToolError("custom_{} is already enabled".format(slug))
    manifest["enabled"] = True
    _write_complete(enabled_manifest, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    try:
        registry.register(_tool_definition(workspace, manifest, run_program))
        os.remove(disabled_manifest)
    except Exception:
        registry.unregister(tool_name)
        os.remove(enabled_manifest)
        raise
    return "Enabled custom_{} again; it will still request approval each time it runs.".format(slug)

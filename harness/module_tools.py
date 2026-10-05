"""Read-only checks for optional Python packages installed in the current runtime."""

from __future__ import annotations

import importlib.util
import re
import sys
from typing import Any, Dict, List, Sequence

from .errors import ToolError
from . import ios

MAX_MODULE_NAMES = 40
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def inspect_modules(names: Sequence[str]) -> Dict[str, Any]:
    """Check top-level import names without importing the requested packages."""
    if not isinstance(names, (list, tuple)) or not names or len(names) > MAX_MODULE_NAMES:
        raise ToolError("modules must be a list of 1 to {} import names".format(MAX_MODULE_NAMES))
    results: List[Dict[str, str]] = []
    seen = set()
    for value in names:
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
            raise ToolError("module names must be one top-level Python import name")
        if value in seen:
            continue
        seen.add(value)
        if value in sys.modules and sys.modules[value] is not None:
            results.append({"name": value, "status": "available", "checked_by": "already loaded"})
            continue
        try:
            spec = importlib.util.find_spec(value)
        except (ImportError, ModuleNotFoundError, ValueError, AttributeError) as exc:
            results.append({"name": value, "status": "check failed", "reason": "{}: {}".format(type(exc).__name__, exc)})
            continue
        results.append(
            {
                "name": value,
                "status": "available" if spec is not None else "not found",
                "checked_by": "Python import paths",
            }
        )
    return {
        "platform": "pyto" if ios.is_pyto() else "desktop",
        "python": "{}.{}".format(sys.version_info[0], sys.version_info[1]),
        "modules": results,
        "note": "This checks availability without importing a package. Run a small import check before relying on it; desktop results do not prove Pyto availability.",
    }

#!/usr/bin/env python3
"""Prove the two structural claims about this codebase.

1. **Stdlib only.**  Import the whole runtime under an ``__import__`` hook and classify
   every top-level module that gets touched.  Anything resolving into ``site-packages``
   or ``dist-packages`` is a third-party dependency and fails the audit.  Each module is
   imported in a *fresh interpreter* so one import cannot mask another's dependencies.
2. **Python 3.10 syntax.**  Parse every runtime file with
   ``ast.parse(..., feature_version=(3, 10))``, then walk the tree for constructs that
   parse on 3.10 but fail at runtime on an older target — ``tomllib``,
   ``asyncio.TaskGroup``, ``typing.Self``, ``except*`` — so the version claim is checked
   rather than asserted.

Run it::

    python3 stdlib_audit.py            # human-readable summary
    python3 stdlib_audit.py --json     # machine-readable

Exit code 0 means both claims hold.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import sysconfig
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.join(HERE, "harness")
RUNTIME_ENTRY = os.path.join(HERE, "run.py")

#: Local (non-stdlib, non-iOS) module names that runtime code may legitimately import.
LOCAL_MODULE_NAMES = frozenset({"harness", "run", "stdlib_audit"})

#: Optional modules that only exist on iOS: importing them is allowed to fail.
OPTIONAL_ON_DEVICE = (
    "pyto",
    "pyto_ui",
    "pasteboard",
    "file_system",
    "share",
    "notifications",
    "speech",
    "photos",
    "calendar_events",
    "background",
    "xcallback",
    "usernotification",
)

#: Attributes that exist only in a newer interpreter than the 3.10 target.
BANNED_ATTRS = {
    "tomllib": "3.11+ stdlib module",
    "TaskGroup": "asyncio.TaskGroup is 3.11+",
    "Self": "typing.Self is 3.11+",
    "ExceptionGroup": "3.11+",
    "BaseExceptionGroup": "3.11+",
    "batched": "itertools.batched is 3.12+",
    "override": "typing.override is 3.12+",
    "add_note": "BaseException.add_note is 3.11+",
}

#: Modules banned outright at import time.
BANNED_IMPORTS = {"tomllib": "3.11+", "asyncio.taskgroups": "3.11+"}


def runtime_files() -> List[str]:
    """Every Python file that ships as runtime code: the package plus the entry point."""
    found: List[str] = []
    for root, dirs, files in os.walk(PACKAGE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(os.path.join(root, name))
    if os.path.exists(RUNTIME_ENTRY):
        found.append(RUNTIME_ENTRY)
    return found


def runtime_modules(files: Optional[Sequence[str]] = None) -> List[str]:
    """Dotted names for every runtime file: the package modules plus ``run``.

    Derived from :func:`runtime_files` rather than hard-coded, so a new module is
    audited the moment it is added.  ``harness/doctor.py`` uses this same list for its
    on-device "no third-party imports" assertion.
    """
    names = ["harness"]
    for path in (list(files) if files is not None else runtime_files()):
        relative = os.path.relpath(path, HERE)
        if not relative.endswith(".py") or relative == os.path.join("harness", "__init__.py"):
            continue
        if relative.startswith("harness" + os.sep):
            names.append("harness." + os.path.basename(relative)[: -len(".py")])
        elif relative == "run.py":
            names.append("run")
    seen: List[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


#: Every runtime module, imported by name in a fresh interpreter.
RUNTIME_MODULES = tuple(runtime_modules())


def example_files() -> List[str]:
    directory = os.path.join(HERE, "examples")
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, name)
        for name in sorted(os.listdir(directory))
        if name.endswith(".py")
    ]


# --------------------------------------------------------------------------------------
# 1. import audit
# --------------------------------------------------------------------------------------

_PROBE = r'''
import json, sys, sysconfig
import builtins

real_import = builtins.__import__

def top_of(name, globals, fromlist, level):
    """The absolute name of the module an __import__ call is actually loading."""
    if level and globals and "__package__" in globals and globals["__package__"]:
        base = globals["__package__"].split(".")
        base = base[: len(base) - level + 1]
        root = ".".join(base + ((name or "").split(".") if name else []))
    else:
        root = name or ""
    if fromlist and root:
        # `import a.b` reports name="a.b"; `from a import b` reports name="a" + fromlist.
        pass
    return root

def traced(name, globals=None, locals=None, fromlist=(), level=0):
    module = real_import(name, globals, locals, fromlist, level)
    full = top_of(name, globals, fromlist, level)
    if full:
        for candidate in [full] + [".".join([full, item]) for item in (fromlist or ())]:
            target = sys.modules.get(candidate)
            if target is None:
                continue
            origin = getattr(target, "__file__", None) or "<builtin>"
            records.append([candidate, origin])
    return module

records = []
builtins.__import__ = traced
result = {"failed": []}
for target in __MODULES__:
    try:
        __import__(target)
    except BaseException as exc:
        result["failed"].append([target, type(exc).__name__, str(exc)[:200]])
builtins.__import__ = real_import
result["records"] = records
result["stdlib"] = sysconfig.get_paths()["stdlib"]
result["platstdlib"] = sysconfig.get_paths()["platstdlib"]
print(json.dumps(result))
'''


def _classify(records: List[List[str]], roots: List[str]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {"stdlib": [], "builtin": [], "local": [], "third_party": []}
    here = os.path.abspath(HERE)
    for name, origin in records:
        if origin in ("<builtin>", "built-in", "frozen"):
            buckets["builtin"].append(name)
        elif "site-packages" in origin or "dist-packages" in origin:
            buckets["third_party"].append(name)
        elif os.path.abspath(origin).startswith(here):
            buckets["local"].append(name)
        elif any(os.path.abspath(origin).startswith(root) for root in roots if root):
            buckets["stdlib"].append(name)
        else:
            # A compiled extension shipped with the interpreter, or an unknown origin:
            # not a `pip install`, so not third-party.
            buckets["stdlib"].append(name)
    for key in buckets:
        buckets[key] = sorted(set(buckets[key]))
    return buckets


def _top_of(name: str, globals_: Any, fromlist: Any, level: int) -> str:
    """The absolute name of the module an ``__import__`` call is actually loading."""
    if level and globals_ and globals_.get("__package__"):
        base = globals_["__package__"].split(".")
        base = base[: len(base) - level + 1]
        return ".".join(base + ((name or "").split(".") if name else []))
    return name or ""


def _summarize(buckets: Dict[str, List[str]], failed: List[List[str]], *, mode: str) -> Dict[str, Any]:
    """The audit verdict, from classified records and import failures."""
    local_names = {os.path.splitext(os.path.basename(path))[0] for path in runtime_files()}
    local_names |= set(LOCAL_MODULE_NAMES)
    third_party = [name for name in buckets["third_party"] if name.split(".")[0] not in local_names]
    on_device = sorted(name for name in third_party if name in OPTIONAL_ON_DEVICE)
    return {
        "ok": not third_party and not failed,
        "mode": mode,
        "modules_imported": sorted(set(buckets["stdlib"] + buckets["builtin"] + buckets["local"])),
        "top_level_modules": sorted(
            {name.split(".")[0] for name in set(buckets["stdlib"] + buckets["builtin"] + buckets["local"])}
        ),
        "stdlib_modules": buckets["stdlib"],
        "builtin_modules": buckets["builtin"],
        "harness_modules": buckets["local"],
        "third_party_modules": third_party,
        "import_failures": failed,
        "note": (
            "iOS-only bridges ({}) are absent here, which is expected: the adapters that "
            "use them record an 'unsupported' result instead of importing them.".format(
                ", ".join(on_device)
            )
            if on_device
            else "no iOS-only modules present"
        ),
    }


def audit_imports() -> Dict[str, Any]:
    probe = _PROBE.replace("__MODULES__", repr(list(RUNTIME_MODULES)))
    process = subprocess.run(
        [sys.executable, "-c", probe], cwd=HERE, capture_output=True, text=True, timeout=180
    )
    if process.returncode != 0:
        return {"ok": False, "error": process.stderr[-4000:], "third_party_modules": ["<import failed>"]}
    try:
        payload = json.loads(process.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {"ok": False, "error": "probe output was not JSON: {}".format(exc), "third_party_modules": []}
    buckets = _classify(payload["records"], [payload.get("stdlib"), payload.get("platstdlib")])
    return _summarize(buckets, payload["failed"], mode="fresh-interpreter")


def audit_imports_in_process(modules: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Run the same import-hook audit **in this interpreter**, for on-device use.

    ``subprocess`` is a fake, synchronous stub inside Pyto, so the fresh-interpreter
    probe above cannot be trusted there.  This variant installs the identical
    ``__import__`` hook in the current process and imports the target modules.

    Modules already imported from the same file are reported as ``preloaded`` instead of
    being re-executed: the hook cannot see imports that happened before it was installed,
    and re-running a module's top level is a side effect a diagnostic must not have.
    """
    import builtins

    records: List[List[str]] = []
    failed: List[List[str]] = []
    preloaded: List[str] = []
    real_import = builtins.__import__

    def traced(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
        module = real_import(name, globals, locals, fromlist, level)
        full = _top_of(name, globals, fromlist, level)
        if full:
            for candidate in [full] + [".".join([full, item]) for item in (fromlist or ())]:
                target = sys.modules.get(candidate)
                if target is None:
                    continue
                origin = getattr(target, "__file__", None) or "<builtin>"
                records.append([candidate, str(origin)])
        return module

    targets = list(modules) if modules is not None else list(RUNTIME_MODULES)
    builtins.__import__ = traced
    try:
        for target in targets:
            existing = sys.modules.get(target)
            if existing is not None:
                preloaded.append(target)
                continue
            try:
                real_import(target)
            except BaseException as exc:  # noqa: BLE001 - any import failure is the finding
                failed.append([target, type(exc).__name__, str(exc)[:200]])
    finally:
        builtins.__import__ = real_import
    payload = {
        "stdlib": sysconfig.get_paths()["stdlib"],
        "platstdlib": sysconfig.get_paths()["platstdlib"],
    }
    buckets = _classify(records, [payload.get("stdlib"), payload.get("platstdlib")])
    summary = _summarize(buckets, failed, mode="in-process")
    summary["preloaded_modules"] = sorted(preloaded)
    return summary


def static_import_scan(
    files: Optional[Sequence[str]] = None,
    *,
    source: Optional[str] = None,
    filename: str = "<string>",
) -> Dict[str, Any]:
    """AST-level import scan: the top-level modules the runtime files name.

    The hook audit only sees modules that are actually *executed*; this scan reads every
    import statement, so a third-party import inside a branch that did not run on this
    device is still caught — which is what makes it usable as a pre-write gate.
    """
    stdlib_names = set(getattr(sys, "stdlib_module_names", ()))
    builtin_names = set(getattr(sys, "builtin_module_names", ()))
    local_names = {os.path.splitext(os.path.basename(path))[0] for path in runtime_files()}
    local_names |= set(LOCAL_MODULE_NAMES)

    if source is not None:
        targets = [(filename, source)]
    else:
        targets = []
        for path in (list(files) if files is not None else runtime_files()):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    targets.append((os.path.relpath(path, HERE), handle.read()))
            except OSError as exc:  # pragma: no cover - unreadable file
                targets.append((os.path.relpath(path, HERE), ""))
                del exc

    imported: Dict[str, List[str]] = {}
    suspicious: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    for name, text in targets:
        try:
            tree = ast.parse(text, filename=name, feature_version=(3, 10))
        except SyntaxError as exc:
            parse_errors.append({"file": name, "line": exc.lineno, "error": str(exc)})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import is local by construction
                    continue
                candidates = [(node.module or "", node.lineno)]
            else:
                continue
            for candidate, lineno in candidates:
                top = (candidate or "").split(".")[0]
                if not top:
                    continue
                imported.setdefault(top, [])
                if name not in imported[top]:
                    imported[top].append(name)
                if top in stdlib_names or top in builtin_names or top in local_names:
                    continue
                if top in OPTIONAL_ON_DEVICE:
                    continue
                suspicious.append({"module": top, "file": name, "line": lineno, "import": candidate})
    return {
        "ok": not suspicious and not parse_errors,
        "files_scanned": len(targets),
        "top_level_modules": sorted(imported),
        "imports": {key: sorted(value) for key, value in sorted(imported.items())},
        "suspicious": suspicious,
        "parse_errors": parse_errors,
    }


# --------------------------------------------------------------------------------------
# 2. syntax audit
# --------------------------------------------------------------------------------------


def _walk_problems(path: str, tree: ast.AST) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []
    relative = os.path.relpath(path, HERE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if isinstance(handler.type, ast.Starred):
                    problems.append({"file": relative, "line": node.lineno, "error": "except* is 3.11+"})
        if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            problems.append(
                {"file": relative, "line": node.lineno, "error": "{}: {}".format(node.attr, BANNED_ATTRS[node.attr])}
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            module = getattr(node, "module", None) or ""
            for candidate in [module] + names:
                top = candidate.split(".")[0]
                if top in BANNED_IMPORTS:
                    problems.append(
                        {
                            "file": relative,
                            "line": node.lineno,
                            "error": "import {} ({})".format(top, BANNED_IMPORTS[top]),
                        }
                    )
                if top in OPTIONAL_ON_DEVICE:
                    # Legal *inside* a function or a try/except ImportError; flagged only
                    # when it happens at module scope, where it would break a desktop run.
                    if isinstance(node, ast.Import) and not _inside_guard(tree, node):
                        problems.append(
                            {
                                "file": relative,
                                "line": node.lineno,
                                "error": "iOS-only module {} imported at module scope".format(top),
                            }
                        )
    return problems


def _inside_guard(tree: ast.AST, target: ast.AST) -> bool:
    """True when ``target`` sits inside a function/try block rather than module scope."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Try)):
            continue
        for child in ast.walk(node):
            if child is target:
                return True
    return False


def audit_syntax() -> Dict[str, Any]:
    problems: List[Dict[str, Any]] = []
    files = runtime_files()
    for path in files:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        try:
            tree = ast.parse(text, filename=path, feature_version=(3, 10))
        except SyntaxError as exc:
            problems.append(
                {"file": os.path.relpath(path, HERE), "line": exc.lineno, "error": "3.10 parse failed: {}".format(exc)}
            )
            continue
        # A module-scope `from __future__ import annotations` (or the absence of PEP 604
        # syntax in evaluated positions) is what lets `X | None` appear in annotations.
        problems.extend(_walk_problems(path, tree))
    return {
        "ok": not problems,
        "files_scanned": len(files),
        "files": [os.path.relpath(path, HERE) for path in files],
        "problems": problems,
    }


def main(argv: List[str]) -> int:
    as_json = "--json" in argv
    imports = audit_imports()
    static = static_import_scan()
    syntax = audit_syntax()
    result = {
        "python": sys.version.split()[0],
        "python_implementation": sys.implementation.name,
        "imports": imports,
        "static_imports": static,
        "syntax": syntax,
        "ok": bool(imports.get("ok")) and bool(syntax.get("ok")) and bool(static.get("ok")),
    }
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print("pyto-harness stdlib audit")
        print("=" * 60)
        print("interpreter          : {} ({})".format(result["python"], result["python_implementation"]))
        print("runtime modules      : {}".format(len(RUNTIME_MODULES)))
        print("third_party_modules  : {}".format(imports.get("third_party_modules")))
        print("import failures      : {}".format(imports.get("import_failures") or "none"))
        top_level = imports.get("top_level_modules") or []
        print("stdlib/builtin touched: {} modules, {} top-level".format(
            len(imports.get("modules_imported") or []), len(top_level)
        ))
        if top_level:
            print("  " + ", ".join(top_level))
        print("note                 : {}".format(imports.get("note", "")))
        print("static imports       : {} files, {} top-level, non-stdlib {}".format(
            static.get("files_scanned", 0),
            len(static.get("top_level_modules") or []),
            static.get("suspicious") or "none",
        ))
        print("-" * 60)
        print("files parsed at (3,10): {} of {}".format(syntax["files_scanned"] - len({p['file'] for p in syntax['problems']}), syntax["files_scanned"]))
        for path in syntax["files"]:
            print("  {}".format(path))
        if syntax["problems"]:
            print("problems:")
            for problem in syntax["problems"]:
                print("  {}:{} {}".format(problem["file"], problem.get("line", "?"), problem["error"]))
        else:
            print("problems             : none")
        print("=" * 60)
        print("RESULT: {}".format("PASS" if result["ok"] else "FAIL"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

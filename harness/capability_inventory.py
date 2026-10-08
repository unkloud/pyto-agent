"""Documentation-backed capability inventory and device evidence overrides.

The checked-in inventory describes possible directions only. Runtime evidence lives in a
separate private state file and is current only for the same non-unique device/runtime
fingerprint. Reading the global file never writes it.
"""

from __future__ import annotations

import datetime
import importlib
import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from . import ios
from .security import chmod_private, mkdir_private, open_private

GLOBAL_VERSION = 1
OVERRIDE_VERSION = 1
GLOBAL_FILENAME = "capability-inventory.json"
OVERRIDE_FILENAME = "capability-overrides.json"
MAX_OVERRIDE_BYTES = 1_000_000
MAX_ENTRIES = 200
MAX_EVIDENCE_CHARS = 2000
CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$")
STATUSES = frozenset(("verified", "unavailable", "unverified"))
DETERMINISTIC_FAILURES = frozenset((
    "ImportError", "ModuleNotFoundError", "PermissionError", "AttributeError",
))
FINGERPRINT_FIELDS = ("device_model", "ios_version", "pyto_version", "pyto_build")
_OFFICIAL_HOSTS = frozenset(("pyto.readthedocs.io", "developer.apple.com", "support.apple.com", "github.com"))


class CapabilityInventoryError(ValueError):
    """An inventory or evidence record is malformed."""


def global_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "docs", GLOBAL_FILENAME))


def override_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(os.path.expanduser(state_dir)), OVERRIDE_FILENAME)


def _validate_document_link(value: Any) -> str:
    if not isinstance(value, str):
        raise CapabilityInventoryError("documentation links must be HTTPS URLs")
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _OFFICIAL_HOSTS:
        raise CapabilityInventoryError("documentation links must use an official Pyto or Apple host")
    if parsed.hostname == "github.com" and not parsed.path.startswith("/ColdGrub1384/Pyto/"):
        raise CapabilityInventoryError("GitHub documentation links must point to Pyto's official source repository")
    return value


def validate_id(value: Any) -> str:
    if not isinstance(value, str) or not CAPABILITY_ID_RE.fullmatch(value):
        raise CapabilityInventoryError("capability IDs must use lowercase <resource>.<action> names")
    return value


def load_global(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and validate the immutable, checked-in global inventory."""
    target = path or global_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CapabilityInventoryError("could not read global capability inventory: {}".format(exc)) from exc
    if not isinstance(value, dict) or isinstance(value.get("version"), bool) or value.get("version") != GLOBAL_VERSION:
        raise CapabilityInventoryError("unsupported global capability inventory version")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ENTRIES:
        raise CapabilityInventoryError("global inventory entries must be a bounded list")
    entries = []
    ids = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise CapabilityInventoryError("global inventory entries must be objects")
        point_id = validate_id(raw.get("id"))
        description = raw.get("description")
        links = raw.get("documentation_links")
        if point_id in ids:
            raise CapabilityInventoryError("duplicate capability ID {!r}".format(point_id))
        if not isinstance(description, str) or not description or "\n" in description:
            raise CapabilityInventoryError("capability descriptions must be one line")
        if not isinstance(links, list) or not links:
            raise CapabilityInventoryError("each capability needs an official documentation link")
        links = [_validate_document_link(link) for link in links]
        if raw.get("status") not in STATUSES:
            raise CapabilityInventoryError("invalid capability status")
        if raw.get("status") != "unverified":
            raise CapabilityInventoryError("global inventory entries must remain unverified")
        if not isinstance(raw.get("evidence"), (dict, list, str)) or not raw.get("date"):
            raise CapabilityInventoryError("capability entries need evidence and a date")
        try:
            datetime.date.fromisoformat(str(raw["date"]))
        except ValueError as exc:
            raise CapabilityInventoryError("capability dates must use YYYY-MM-DD") from exc
        entries.append({
            "id": point_id,
            "description": description,
            "documentation_links": links,
            "status": "unverified",
            "evidence": raw["evidence"],
            "date": raw["date"],
        })
        ids.add(point_id)
    return {"version": GLOBAL_VERSION, "entries": entries}


def normalize_dependencies(values: Optional[Sequence[str]], inventory: Optional[Mapping[str, Any]] = None) -> List[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise CapabilityInventoryError("capability_dependencies must be a list of capability IDs")
    entries = (inventory or load_global()).get("entries", [])
    known = {entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry}
    result = []
    for value in values:
        point_id = validate_id(value)
        if point_id not in known:
            raise CapabilityInventoryError("unknown capability ID {!r}; consult the capability inventory".format(point_id))
        if point_id not in result:
            result.append(point_id)
    if len(result) > 40:
        raise CapabilityInventoryError("at most 40 capability dependencies may be declared")
    return result


def _global_entry(capability_id: str) -> Dict[str, Any]:
    for entry in load_global()["entries"]:
        if entry["id"] == capability_id:
            return entry
    raise CapabilityInventoryError("unknown capability ID {!r}".format(capability_id))


def runtime_fingerprint() -> Dict[str, str]:
    """Read non-unique device/runtime metadata where Pyto exposes it."""
    value = {
        "device_model": "unknown",
        "ios_version": "unknown",
        "pyto_version": "unknown",
        "pyto_build": "unknown",
    }
    if not ios.is_pyto():
        return value
    try:
        uikit = importlib.import_module("UIKit")
        UIDevice = getattr(uikit, "UIDevice")
        current = UIDevice.currentDevice
        device = current() if callable(current) else current
        value["device_model"] = str(device.model)
        value["ios_version"] = str(device.systemVersion)
    except Exception:
        pass
    try:
        foundation = importlib.import_module("Foundation")
        NSBundle = getattr(foundation, "NSBundle")
        current = NSBundle.mainBundle
        bundle = current() if callable(current) else current
        info = bundle.infoDictionary
        info = info() if callable(info) else info
        for key, field in (("CFBundleShortVersionString", "pyto_version"), ("CFBundleVersion", "pyto_build")):
            try:
                raw = info.objectForKey_(key)
            except Exception:
                try:
                    raw = info.get(key)
                except Exception:
                    raw = None
            if raw:
                value[field] = str(raw)
    except Exception:
        pass
    return value


def _empty_override(runtime: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    return {
        "version": OVERRIDE_VERSION,
        "device": dict(runtime or runtime_fingerprint()),
        "entries": {},
    }


def _load_override(state_dir: str) -> Dict[str, Any]:
    path = override_path(state_dir)
    if os.path.islink(path):
        raise CapabilityInventoryError("capability override is a symbolic link; it was left untouched")
    if not os.path.exists(path):
        return _empty_override()
    try:
        if os.path.getsize(path) > MAX_OVERRIDE_BYTES:
            raise CapabilityInventoryError("capability override exceeds its size limit")
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except CapabilityInventoryError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise CapabilityInventoryError("could not read capability override: {}".format(exc)) from exc
    if not isinstance(value, dict) or isinstance(value.get("version"), bool) or value.get("version") != OVERRIDE_VERSION:
        raise CapabilityInventoryError("unsupported capability override version")
    entries = value.get("entries")
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise CapabilityInventoryError("capability override entries must be an object")
    if not isinstance(value.get("device"), dict):
        raise CapabilityInventoryError("capability override needs a device fingerprint")
    for point_id, entry in entries.items():
        validate_id(point_id)
        if not isinstance(entry, dict) or entry.get("id") != point_id or entry.get("status") not in STATUSES:
            raise CapabilityInventoryError("capability override entry {!r} is malformed".format(point_id))
        if not isinstance(entry.get("description"), str) or not isinstance(entry.get("documentation_links"), list) or not entry["documentation_links"]:
            raise CapabilityInventoryError("capability override entry {!r} needs its description and documentation links".format(point_id))
        for link in entry["documentation_links"]:
            _validate_document_link(link)
        try:
            datetime.date.fromisoformat(str(entry.get("date", "")))
        except ValueError as exc:
            raise CapabilityInventoryError("capability override entry {!r} needs an ISO date".format(point_id)) from exc
        if not isinstance(entry.get("evidence"), (dict, list, str)) or not entry.get("evidence"):
            raise CapabilityInventoryError("capability override entry {!r} needs evidence".format(point_id))
        if not isinstance(entry.get("device"), dict):
            raise CapabilityInventoryError("capability override entry {!r} needs a device fingerprint".format(point_id))
    return value


def _is_fresh(entry: Mapping[str, Any], runtime: Mapping[str, str]) -> bool:
    saved = entry.get("device")
    return (
        isinstance(saved, dict)
        and all(runtime.get(key) not in (None, "", "unknown") for key in FINGERPRINT_FIELDS)
        and all(saved.get(key) == runtime.get(key) for key in FINGERPRINT_FIELDS)
    )


def effective_inventory(
    state_dir: str,
    *,
    runtime: Optional[Mapping[str, str]] = None,
    global_inventory: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return global points with only fresh device evidence applied."""
    current = dict(runtime or runtime_fingerprint())
    global_data = dict(global_inventory or load_global())
    override = _load_override(state_dir)
    points = []
    for raw in global_data.get("entries", []):
        point = dict(raw)
        saved = override["entries"].get(point["id"])
        if isinstance(saved, dict) and saved.get("id") == point["id"] and saved.get("status") in ("verified", "unavailable"):
            if _is_fresh(saved, current):
                point.update({"status": saved["status"], "evidence": saved.get("evidence"), "date": saved.get("date")})
                point["evidence_source"] = "device_override"
            else:
                point["evidence"] = {
                    "global": raw.get("evidence"),
                    "stale_device_evidence": saved.get("evidence"),
                    "stale_device_date": saved.get("date"),
                    "stale_device_fingerprint": saved.get("device"),
                }
                point["status"] = "unverified"
                point["evidence_source"] = "stale_device_override"
        else:
            point["evidence_source"] = "global_documentation"
        points.append(point)
    return {"version": GLOBAL_VERSION, "runtime": current, "entries": points}


def unavailable_dependencies(state_dir: str, capability_ids: Iterable[str]) -> List[str]:
    """Return only dependencies with fresh, target-device unavailable evidence."""
    wanted = set(normalize_dependencies(list(capability_ids)))
    if not wanted:
        return []
    inventory = effective_inventory(state_dir)
    return [point["id"] for point in inventory["entries"] if point["id"] in wanted and point["status"] == "unavailable"]


def render_prompt_context(state_dir: str, *, runtime: Optional[Mapping[str, str]] = None) -> str:
    inventory = effective_inventory(state_dir, runtime=runtime)
    rows = ["Documented capability points (metadata only; signatures are in the linked official docs):"]
    for point in inventory["entries"]:
        links = ", ".join(point["documentation_links"])
        evidence = json.dumps(point.get("evidence"), ensure_ascii=False, sort_keys=True)
        if len(evidence) > 320:
            evidence = evidence[:317] + "..."
        row = dict(point)
        row.update({"links": links, "evidence": evidence})
        rows.append("- {id} — {status}: {description} [{links}] (date: {date}; evidence: {evidence})".format(**row))
    rows.append("Declare new tool/program dependencies using these IDs. For an unverified point, consult its linked docs and test the smallest relevant step during creation. A specific unavailable point does not rule out other documented approaches.")
    return "\n".join(rows)


def _write_override(state_dir: str, override: Mapping[str, Any]) -> None:
    path = override_path(state_dir)
    if os.path.islink(path):
        raise CapabilityInventoryError("capability override is a symbolic link; it was left untouched")
    mkdir_private(os.path.dirname(path))
    serialized = json.dumps(override, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_OVERRIDE_BYTES:
        raise CapabilityInventoryError("capability override would exceed its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".capability-overrides-", dir=os.path.dirname(path))
    os.close(fd)
    try:
        with open_private(temporary, truncate=True) as handle:
            handle.write(serialized)
        os.replace(temporary, path)
        if os.name == "posix":
            chmod_private(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def initialize_override(state_dir: str, *, runtime: Optional[Mapping[str, str]] = None) -> str:
    """Create an empty versioned envelope on Pyto; never add attempted/unverified points."""
    path = override_path(state_dir)
    if not ios.is_pyto():
        return path
    if not os.path.exists(path):
        _write_override(state_dir, _empty_override(runtime or runtime_fingerprint()))
    return path


def record_unavailable(
    state_dir: str,
    capability_id: str,
    *,
    failed_step: str,
    error_type: str,
    detail: str,
    test_id: str,
    deterministic_platform_failure: bool = False,
    runtime: Optional[Mapping[str, str]] = None,
    date: Optional[str] = None,
) -> bool:
    """Persist only a deterministic failure observed on the target Pyto runtime."""
    allowed_platform_error = deterministic_platform_failure and error_type in ("RuntimeError", "OSError")
    if (
        not ios.is_pyto()
        or (error_type not in DETERMINISTIC_FAILURES and not allowed_platform_error)
        or not str(failed_step).strip()
        or not str(test_id).strip()
        or not str(detail).strip()
    ):
        return False
    point_id = validate_id(capability_id)
    normalize_dependencies([point_id])
    runtime_info = dict(runtime or runtime_fingerprint())
    if any(runtime_info.get(field) in (None, "", "unknown") for field in FINGERPRINT_FIELDS):
        return False
    failure = {
        "kind": "minimal_path_failure",
        "test_id": str(test_id)[:200],
        "failed_step": str(failed_step)[:300],
        "error_type": error_type,
        "deterministic_platform_failure": bool(allowed_platform_error),
        "detail": str(detail)[:MAX_EVIDENCE_CHARS],
    }
    override = _load_override(state_dir)
    evidence_date = date or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(evidence_date)
    except ValueError as exc:
        raise CapabilityInventoryError("evidence date must use YYYY-MM-DD") from exc
    override["device"] = runtime_info
    override["entries"][point_id] = {
        "id": point_id,
        "description": _global_entry(point_id)["description"],
        "documentation_links": _global_entry(point_id)["documentation_links"],
        "status": "unavailable",
        "evidence": failure,
        "date": evidence_date,
        "device": runtime_info,
    }
    _write_override(state_dir, override)
    return True


def record_verified(
    state_dir: str,
    capability_id: str,
    *,
    test_id: str,
    result: str,
    runtime: Optional[Mapping[str, str]] = None,
    date: Optional[str] = None,
) -> bool:
    """Persist a successful automated end-to-end result from the target Pyto app."""
    if not ios.is_pyto():
        return False
    point_id = validate_id(capability_id)
    normalize_dependencies([point_id])
    runtime_info = dict(runtime or runtime_fingerprint())
    if any(runtime_info.get(field) in (None, "", "unknown") for field in FINGERPRINT_FIELDS):
        return False
    summary = str(result).strip()
    if not test_id or not summary or len(summary) > MAX_EVIDENCE_CHARS:
        return False
    evidence_date = date or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(evidence_date)
    except ValueError as exc:
        raise CapabilityInventoryError("evidence date must use YYYY-MM-DD") from exc
    override = _load_override(state_dir)
    override["device"] = runtime_info
    override["entries"][point_id] = {
        "id": point_id,
        "description": _global_entry(point_id)["description"],
        "documentation_links": _global_entry(point_id)["documentation_links"],
        "status": "verified",
        "evidence": {"kind": "automated_end_to_end", "test_id": str(test_id)[:200], "result": summary},
        "date": evidence_date,
        "device": runtime_info,
    }
    _write_override(state_dir, override)
    return True

"""Safe, test-gated self-modification of the harness's own source.

The rule this module exists to enforce: **no edit is promoted unless the offline test
suite still passes.**  A model that wants to repair the harness hands over the complete
new source of one file; this module snapshots the tree, writes the file, runs the suite,
and either keeps the change or puts the previous bytes back — verbatim.

Guardrails, and why each one is here:

* **Path jail.**  Only ``harness/*.py`` and ``run.py`` can be written.  Absolute paths,
  ``..``, symlinks and anything under ``tests/``, ``.git/`` or a backups directory are
  refused.  ``tests/`` matters most: if an edit could weaken the tests, the gate would be
  worthless.
* **The gate cannot edit itself.**  ``harness/repair.py`` is refused outright.
* **Syntax and stdlib checks before the write.**  ``ast.parse(feature_version=(3, 10))``
  catches 3.11+ syntax, and the static import scan catches a new third-party import, which
  can never work inside Pyto.  Both are cheap; both are checked before anything is written.
* **Identical and empty sources are refused.**  A no-op edit that "passes" teaches the
  model nothing, and an empty file is a deletion.
* **Always snapshot first, always keep the previous bytes.**  The backup is a real
  directory with a ``manifest.json`` of SHA-256 hashes, so a restore can be verified.
* **A read-only or unusual location is a report, not a crash.**  :func:`can_self_repair`
  answers whether this installation can repair itself at all, and every public function
  returns a :class:`RepairResult` that says "cannot self-repair here" instead of raising.
* **No user data, no secrets.**  Snapshots contain source files only; nothing here reads
  the config, the API key or a session log.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import hmac
import json
import os
import posixpath
import shutil
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import doctor
from .config import Config
from .security import mkdir_private, open_private, write_private

#: Writable paths, relative to the harness root.  Everything else is refused.
ALLOWED_PREFIXES = ("harness/",)
ALLOWED_EXACT = ("run.py",)

#: Path components that are never writable, whatever the prefix says.
FORBIDDEN_PARTS = frozenset({".git", "backups", "__pycache__", "tests", ".hg", ".svn"})

#: The file that implements the gate.  It cannot be edited by the gate.
PROTECTED_RELATIVE = ("harness/repair.py",)

#: Largest source file the gate will accept.
MAX_SOURCE_BYTES = 512 * 1024

#: Unified diff budget in a repair result.
MAX_DIFF_CHARS = 20000

#: Environment seam used by the tests (and by anyone who wants a different gate):
#: a comma-separated list of test module names to run instead of the default subset.
TEST_MODULES_ENV = "PYTO_HARNESS_REPAIR_TEST_MODULES"

#: Set ``PYTO_HARNESS_REPAIR_GATE=full`` to gate on the whole suite instead of the
#: bounded subset.  ``run.py --repair --deep-tests`` does the same thing explicitly.
GATE_ENV = "PYTO_HARNESS_REPAIR_GATE"

#: Guards against a repair that triggers a repair inside the same process.
_ACTIVE = threading.local()

#: Appended to the system prompt for a ``run.py --repair`` turn.
REPAIR_INSTRUCTIONS = """\
You are repairing pyto-harness itself, the program you are running inside.

How to do it
- `diagnose` first when the problem is vague, and `diagnose(network=true)` when it smells like
  DNS, TLS, authentication or a model name.
- Read the file before you change it: `read_source("harness/config.py")`.
- Prefer a patch (`old_string`/`new_string`) over rewriting a whole file, and prefer the smallest
  patch that fixes the cause rather than the symptom.
- Run `selftest` before you propose the edit. After `self_edit` the same suite runs automatically:
  a green suite keeps the change, a red one restores the previous bytes and hands you the failure
  verbatim. Never weaken or delete a test to get a change through -- that is the one thing this
  gate exists to prevent.
- If the gate reverts you twice for the same reason, stop and report the failure instead of
  trying variations. `list_backups` and `restore_backup` can undo anything you did.

What is not a code problem
- A missing iOS permission, a missing entitlement, an unavailable Pyto module, a compiled
  dependency or anything that needs App Review cannot be fixed by editing source. Say which one it
  is and give the user the action from the doctor's `human_action` field.
- Never write the API key anywhere, and never add a fallback that disables TLS verification.
"""


class RepairRefused(Exception):
    """The requested repair is not allowed.  Carries a decision and a readable reason."""

    def __init__(self, decision: str, reason: str) -> None:
        super().__init__(reason)
        self.decision = decision
        self.reason = reason


@dataclass
class RepairResult:
    """Structured outcome of a snapshot / edit / restore."""

    ok: bool
    decision: str
    path: str = ""
    reason: str = ""
    diff: str = ""
    backup_id: str = ""
    tests: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    warnings: List[str] = field(default_factory=list)
    hashes: Dict[str, str] = field(default_factory=dict)
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision,
            "path": self.path,
            "reason": self.reason,
            "backup_id": self.backup_id,
            "tests": dict(self.tests),
            "error": self.error,
            "warnings": list(self.warnings),
            "hashes": dict(self.hashes),
            "diff_chars": len(self.diff),
            "text_chars": len(self.text),
        }

    def render(self, *, with_diff: bool = True) -> str:
        lines = ["{}: {} ({})".format("ok" if self.ok else "not ok", self.decision, self.path or "-")]
        if self.reason:
            lines.append("reason: {}".format(self.reason))
        if self.backup_id:
            lines.append("backup: {}".format(self.backup_id))
        if self.tests:
            lines.append(
                "tests: {ran} ran, {failures} failure(s), {errors} error(s) ({mode})".format(
                    ran=self.tests.get("ran", "?"),
                    failures=self.tests.get("failures", "?"),
                    errors=self.tests.get("errors", "?"),
                    mode=self.tests.get("mode", "?"),
                )
            )
        for warning in self.warnings:
            lines.append("warning: {}".format(warning))
        if self.error:
            lines.append("error: {}".format(self.error))
        if self.text:
            lines.append(self.text)
        if with_diff and self.diff:
            lines.append("diff:")
            lines.append(self.diff)
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Paths and locations
# --------------------------------------------------------------------------------------


def default_backups_dir() -> str:
    return os.path.join(doctor.state_dir(), "backups")


#: State directory kept beside a *copy* of the tree (a repairs root that is not the
#: installed one).  Visible, like the real state directory: no leading dot.
COPY_STATE_DIR_NAME = "pyto_harness_state"
#: What older copies used.  Read-only fallback: when only the legacy directory exists it
#: is still used, so the snapshots and the gate an older copy wrote stay listable and
#: restorable.  Nothing creates it any more.
LEGACY_COPY_STATE_DIR_NAME = ".pyto_harness_state"


def _state_for(root: str) -> str:
    """State dir for a repairs root: the real one for this install, a sibling for a copy."""
    real_root = doctor.harness_root()
    if os.path.abspath(root) == os.path.abspath(real_root):
        return doctor.state_dir()
    base = os.path.abspath(root)
    current = os.path.join(base, COPY_STATE_DIR_NAME)
    legacy = os.path.join(base, LEGACY_COPY_STATE_DIR_NAME)
    if not os.path.isdir(current) and os.path.isdir(legacy):
        # A copy made before the rename kept its state under the hidden name.  Keep using
        # it instead of stranding its backups; new copies never create it.
        return legacy
    return current


def can_self_repair(root: Optional[str] = None, backups_dir: Optional[str] = None) -> Tuple[bool, str]:
    """Can this installation rewrite its own source right now?

    Answers with a reason instead of raising, because "cannot self-repair here" is a
    normal outcome on a read-only filesystem, in a signed bundle, or under a test runner
    that copied the tree somewhere immutable.
    """
    resolved_root = os.path.abspath(root or doctor.harness_root())
    if not os.path.isdir(resolved_root):
        return False, "harness root {} does not exist".format(resolved_root)
    if not os.path.isfile(os.path.join(resolved_root, "run.py")):
        return False, "no run.py under {} -- not a harness installation".format(resolved_root)
    if not os.path.isdir(os.path.join(resolved_root, "harness")):
        return False, "no harness/ package under {}".format(resolved_root)
    for candidate in (resolved_root, os.path.join(resolved_root, "harness")):
        if not os.access(candidate, os.W_OK):
            return False, "{} is not writable".format(candidate)
    target_backups = backups_dir or _state_for(resolved_root) + "/backups"
    try:
        os.makedirs(target_backups, exist_ok=True)
        probe = os.path.join(target_backups, ".write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
    except OSError as exc:
        return False, "cannot write backups to {}: {}: {}".format(
            target_backups, type(exc).__name__, exc
        )
    return True, ""


def _validate_relative(relative_path: str, *, allow_protected: bool = False) -> str:
    """Normalise and police a relative source path.  Raises :class:`RepairRefused`.

    ``allow_protected`` is for :func:`restore` only: putting ``harness/repair.py`` *back*
    from a snapshot is legitimate, while the model editing it forward is not.
    """
    raw = (relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise RepairRefused("refused", "a relative path is required, e.g. 'harness/config.py'")
    if "\x00" in raw:
        raise RepairRefused("refused", "the path contains a NUL byte")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise RepairRefused("refused", "absolute paths are refused; give a path inside the harness root")
    normalised = posixpath.normpath(raw)
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise RepairRefused("refused", "'..' is refused: the path must stay inside the harness root")
    if any(part in FORBIDDEN_PARTS for part in parts):
        raise RepairRefused(
            "refused",
            "{} is not writable by the gate (tests, .git and backups are off limits)".format(normalised),
        )
    if normalised in PROTECTED_RELATIVE and not allow_protected:
        raise RepairRefused(
            "refused",
            "harness/repair.py implements the snapshot/restore gate and cannot be edited by that gate",
        )
    if not (normalised in ALLOWED_EXACT or any(normalised.startswith(prefix) for prefix in ALLOWED_PREFIXES)):
        raise RepairRefused(
            "refused",
            "only {} and {} may be edited".format(", ".join(ALLOWED_EXACT), "harness/*.py"),
        )
    if not normalised.endswith(".py"):
        raise RepairRefused("refused", "only .py files may be edited")
    return normalised


def _resolve_target(root: str, relative_path: str, *, allow_protected: bool = False) -> str:
    """Resolve a validated relative path, refusing symlinks anywhere along the way."""
    relative = _validate_relative(relative_path, allow_protected=allow_protected)
    real_root = os.path.realpath(root)
    target = os.path.join(real_root, *relative.split("/"))
    current = real_root
    for part in relative.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise RepairRefused("refused", "{} is a symlink; the gate will not follow it".format(current))
    resolved = os.path.realpath(target)
    if resolved != target and not resolved.startswith(real_root + os.sep):
        raise RepairRefused("refused", "{} resolves outside the harness root".format(relative))
    if resolved != target:
        raise RepairRefused("refused", "{} does not resolve to itself (symlinked parent)".format(relative))
    return target


# --------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------


def _iter_sources(root: str) -> List[str]:
    """Every file a snapshot keeps: ``harness/**/*.py`` plus ``run.py``."""
    found: List[str] = []
    package = os.path.join(root, "harness")
    for current, dirs, files in os.walk(package):
        dirs[:] = sorted(d for d in dirs if d not in FORBIDDEN_PARTS)
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(os.path.relpath(os.path.join(current, name), root).replace(os.sep, "/"))
    if os.path.isfile(os.path.join(root, "run.py")):
        found.append("run.py")
    return found


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------
# Manifest signing
# --------------------------------------------------------------------------------------
#
# A snapshot used to be an ordinary writable directory plus a manifest whose hashes were
# computed by whoever wrote it — so anything that could create a directory under
# ``backups/`` could have ``restore_backup`` write an arbitrary ``harness/*.py`` (including
# this file, which the edit gate refuses).  The manifest is now authenticated with an HMAC
# keyed by a per-install secret that lives **outside** the backup tree, at 0600:
#
#   <state>/backup.key      the key (32 random bytes, hex)
#   <state>/backups/<id>/   the signed snapshot
#
# The honest limit: on iOS the same process holds the key in memory while it runs, so a
# program running in-process could read it.  A key the process cannot reach needs the
# iOS Keychain, which this harness does not use.  What signing does buy is that a backup
# *directory* dropped into ``backups/`` by another app, a Shortcut, a sync conflict or a
# stale copy is refused instead of restored, and that any tampering with a payload is a
# hard refusal instead of a warning.

#: Manifest signature algorithm, recorded so a future change can migrate.
SIGNATURE_ALG = "hmac-sha256"
#: Name of the signing key inside the state dir (never inside a backup directory).
BACKUP_KEY_NAME = "backup.key"
#: Random key size.
BACKUP_KEY_BYTES = 32


def _backups_dir_for(root: str, backups_dir: Optional[str]) -> str:
    """The backups directory a snapshot/restore actually uses for ``root``."""
    return backups_dir or os.path.join(_state_for(root), "backups")


def backup_key_path(backups_dir: str) -> str:
    """Where the signing key lives: beside ``backups/``, never inside it."""
    return os.path.join(os.path.dirname(os.path.abspath(backups_dir)), BACKUP_KEY_NAME)


def _load_key(key_path: str, *, create: bool) -> bytes:
    """Read (or create) the per-install signing key.  Raises OSError when unusable."""
    try:
        with open(key_path, "rb") as handle:
            data = handle.read().strip()
        if len(data) >= 32:
            return data
    except OSError:
        if not create:
            raise FileNotFoundError(key_path)
    if not create:
        raise FileNotFoundError(key_path)
    mkdir_private(os.path.dirname(key_path) or ".")
    fresh = os.urandom(BACKUP_KEY_BYTES).hex().encode("ascii")
    try:
        write_private(key_path, fresh, exclusive=True)
    except FileExistsError:  # pragma: no cover - a concurrent snapshot won the race
        with open(key_path, "rb") as handle:
            return handle.read().strip()
    return fresh


def _manifest_payload(manifest: Mapping[str, Any]) -> bytes:
    """The canonical bytes the signature covers: identity + every file hash."""
    files = manifest.get("files") or {}
    canonical = {
        "version": manifest.get("version"),
        "id": manifest.get("id"),
        "label": manifest.get("label"),
        "created_at": manifest.get("created_at"),
        "root": manifest.get("root"),
        "files": {
            str(rel): {"sha256": (item or {}).get("sha256"), "size": (item or {}).get("size")}
            for rel, item in sorted(files.items())
        },
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: Dict[str, Any], backups_dir: str) -> Dict[str, Any]:
    """Attach an HMAC over the manifest's file hashes.  Creates the key if needed."""
    key = _load_key(backup_key_path(backups_dir), create=True)
    manifest["signature"] = {
        "alg": SIGNATURE_ALG,
        "value": hmac.new(key, _manifest_payload(manifest), hashlib.sha256).hexdigest(),
    }
    return manifest


def verify_manifest(manifest: Mapping[str, Any], backups_dir: str) -> str:
    """``""`` when the manifest is signed by this installation, else the reason it is not."""
    signature = manifest.get("signature")
    if not isinstance(signature, Mapping) or not signature.get("value"):
        return (
            "the snapshot manifest is not signed, so this installation cannot tell who wrote it; "
            "refusing to restore it (snapshots taken by an older build must be re-created)"
        )
    if signature.get("alg") not in (None, SIGNATURE_ALG):
        return "the snapshot manifest uses an unsupported signature ({!r})".format(signature.get("alg"))
    try:
        key = _load_key(backup_key_path(backups_dir), create=False)
    except OSError:
        return (
            "the backup signing key {!r} is missing, so the manifest cannot be verified; "
            "refusing to restore".format(backup_key_path(backups_dir))
        )
    expected = hmac.new(key, _manifest_payload(manifest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature.get("value"))):
        return (
            "the snapshot manifest signature does not match this installation's key: the manifest "
            "was modified (or written by another installation). Refusing to restore it."
        )
    return ""


def _snapshot_into(backup_dir: str, root: str, label: str, relative: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    files = list(relative) if relative is not None else _iter_sources(root)
    manifest: Dict[str, Any] = {
        "version": 1,
        "id": os.path.basename(backup_dir),
        "label": label,
        "created_at": int(time.time() * 1000),
        "root": root,
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "harness_version": _harness_version(),
        "files": {},
    }
    for rel in files:
        source = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(source):
            continue
        destination = os.path.join(backup_dir, *rel.split("/"))
        mkdir_private(os.path.dirname(destination))
        shutil.copyfile(source, destination)
        mode = stat.S_IMODE(os.stat(source).st_mode)
        try:
            os.chmod(destination, mode)
        except OSError:  # pragma: no cover - non-POSIX
            pass
        manifest["files"][rel] = {
            "sha256": _sha256(destination),
            "size": os.path.getsize(destination),
            "mode": oct(mode),
        }
    sign_manifest(manifest, os.path.dirname(backup_dir))
    write_private(
        os.path.join(backup_dir, "manifest.json"),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def _harness_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - a broken package must not stop a snapshot
        return "unknown"


def snapshot(
    label: str = "manual",
    *,
    root: Optional[str] = None,
    backups_dir: Optional[str] = None,
) -> RepairResult:
    """Copy ``harness/`` + ``run.py`` into a timestamped backup directory."""
    resolved_root = os.path.abspath(root or doctor.harness_root())
    ok, reason = can_self_repair(resolved_root, backups_dir)
    if not ok:
        return RepairResult(False, "cannot-self-repair", path=resolved_root, reason=reason)
    target_dir = backups_dir or os.path.join(_state_for(resolved_root), "backups")
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (label or "manual"))[:40].strip("-")
    backup_id = "{}-{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), safe_label or "manual", uuid.uuid4().hex[:6])
    backup_dir = os.path.join(target_dir, backup_id)
    try:
        mkdir_private(target_dir)
        os.makedirs(backup_dir, exist_ok=False)
        manifest = _snapshot_into(backup_dir, resolved_root, safe_label or "manual")
    except OSError as exc:
        return RepairResult(
            False,
            "cannot-self-repair",
            path=resolved_root,
            reason="could not write a snapshot: {}: {}".format(type(exc).__name__, exc),
        )
    return RepairResult(
        True,
        "snapshot",
        path=backup_dir,
        reason="{} file(s) snapshotted".format(len(manifest["files"])),
        backup_id=backup_id,
        hashes={rel: item["sha256"] for rel, item in manifest["files"].items()},
    )


def load_manifest(
    backup_id: str, *, backups_dir: Optional[str] = None, verify: bool = False
) -> Dict[str, Any]:
    directory = _backup_path(backup_id, backups_dir)
    with open(os.path.join(directory, "manifest.json"), "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RepairRefused("refused", "{} has a malformed manifest".format(backup_id))
    if verify:
        problem = verify_manifest(payload, backups_dir or os.path.dirname(directory))
        if problem:
            raise RepairRefused("refused", problem)
    return payload


def _backup_path(backup_id: str, backups_dir: Optional[str]) -> str:
    raw = (backup_id or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw or "\x00" in raw:
        raise RepairRefused("refused", "invalid backup id {!r}".format(backup_id))
    directory = backups_dir or default_backups_dir()
    path = os.path.join(directory, raw)
    if not os.path.isdir(path):
        raise RepairRefused("refused", "no such backup: {}".format(raw))
    return path


def list_backups(*, backups_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first.  A directory without a readable manifest is reported, not hidden."""
    directory = backups_dir or default_backups_dir()
    entries: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        return entries
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isdir(path):
            continue
        entry: Dict[str, Any] = {"id": name, "path": path, "label": "", "created_at": 0, "files": 0, "bytes": 0}
        try:
            manifest = load_manifest(name, backups_dir=directory)
        except (RepairRefused, OSError, ValueError) as exc:
            entry["error"] = "{}: {}".format(type(exc).__name__, exc)
            entries.append(entry)
            continue
        files = manifest.get("files") or {}
        entry.update(
            label=str(manifest.get("label") or ""),
            created_at=int(manifest.get("created_at") or 0),
            files=len(files),
            bytes=sum(int(item.get("size") or 0) for item in files.values()),
            harness_version=str(manifest.get("harness_version") or "?"),
        )
        signature_problem = verify_manifest(manifest, directory)
        entry["verified"] = not signature_problem
        if signature_problem:
            entry["unverified"] = signature_problem
        entries.append(entry)
    entries.sort(key=lambda item: (item.get("created_at") or 0, item["id"]), reverse=True)
    return entries


# --------------------------------------------------------------------------------------
# The test gate
# --------------------------------------------------------------------------------------


def gate_modules(
    relative_path: str = "",
    test_modules: Optional[Sequence[str]] = None,
    *,
    full: bool = False,
) -> Sequence[str]:
    """Which test modules the gate runs.

    Default is **bounded**: the fast base subset plus the modules that cover the file
    being edited, which is a few seconds rather than half a minute.  On a phone that
    matters — the gate runs while the user waits, and Pyto's watchdog is watching.  The
    full suite is opt-in: ``full=True``, ``PYTO_HARNESS_REPAIR_GATE=full``, or an explicit
    module list (which the tests use to keep the suite fast and deterministic).
    """
    if test_modules is not None:
        return tuple(test_modules)
    raw = os.environ.get(TEST_MODULES_ENV, "").strip()
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if full or os.environ.get(GATE_ENV, "").strip().lower() in ("full", "all", "1", "yes"):
        return ()
    return tuple(doctor.gate_modules_for(relative_path))


def run_gate(
    root: str,
    *,
    relative_path: str = "",
    test_modules: Optional[Sequence[str]] = None,
    full: bool = False,
    timeout: float = 900.0,
) -> Dict[str, Any]:
    """Run the offline suite (bounded, by default) through :mod:`harness.doctor`'s runner."""
    ctx = doctor.DoctorContext.for_config(
        Config(),
        root=root,
        state=os.path.join(_state_for(root), "gate"),
        network=False,
        persist=False,
        selftest_timeout=timeout,
    )
    modules = gate_modules(relative_path, test_modules, full=full)
    report = doctor.run_offline_tests(ctx, modules=modules or None, timeout=timeout)
    report["gate"] = {
        "modules": list(modules) or ["<all>"],
        "full": not modules,
        "coverage": list(doctor.GATE_COVERAGE.get((relative_path or "").replace(os.sep, "/"), ())),
    }
    return report


def _tests_failed(report: Mapping[str, Any]) -> bool:
    if report.get("ok"):
        return False
    return True


# --------------------------------------------------------------------------------------
# The edit gate
# --------------------------------------------------------------------------------------


def apply_source_edit(
    relative_path: str,
    new_source: str,
    reason: str = "",
    *,
    root: Optional[str] = None,
    backups_dir: Optional[str] = None,
    run_tests: bool = True,
    test_modules: Optional[Sequence[str]] = None,
    full_gate: bool = False,
    timeout: float = 900.0,
) -> RepairResult:
    """Replace one harness source file, gated by the offline test suite.

    Success means: the new source parses as Python 3.10, imports nothing outside the
    standard library, the file was written, **and the suite still passes**.  Any other
    outcome leaves the tree byte-for-byte as it was.
    """
    if getattr(_ACTIVE, "repairing", False):
        return RepairResult(
            False,
            "refused",
            path=relative_path,
            reason="a self-repair is already running in this process; nested repairs are refused",
        )
    resolved_root = os.path.abspath(root or doctor.harness_root())
    ok, why = can_self_repair(resolved_root, backups_dir)
    if not ok:
        return RepairResult(False, "cannot-self-repair", path=resolved_root, reason=why)
    try:
        relative = _validate_relative(relative_path)
        target = _resolve_target(resolved_root, relative)
    except RepairRefused as refusal:
        return RepairResult(False, refusal.decision, path=str(relative_path), reason=refusal.reason)
    source = new_source if isinstance(new_source, str) else ""
    if not source.strip():
        return RepairResult(False, "refused", path=relative, reason="the new source is empty; that is a deletion")
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        return RepairResult(
            False,
            "refused",
            path=relative,
            reason="the new source is {} bytes, above the {} byte limit".format(len(encoded), MAX_SOURCE_BYTES),
        )
    try:
        previous_bytes: Optional[bytes] = None
        if os.path.exists(target):
            with open(target, "rb") as handle:
                previous_bytes = handle.read()
        previous_text = previous_bytes.decode("utf-8") if previous_bytes is not None else None
    except OSError as exc:
        return RepairResult(
            False,
            "cannot-self-repair",
            path=relative,
            reason="cannot read {}: {}: {}".format(relative, type(exc).__name__, exc),
        )
    if previous_text is not None and previous_text == source:
        return RepairResult(False, "refused", path=relative, reason="the new source is identical to the current file")
    try:
        ast.parse(source, filename=relative, feature_version=(3, 10))
    except SyntaxError as exc:
        return RepairResult(
            False,
            "refused",
            path=relative,
            reason="the new source is not valid Python 3.10: line {}: {}".format(exc.lineno, exc.msg),
        )
    stdlib_problem = _static_import_problem(resolved_root, relative, source)
    if stdlib_problem:
        return RepairResult(False, "refused", path=relative, reason=stdlib_problem)
    diff = _unified_diff(previous_text or "", source, relative)

    if not run_tests:
        return RepairResult(
            False,
            "refused",
            path=relative,
            reason="the edit gate requires the offline test suite; an ungated edit is refused",
            diff=diff,
        )

    _ACTIVE.repairing = True
    try:
        snap = snapshot("pre-edit-" + posixpath.basename(relative), root=resolved_root, backups_dir=backups_dir)
        if not snap.ok:
            return RepairResult(
                False,
                "cannot-self-repair",
                path=relative,
                reason="could not snapshot before editing: {}".format(snap.reason),
                diff=diff,
            )
        backup_id = snap.backup_id
        try:
            _write_bytes(target, encoded)
        except OSError as exc:
            restore_note = _restore_previous(target, previous_bytes)
            return RepairResult(
                False,
                "reverted",
                path=relative,
                reason="the write failed; the previous bytes were put back",
                diff=diff,
                backup_id=backup_id,
                error="{}: {} {}".format(type(exc).__name__, exc, restore_note),
            )
        report = run_gate(
            resolved_root, relative_path=relative, test_modules=test_modules, full=full_gate, timeout=timeout
        )
        tests = _test_summary(report)
        if _tests_failed(report):
            restore_note = _restore_previous(target, previous_bytes)
            return RepairResult(
                False,
                "reverted",
                path=relative,
                reason="the offline test suite failed, so the edit was reverted",
                diff=diff,
                backup_id=backup_id,
                tests=tests,
                error=(report.get("output_tail") or report.get("error") or "no test output")[-4000:],
                warnings=[
                    "the file is byte-for-byte what it was before the edit" + restore_note,
                    "the test output above is verbatim; do not weaken the tests to make an edit pass",
                ],
            )
        return RepairResult(
            True,
            "promoted",
            path=relative,
            reason=reason or "source edit promoted",
            diff=diff,
            backup_id=backup_id,
            tests=tests,
            warnings=[
                "the running process still holds the old module: restart the harness to load the change",
            ],
            hashes={"after": hashlib.sha256(encoded).hexdigest()},
        )
    finally:
        _ACTIVE.repairing = False


def apply_source_patch(
    relative_path: str,
    old_string: str,
    new_string: str,
    reason: str = "",
    *,
    root: Optional[str] = None,
    backups_dir: Optional[str] = None,
    count: int = 1,
    test_modules: Optional[Sequence[str]] = None,
    full_gate: bool = False,
    timeout: float = 900.0,
) -> RepairResult:
    """The same gated edit, expressed as one exact replacement.

    Rewriting a 3 000-line module to change three lines is neither practical for a model
    nor reviewable by a human, so the gate accepts a patch as well as a whole file.  The
    replacement is applied to the bytes on disk, then the *full* gate runs on the result.
    """
    resolved_root = os.path.abspath(root or doctor.harness_root())
    try:
        relative = _validate_relative(relative_path)
        target = _resolve_target(resolved_root, relative)
    except RepairRefused as refusal:
        return RepairResult(False, refusal.decision, path=str(relative_path), reason=refusal.reason)
    if not old_string:
        return RepairResult(False, "refused", path=relative, reason="old_string must not be empty")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            current = handle.read()
    except OSError as exc:
        return RepairResult(
            False,
            "refused",
            path=relative,
            reason="cannot read {}: {}: {}".format(relative, type(exc).__name__, exc),
        )
    occurrences = current.count(old_string)
    if occurrences == 0:
        return RepairResult(
            False,
            "refused",
            path=relative,
            reason="old_string was not found in {}; read the file and copy the text verbatim".format(relative),
        )
    updated = current.replace(old_string, new_string, max(1, int(count)))
    if updated == current:
        return RepairResult(False, "refused", path=relative, reason="the patch changes nothing")
    result = apply_source_edit(
        relative,
        updated,
        reason or "patched {} occurrence(s)".format(min(max(1, int(count)), occurrences)),
        root=resolved_root,
        backups_dir=backups_dir,
        test_modules=test_modules,
        full_gate=full_gate,
        timeout=timeout,
    )
    if occurrences > 1:
        result.warnings.append(
            "old_string appeared {} times; {} replaced".format(occurrences, min(max(1, int(count)), occurrences))
        )
    return result


def _load_audit(root: str) -> Optional[Any]:
    """Load ``stdlib_audit.py`` from ``root`` (never from ``sys.modules``)."""
    path = os.path.join(root, "stdlib_audit.py")
    if not os.path.isfile(path):
        return None
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location("_pyto_stdlib_audit_gate", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - the gate must not depend on the auditing tool
        return None


def _static_import_problem(root: str, relative: str, source: str) -> str:
    audit = _load_audit(root)
    if audit is None:
        return ""
    try:
        scan = audit.static_import_scan(source=source, filename=relative)
    except Exception:  # noqa: BLE001 - never block an edit on the auditing tool
        return ""
    suspicious = scan.get("suspicious") or []
    if suspicious:
        listed = ", ".join(sorted({"{} (line {})".format(item.get("module"), item.get("line")) for item in suspicious}))
        return "the new source imports something outside the standard library: {}".format(listed)
    if scan.get("parse_errors"):
        return "the new source does not parse at (3, 10)"
    return ""


def _write_bytes(target: str, payload: bytes) -> None:
    mode = None
    if os.path.exists(target):
        mode = stat.S_IMODE(os.stat(target).st_mode)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    temporary = target + ".repair-tmp"
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:  # pragma: no cover - refused on some iOS paths
            pass
    if mode is not None:
        try:
            os.chmod(temporary, mode)
        except OSError:  # pragma: no cover - non-POSIX
            pass
    os.replace(temporary, target)


def _restore_previous(target: str, previous_bytes: Optional[bytes]) -> str:
    """Put the previous bytes back.  Returns a short note about what happened."""
    try:
        if previous_bytes is None:
            if os.path.exists(target):
                os.unlink(target)
            return " (the new file was removed)"
        current = b""
        if os.path.exists(target):
            with open(target, "rb") as handle:
                current = handle.read()
        if current == previous_bytes:
            return " (verified identical)"
        _write_bytes(target, previous_bytes)
        with open(target, "rb") as handle:
            if handle.read() == previous_bytes:
                return " (verified identical)"
        return " (WARNING: the bytes on disk do not match the backup)"
    except OSError as exc:
        return " (WARNING: could not restore the previous bytes: {}: {})".format(type(exc).__name__, exc)


def _unified_diff(before: str, after: str, relative: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="{} (before)".format(relative),
            tofile="{} (after)".format(relative),
        )
    )
    text = "".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (diff truncated at {} chars)\n".format(MAX_DIFF_CHARS)
    return text


def _test_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "ok": bool(report.get("ok")),
        "mode": report.get("mode"),
        "ran": report.get("ran"),
        "failures": report.get("failures"),
        "errors": report.get("errors"),
        "skipped": report.get("skipped"),
        "duration_ms": report.get("duration_ms"),
        "modules": report.get("modules"),
        "gate": report.get("gate"),
    }


# --------------------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------------------


def restore(
    backup_id: str,
    *,
    root: Optional[str] = None,
    backups_dir: Optional[str] = None,
    run_tests: bool = True,
    test_modules: Optional[Sequence[str]] = None,
    full_gate: bool = False,
    timeout: float = 900.0,
) -> RepairResult:
    """Put a snapshot back, then run the same test gate.

    A restore is not trusted at all before it writes: the manifest must carry a valid HMAC
    for this installation, every payload is read and hashed, every target path must pass
    the repair jail, and **only then** is anything written.  A missing signature, a
    modified manifest or a single mismatched byte is a refusal that names what it found —
    the old behaviour (warn about the mismatch and restore the tampered bytes anyway) made
    ``restore_backup`` a write primitive for whatever could create a directory under
    ``backups/``.
    """
    if getattr(_ACTIVE, "repairing", False):
        return RepairResult(
            False, "refused", path=backup_id, reason="a self-repair is already running in this process"
        )
    resolved_root = os.path.abspath(root or doctor.harness_root())
    ok, why = can_self_repair(resolved_root, backups_dir)
    if not ok:
        return RepairResult(False, "cannot-self-repair", path=resolved_root, reason=why)
    try:
        directory = _backup_path(backup_id, backups_dir)
        manifest = load_manifest(backup_id, backups_dir=backups_dir)
    except RepairRefused as refusal:
        return RepairResult(False, refusal.decision, path=backup_id, reason=refusal.reason)
    except (OSError, ValueError) as exc:
        return RepairResult(
            False, "refused", path=backup_id, reason="cannot read the manifest: {}: {}".format(type(exc).__name__, exc)
        )
    signing_dir = backups_dir or os.path.dirname(directory)
    try:
        signature_problem = verify_manifest(manifest, signing_dir)
    except OSError as exc:  # pragma: no cover - unreadable key file
        signature_problem = "the backup signing key could not be read: {}: {}".format(type(exc).__name__, exc)
    if signature_problem:
        return RepairResult(False, "refused", path=backup_id, reason=signature_problem)
    files = manifest.get("files") or {}
    if not files:
        return RepairResult(False, "refused", path=backup_id, reason="the snapshot is empty")

    # Phase 1: read and verify *everything* before writing anything.  A refusal must not
    # leave the tree half-restored.
    payloads: List[Tuple[str, str, bytes]] = []
    for relative, item in sorted(files.items()):
        try:
            target = _resolve_target(resolved_root, relative, allow_protected=True)
        except RepairRefused as refusal:
            return RepairResult(
                False,
                refusal.decision,
                path=relative,
                reason="the snapshot names a path outside the restorable set: {}".format(refusal.reason),
            )
        source = os.path.join(directory, *relative.split("/"))
        try:
            with open(source, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            return RepairResult(
                False,
                "refused",
                path=backup_id,
                reason="the snapshot is missing {}: {}: {}".format(relative, type(exc).__name__, exc),
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != item.get("sha256"):
            return RepairResult(
                False,
                "refused",
                path=relative,
                reason=(
                    "REFUSING TO RESTORE: {} in snapshot {} does not match its signed manifest hash "
                    "(expected {}, found {}). The snapshot has been modified; nothing was written.".format(
                        relative, backup_id, item.get("sha256"), digest
                    )
                ),
                warnings=["the backup directory was tampered with; re-create the snapshot before restoring"],
            )
        payloads.append((relative, target, payload))

    _ACTIVE.repairing = True
    try:
        warnings: List[str] = []
        pre = snapshot("pre-restore", root=resolved_root, backups_dir=backups_dir)
        if pre.ok:
            warnings.append("the tree before this restore was saved as {}".format(pre.backup_id))
        else:
            warnings.append("could not snapshot the current tree before restoring: {}".format(pre.reason))
        restored: List[str] = []
        for relative, target, payload in payloads:
            try:
                _write_bytes(target, payload)
            except OSError as exc:
                return RepairResult(
                    False,
                    "cannot-self-repair",
                    path=relative,
                    reason="could not write {}: {}: {}".format(relative, type(exc).__name__, exc),
                    warnings=warnings,
                )
            restored.append(relative)
        present = set(_iter_sources(resolved_root))
        extra = sorted(present - set(files))
        if extra:
            warnings.append(
                "files exist now that were not in the snapshot (left in place): {}".format(", ".join(extra))
            )
        report = (
            run_gate(resolved_root, test_modules=test_modules, full=full_gate, timeout=timeout)
            if run_tests
            else {}
        )
        tests = _test_summary(report) if report else {}
        if report and _tests_failed(report):
            warnings.append(
                "LOUD: the offline test suite does NOT pass after this restore. The source is back to "
                "{} but the tree is not healthy -- run `--doctor` and `--repair` before trusting a run.".format(
                    backup_id
                )
            )
            return RepairResult(
                True,
                "restored-with-failing-tests",
                path=backup_id,
                reason="restored {} file(s)".format(len(restored)),
                backup_id=backup_id,
                tests=tests,
                error=(report.get("output_tail") or "")[-4000:],
                warnings=warnings,
            )
        return RepairResult(
            True,
            "restored",
            path=backup_id,
            reason="restored {} file(s) from {}".format(len(restored), backup_id),
            backup_id=backup_id,
            tests=tests,
            warnings=warnings,
            hashes={relative: files[relative].get("sha256", "") for relative in restored},
        )
    finally:
        _ACTIVE.repairing = False


def read_source(
    relative_path: str,
    *,
    root: Optional[str] = None,
    max_chars: int = 60000,
) -> RepairResult:
    """Read one harness source file, with the same path jail as an edit.

    The model cannot use ``read_file`` here: that tool is jailed to the workspace, and the
    harness source lives outside it.  Without this, ``self_edit`` would be blind.
    """
    resolved_root = os.path.abspath(root or doctor.harness_root())
    try:
        relative = _validate_relative(relative_path)
        target = _resolve_target(resolved_root, relative)
    except RepairRefused as refusal:
        return RepairResult(False, refusal.decision, path=str(relative_path), reason=refusal.reason)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read(max_chars + 1)
    except OSError as exc:
        return RepairResult(
            False, "refused", path=relative, reason="cannot read {}: {}: {}".format(relative, type(exc).__name__, exc)
        )
    truncated = len(text) > max_chars
    return RepairResult(
        True,
        "read",
        path=relative,
        reason="{} characters{}".format(min(len(text), max_chars), " (truncated)" if truncated else ""),
        text=text[:max_chars],
    )

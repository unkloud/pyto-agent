"""The one place that decides where ``~/pyto_harness`` really is.

On iOS, inside Pyto, there is no usable home directory: ``HOME`` is unset (or points
somewhere the sandbox refuses) and ``os.path.expanduser("~")`` returns the string ``"~"``
because CPython cannot resolve a ``pwd`` entry.  Code that trusts it builds paths such as
``~/pyto_harness`` — a *relative* path with a literal tilde — and iOS answers the first
``mkdir`` with ``[Errno 1] Operation not permitted``.

This module is the fix, in one place: :func:`resolve_home` returns an **absolute,
existing, writable** directory, proving writability with a real probe (create a private
directory and a temp file, then delete it), and raising :class:`~harness.errors.ConfigError`
with the workaround (``PYTO_HARNESS_HOME``) when nothing is writable.  It never returns a
path that still contains an unexpanded ``~``.

Precedence, first writable candidate wins:

1. ``PYTO_HARNESS_HOME`` — the documented escape hatch (created if missing);
2. ``HOME`` from the environment, when it is absolute and writable;
3. ``os.path.expanduser("~")``, when the result is absolute (i.e. really expanded);
4. the folder Pyto runs scripts from — the current working directory, then the folder
   that holds ``run.py`` — each probed through a ``pyto_harness`` directory inside it;
5. :func:`tempfile.gettempdir` with a ``pyto_harness`` subdirectory.  This one is
   **temporary**: iOS can purge it at any time, so a loud warning is printed;
6. nothing writable → :class:`ConfigError` naming the folder Pyto opened and the exact
   line to run.

The state directory is :data:`STATE_DIR_NAME` (``pyto_harness``), deliberately without a
leading dot: the iOS Files app hides dot-folders, so a hidden state directory could not be
seen, saved into, backed up or deleted from the device.  Installs made before the rename
kept it under the legacy hidden name, :data:`LEGACY_STATE_DIR_NAME`;
:func:`migrate_legacy_state` moves it out of hiding exactly once, at startup.

The move has to happen *before the resolver probes anything*, because a probe **creates**
``<home>/pyto_harness`` — and an empty new directory on disk is what used to make the move
a no-op and strand the old data.  The startup hooks therefore use
:func:`candidate_homes`, a guess that only reads the environment, ``argv[0]`` and the
current directory and creates nothing, and call :func:`migrate_legacy_state` for each
candidate.  When the new directory is already there, :func:`migrate_legacy_state` moves the
old entries in one by one without overwriting anything, or (when the new directory already
owns a ``config.json``) leaves the old one alone and says so — it never merges two configs
and never deletes a non-empty folder.

The winner is cached per process (one probe, not one per call) and keyed by the values
that can change the answer, so setting ``PYTO_HARNESS_HOME`` in a running interpreter is
honoured.  :func:`reset_home_cache` clears it for tests.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from .errors import ConfigError
from .security import mkdir_private

#: The directory the harness keeps its own state in, inside the resolved home.  Visible in
#: the iOS Files app: ``pyto_harness``, no leading dot.
STATE_DIR_NAME = "pyto_harness"
#: The name releases before the visible-state change used.  Read once at startup by
#: :func:`migrate_legacy_state` and then left empty; nothing writes to it any more.
LEGACY_STATE_DIR_NAME = ".pyto_harness"
#: The default workspace directory, inside the resolved home.
WORKSPACE_DIR_NAME = "pyto_harness_workspace"
#: The subdirectory of the system temp dir used when nothing else is writable.
TEMP_HOME_DIR_NAME = "pyto_harness"

#: The escape hatch: a directory to use *as the home directory* (state goes in
#: ``$PYTO_HARNESS_HOME/pyto_harness``).
ENV_HOME = "PYTO_HARNESS_HOME"
#: The config-file override, honoured next to the home (kept here so one module owns
#: every path rule; :mod:`harness.config` reads it).
ENV_CONFIG = "PYTO_HARNESS_CONFIG"
#: The state-directory override.
ENV_STATE_DIR = "PYTO_HARNESS_STATE_DIR"

#: Prefix of the probe file; created 0600 and deleted immediately.
PROBE_PREFIX = STATE_DIR_NAME + "-write-probe-"

#: The exact line a user can paste.  Quoted verbatim in every failure message.
WORKAROUND = (
    "Set {env} to somewhere you can write, for example:\n"
    '  import os; os.environ["{env}"] = os.getcwd()'
).format(env=ENV_HOME)

#: One-line reminder printed (once per process) when the temp fallback wins.
TEMPORARY_WARNING = (
    "[warning] ~/" + STATE_DIR_NAME + " is not usable here; using {path} instead.\n"
    "[warning] That folder is temporary: iOS can purge it at any time, and the config,\n"
    "[warning] sessions, memory and backups kept in it can disappear. Set {env} to a\n"
    "[warning] folder you control (for example: {example}) to keep them."
)

#: What the user is told when the old, hidden state directory has been moved.  Plain human
#: text (the tilde is a product name here, not a path the harness will ever open).
MIGRATED_MESSAGE = "moved the old ~/{legacy} to ~/{state} so the Files app can see it".format(
    legacy=LEGACY_STATE_DIR_NAME, state=STATE_DIR_NAME
)

#: The same news for the case where ``<home>/pyto_harness`` already existed when the old
#: folder was found -- typically because a write probe created it before the move ran.
#: The entries are moved in one by one (never overwriting), so the message says "merged".
MERGED_MESSAGE = "merged the old ~/{legacy} into ~/{state} so the Files app can see it".format(
    legacy=LEGACY_STATE_DIR_NAME, state=STATE_DIR_NAME
)

#: Nothing could be taken out of the old folder: every entry in it is already in the new
#: one (and the new one has no ``config.json``, or it would not have been touched at all).
MERGE_KEPT_MESSAGE = "kept the old ~/{legacy}: every entry in it is already in ~/{state}".format(
    legacy=LEGACY_STATE_DIR_NAME, state=STATE_DIR_NAME
)

#: Every single move failed: say so instead of claiming a merge that did not happen.
MERGE_FAILED_MESSAGE = "could not merge the old ~/{legacy} into ~/{state}".format(
    legacy=LEGACY_STATE_DIR_NAME, state=STATE_DIR_NAME
)


def legacy_kept_message(legacy_path: str) -> str:
    """``<home>/pyto_harness/config.json`` exists: two configs are never merged.

    Says where the old folder is and exactly how to remove it, and nothing else: the old
    folder still holds whatever the user put there.
    """
    return (
        "the old ~/{legacy} is still there and was left untouched: ~/{state} already has a\n"
        "config.json, and two configs are never merged. When you are sure the old one is not\n"
        "needed, delete it yourself -- in the Files app it is the hidden folder inside the\n"
        "Pyto folder (turn on 'Show Hidden Files'), or with:\n"
        "  rm -rf {path}"
    ).format(legacy=LEGACY_STATE_DIR_NAME, state=STATE_DIR_NAME, path=legacy_path)


@dataclass(frozen=True)
class HomeChoice:
    """How the home directory was chosen, for diagnostics and for the failure path."""

    path: str = ""
    #: Machine-readable winner: ``PYTO_HARNESS_HOME``, ``HOME``, ``expanduser``,
    #: ``cwd``, ``runpy``, ``tempdir`` or ``""``.
    source: str = ""
    #: Human suffix for the doctor line, e.g. ``from cwd; HOME was unusable``.
    note: str = ""
    #: Short reasons earlier candidates lost, in order.
    skipped: Tuple[str, ...] = ()
    #: True when the winner is the purgeable temp directory.
    temporary: bool = False
    #: The full actionable message when nothing was writable (``path`` is empty then).
    error: str = field(default="")

    @property
    def ok(self) -> bool:
        return bool(self.path) and not self.error

    def describe(self) -> str:
        """``/path (from cwd; HOME was unusable)`` — the doctor's one line."""
        if not self.path:
            return "<unresolved>"
        return "{} ({})".format(self.path, self.note) if self.note else self.path


# --------------------------------------------------------------------------------------
# Path rules
# --------------------------------------------------------------------------------------


def has_unexpanded_tilde(path: str) -> bool:
    """True when ``path`` still carries a ``~`` that was never expanded.

    Checked per path segment, so ``~/x``, ``/tmp/~/x`` and a literal ``~`` all count,
    while an ordinary ``/tmp/a~b`` does not.
    """
    if not path:
        return False
    return any(part.startswith("~") for part in str(path).split(os.sep))


def expand_user_path(path: str, *, what: str = "path") -> str:
    """Expand a user-supplied path, or reject it with the actionable message.

    Every ``os.path.expanduser`` on a path the user (or the model) supplied goes through
    here.  When the platform cannot expand ``~`` — Pyto's case — the result would be a
    relative path with a literal tilde, and the next write would try to create a directory
    called ``~``.  That is refused instead, with the escape hatch in the message.
    """
    if path is None:
        raise ConfigError("{} is empty".format(what))
    text = str(path).strip()
    if not text:
        return text
    expanded = os.path.expanduser(text)
    if has_unexpanded_tilde(expanded):
        raise ConfigError(
            "cannot expand the '~' in {} ({!r}): this device has no home directory, so the\n"
            "path would be used literally and the write would fail.\n"
            "Pass an absolute path instead.\n{}".format(what, text, WORKAROUND)
        )
    return os.path.abspath(expanded)


def writability_problem(path: str, *, create: bool = False) -> Optional[str]:
    """``None`` when ``path`` is (or can be made) a writable directory, else the reason.

    Used by the resolver and by the doctor's check, so both apply the same rule: a real
    write of a private temp file that is deleted again, never a ``stat`` guess.
    """
    if not path:
        return "no path was given"
    if has_unexpanded_tilde(path):
        return "the path still contains an unexpanded '~'"
    if not os.path.isabs(path):
        return "the path is not absolute"
    if create:
        try:
            mkdir_private(path)
        except OSError as exc:
            return "cannot create it ({})".format(_oserror_text(exc))
    if not os.path.isdir(path):
        return "it does not exist"
    return _probe_problem(path)


def _probe_problem(directory: str) -> Optional[str]:
    """Write a private temp file inside ``directory``; return the failure, or ``None``."""
    try:
        descriptor, probe = tempfile.mkstemp(prefix=PROBE_PREFIX, dir=directory)
    except OSError as exc:
        return "it is not writable ({})".format(_oserror_text(exc))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"pyto-harness write probe\n")
            handle.flush()
    except OSError as exc:
        return "it is not writable ({})".format(_oserror_text(exc))
    finally:
        try:
            os.unlink(probe)
        except OSError:  # pragma: no cover - the probe is already gone
            pass
    return None


def _oserror_text(exc: OSError) -> str:
    return "{}: {}".format(type(exc).__name__, exc)


def _probe(directory: str, *, create: bool, subdir: str = "") -> Tuple[bool, str]:
    """Probe ``directory`` (or ``directory/subdir``, created) and return ``(ok, reason)``."""
    target = os.path.join(directory, subdir) if subdir else directory
    problem = writability_problem(target, create=create or bool(subdir))
    return (problem is None), (problem or "")


# --------------------------------------------------------------------------------------
# The visible state directory, and the one-time move out of hiding
# --------------------------------------------------------------------------------------


def state_dir_in(home_dir: str) -> str:
    """``<home_dir>/pyto_harness`` — the state directory this release uses."""
    return os.path.join(home_dir, STATE_DIR_NAME)


def legacy_state_dir_in(home_dir: str) -> str:
    """``<home_dir>`` + :data:`LEGACY_STATE_DIR_NAME` — the pre-rename hidden location."""
    return os.path.join(home_dir, LEGACY_STATE_DIR_NAME)


def migrate_legacy_state(home_dir: str) -> str:
    """Move a hidden ``<home>/.pyto_harness`` to ``<home>/pyto_harness``, once.

    Called where a run starts (``run.py``, ``install.py``, ``DoctorContext.for_config``)
    through :func:`migrate_candidate_homes`, and never from a library helper in the middle
    of a session, so it can only ever happen before anything has opened the state
    directory.  Because the resolver's write probe *creates* the new directory, the call
    has to come first: that is why the startup hooks guess the home with
    :func:`candidate_homes` instead of resolving it.

    The rules, in order:

    * nothing named ``.pyto_harness`` → nothing to do (the usual case after the first run).
    * ``.pyto_harness`` is a symlink or a regular file → refuse and report it, whatever
      else exists.  A symlink could point anywhere, and moving through it would move
      somebody else's directory.
    * ``<home>/pyto_harness`` does not exist → rename the old folder (``os.replace``),
      falling back to ``shutil.move`` for a cross-device home.  Both failing leaves the
      old directory exactly where it was.  This is the only case that prints
      :data:`MIGRATED_MESSAGE`.
    * ``<home>/pyto_harness`` exists and already has a ``config.json`` → leave the old
      folder alone and report where it is and how to delete it: a configured state
      directory is never merged into.
    * ``<home>/pyto_harness`` exists without a ``config.json`` (a write probe got there
      first) → move the old entries in one by one with ``shutil.move``, **never
      overwriting** an entry that is already there; skipped entries are named in the
      report, and the old folder is removed only once it is empty.  Prints
      :data:`MERGED_MESSAGE`.
    * ``<home>/pyto_harness`` is a symlink or a regular file → refuse and report it: the
      harness never writes through a link, and never replaces a file it did not create.

    Returns the message to show the user, or ``""`` when there was nothing to do: no old
    folder, an old folder with nothing in it, or a move that has already happened.  It
    never deletes a non-empty directory, and a failed move leaves the old directory in
    place.
    """
    if not home_dir:
        return ""
    new = state_dir_in(home_dir)
    legacy = legacy_state_dir_in(home_dir)
    if not os.path.lexists(legacy):
        return ""
    if os.path.islink(legacy):
        return (
            "{} is a symbolic link, not a folder, so the old state was left alone.\n"
            "Move it aside yourself (or delete the link) and re-run; the harness will not "
            "follow a link out of your home.".format(legacy)
        )
    if not os.path.isdir(legacy):
        return (
            "{} is a file, not a folder, so it was left alone (it may be yours, not the\n"
            "harness's). Move it aside and re-run if it is a leftover.".format(legacy)
        )
    if not os.path.lexists(new):
        try:
            os.replace(legacy, new)
        except OSError as first:
            try:
                shutil.move(legacy, new)
            except OSError as second:
                return (
                    "could not move the old {} to {} ({}; then {}).\n"
                    "Nothing was deleted: the old folder is still there, untouched. If a "
                    "partial {} was created, delete it and re-run.".format(
                        legacy, new, _oserror_text(first), _oserror_text(second), new
                    )
                )
        return MIGRATED_MESSAGE
    # The new directory is already there: the probe that created it (or an earlier run)
    # got ahead of the move.  Never rename a folder over it -- merge entry by entry.
    if os.path.islink(new):
        return (
            "{} is a symbolic link, not a folder, so nothing was merged and the old {} was\n"
            "left alone. Move the link aside yourself (or delete it) and re-run; the harness\n"
            "will not follow a link out of your home.".format(new, legacy)
        )
    if not os.path.isdir(new):
        return (
            "{} is a file, not a folder, so nothing was merged and the old {} was left alone\n"
            "(the file may be yours, not the harness's). Move it aside and re-run if it is a\n"
            "leftover.".format(new, legacy)
        )
    if os.path.isfile(os.path.join(new, "config.json")):
        return legacy_kept_message(legacy)
    return _merge_legacy_into(legacy, new)


def _merge_legacy_into(legacy: str, new: str) -> str:
    """Move each entry of ``legacy`` into ``new``, skipping (and naming) collisions.

    A collision is any name that already exists in ``new`` -- including a symlink or a
    directory -- and it is never overwritten: it is left exactly as it is and reported.  A
    symlink *inside* the old folder is moved as the link it is, never dereferenced, so the
    merge cannot copy or delete anything outside the two folders.  The legacy directory is
    removed only when it is empty *and* this call moved something; an old folder with
    nothing in it is left alone (silently: no data is at stake), so the doctor can still
    point at it.
    """
    try:
        names = sorted(os.listdir(legacy))
    except OSError as exc:
        return (
            "could not read the old {} ({}), so nothing was moved and it was left exactly\n"
            "where it is.".format(legacy, _oserror_text(exc))
        )
    if not names:
        return ""
    moved: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for name in names:
        source = os.path.join(legacy, name)
        target = os.path.join(new, name)
        if os.path.lexists(target):
            skipped.append(name)
            continue
        try:
            shutil.move(source, target)
        except OSError as exc:
            failed.append("{} ({})".format(name, _oserror_text(exc)))
        else:
            moved.append(name)
    lines = [MERGED_MESSAGE if moved else (MERGE_KEPT_MESSAGE if skipped else MERGE_FAILED_MESSAGE)]
    if moved:
        lines.append("moved: {}".format(", ".join(moved)))
    if skipped:
        lines.append("left in {} (already there): {}".format(new, ", ".join(skipped)))
    if failed:
        lines.append("could not move: {}".format(", ".join(failed)))
    leftovers = _listdir(legacy)
    if leftovers:
        lines.append("the old folder is still there: {} ({} left; nothing was deleted)".format(legacy, len(leftovers)))
    elif leftovers == [] and moved and not failed:
        try:
            os.rmdir(legacy)
        except OSError as exc:
            lines.append(
                "the old folder is empty now but could not be removed: {} ({})".format(legacy, _oserror_text(exc))
            )
    return "\n".join(lines)


def _listdir(path: str) -> Optional[List[str]]:
    """The entries in ``path``, or ``None`` when it cannot be read (never raises)."""
    try:
        return os.listdir(path)
    except OSError:
        return None


# --------------------------------------------------------------------------------------
# Candidate sources
# --------------------------------------------------------------------------------------


def harness_root() -> str:
    """The directory that holds ``run.py`` and the ``harness`` package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def entry_point_dirs() -> List[Tuple[str, str]]:
    """``(directory, label)`` pairs for the folders Pyto runs scripts from, in order.

    ``sys.argv[0]`` is the script Pyto opened (``run.py`` or the installer's ``start.py``);
    the harness package's own parent is the second chance, so a run started from a
    different working directory still finds the folder it was installed into.
    """
    pairs: List[Tuple[str, str]] = []
    argv0 = ""
    if getattr(sys, "argv", None):
        argv0 = str(sys.argv[0] or "")
    if argv0 and not argv0.startswith("-"):
        directory = os.path.dirname(os.path.abspath(argv0))
        if directory and os.path.isdir(directory):
            pairs.append((directory, "the folder that holds {}".format(os.path.basename(argv0))))
    root = harness_root()
    if os.path.isdir(root) and not any(os.path.abspath(root) == os.path.abspath(item[0]) for item in pairs):
        pairs.append((root, "the folder that holds run.py"))
    return pairs


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:  # pragma: no cover - a deleted working directory
        return ""


def candidate_homes(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The plausible home directories, in resolver order, guessed **without writing**.

    This is the cheap guess the startup hooks use to find a legacy ``.pyto_harness``
    *before* anything resolves the home: :func:`resolve_home` proves a candidate with a
    real write probe, and that probe creates ``<home>/pyto_harness`` -- which is exactly
    what used to defeat the one-time move.  So this function reads the environment,
    ``argv[0]`` and the current directory and nothing else: it creates no directory, writes
    no probe file and never calls :func:`resolve_home` (``os.path.isdir`` on the entry
    points is the only filesystem question it asks).

    The candidates mirror the resolver's first four sources:

    1. ``PYTO_HARNESS_HOME`` — absolutised like the resolver does, so a relative escape
       hatch is honoured;
    2. ``HOME`` — only when it is already an absolute path (never a literal ``~``);
    3. ``os.path.expanduser("~")`` — only when it genuinely expanded;
    4. the directories Pyto runs scripts from: the current directory, then the folder that
       holds ``run.py``.

    Duplicates are removed, order preserved.  A candidate that does not exist is still
    listed: whether it holds a legacy folder is :func:`migrate_legacy_state`'s question.
    The temporary fallback is deliberately absent — a home that only came into being
    because nothing else was writable cannot hold an install from before the rename.
    """
    env = environment(environ)
    candidates: List[str] = []

    def remember(path: str) -> None:
        text = str(path or "").strip()
        if not text or has_unexpanded_tilde(text) or not os.path.isabs(text):
            return
        absolute = os.path.abspath(text)
        if absolute not in candidates:
            candidates.append(absolute)

    escape = str(env.get(ENV_HOME) or "").strip()
    if escape and not has_unexpanded_tilde(escape):
        escape = os.path.abspath(escape)
    remember(escape)
    remember(str(env.get("HOME") or ""))
    try:
        expanded = os.path.expanduser("~")
    except Exception:  # noqa: BLE001 - a hostile expanduser must not stop the guess
        expanded = ""
    if expanded and expanded != "~":
        remember(expanded)
    remember(_cwd())
    for directory, _label in entry_point_dirs():
        remember(directory)
    return candidates


def migrate_candidate_homes(environ: Optional[Mapping[str, str]] = None) -> str:
    """Run :func:`migrate_legacy_state` for every :func:`candidate_homes` entry.

    The startup hook: ``run.py``, ``install.py`` and ``DoctorContext.for_config`` call this
    before they resolve a home or read a config, so the move happens before the probe can
    create an empty ``pyto_harness``.  It returns the messages for the folders that had
    something to report (joined), or ``""`` when there was nothing to do -- the usual case,
    and the only case after the first run.  It never raises: housekeeping must not stop a
    run, and a home it cannot migrate is reported by the run's own checks.
    """
    messages: List[str] = []
    for candidate in candidate_homes(environ):
        try:
            message = migrate_legacy_state(candidate)
        except Exception:  # noqa: BLE001 - a broken candidate must not stop the others
            continue
        if message:
            messages.append(message)
    return "\n".join(messages)


def _temp_root() -> str:
    """The system temporary directory (a seam so tests can make it unusable)."""
    return tempfile.gettempdir()


def _on_pyto() -> bool:
    """``harness.ios.is_pyto()`` without importing the whole iOS module at import time."""
    try:
        from .ios import is_pyto

        return bool(is_pyto())
    except Exception:  # noqa: BLE001 - a broken probe must not stop the resolver
        return False


# --------------------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------------------

#: Cache keyed by ``(purpose, PYTO_HARNESS_HOME, HOME, cwd)`` so a changed environment
#: re-resolves while repeated calls in one run do not re-probe.
_CACHE: Dict[Tuple[str, str, str, str], HomeChoice] = {}

#: Set once the temporary-folder warning has been printed.
_WARNED_TEMPORARY = False


def _cache_key(purpose: str, environ: Mapping[str, str]) -> Tuple[str, str, str, str]:
    # The working directory is part of the key: it is a candidate, and a process that
    # chdirs (or a test that does) must not keep a stale winner or a stale failure.
    return (purpose, str(environ.get(ENV_HOME) or ""), str(environ.get("HOME") or ""), _cwd())


def environment(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    """The environment the resolver reads.

    ``None`` is the process environment.  An explicit mapping is an **override layer** on
    top of it (``os.environ`` first, then the mapping), which lets a test or a caller
    change one variable without having to reconstruct the whole environment; setting a
    key to ``""`` means "treat it as unset".
    """
    if environ is None:
        return os.environ
    merged: Dict[str, str] = {str(name): str(value) for name, value in os.environ.items()}
    for name, value in environ.items():
        merged[str(name)] = "" if value is None else str(value)
    return merged


def reset_home_cache() -> None:
    """Forget the cached resolution.  For tests and for a changed environment."""
    global _WARNED_TEMPORARY
    _CACHE.clear()
    _WARNED_TEMPORARY = False


def resolve_home_choice(
    *,
    purpose: str = "state",
    environ: Optional[Mapping[str, str]] = None,
    refresh: bool = False,
) -> HomeChoice:
    """Resolve the home directory and return the full choice (never raises for the path).

    A failure is reported through :attr:`HomeChoice.error`; :func:`resolve_home` raises it
    as a :class:`ConfigError`.  The doctor uses this form so it can *report* the failure.
    """
    env = environment(environ)
    key = _cache_key(purpose, env)
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    choice = _resolve(purpose, env)
    _CACHE[key] = choice
    return choice


def resolve_home(
    *,
    purpose: str = "state",
    environ: Optional[Mapping[str, str]] = None,
    refresh: bool = False,
) -> str:
    """An absolute, existing, writable home directory.  Raises :class:`ConfigError`.

    This is the only supported way to answer "where is the harness allowed to write".
    The returned path never contains an unexpanded ``~``.
    """
    choice = resolve_home_choice(purpose=purpose, environ=environ, refresh=refresh)
    if not choice.ok:
        raise ConfigError(choice.error)
    return choice.path


def home_choice(purpose: str = "state") -> HomeChoice:
    """The cached :class:`HomeChoice` for this process (resolving it if needed)."""
    return resolve_home_choice(purpose=purpose)


def describe_home(purpose: str = "state") -> str:
    """``/path (from cwd; HOME was unusable)`` for the doctor's header line."""
    return home_choice(purpose).describe()


def _resolve(purpose: str, environ: Mapping[str, str]) -> HomeChoice:
    probe_subdir = STATE_DIR_NAME if purpose == "state" else "{}_{}".format(STATE_DIR_NAME, purpose)
    skipped: List[str] = []
    tried: List[str] = []

    # 1. PYTO_HARNESS_HOME: the documented escape hatch.  Created if missing, because the
    #    user explicitly asked for this folder.
    raw_home = str(environ.get(ENV_HOME) or "").strip()
    if not raw_home:
        tried.append("{0} is not set".format(ENV_HOME))
    elif has_unexpanded_tilde(raw_home):
        skipped.append("{0} was unusable".format(ENV_HOME))
        tried.append("{0}={1!r}: the '~' cannot be expanded on this device".format(ENV_HOME, raw_home))
    else:
        candidate = os.path.abspath(raw_home)
        ok, why = _probe(candidate, create=True)
        if ok:
            return HomeChoice(
                path=candidate,
                source=ENV_HOME,
                note=_note("from {} (the escape hatch)".format(ENV_HOME), skipped),
                skipped=tuple(skipped),
            )
        skipped.append("{0} was not writable".format(ENV_HOME))
        tried.append("{0}={1}: {2}".format(ENV_HOME, candidate, why))

    # 2. HOME from the environment: only when it is already an absolute path.
    raw_home_env = str(environ.get("HOME") or "").strip()
    if not raw_home_env:
        skipped.append("HOME was unset")
        tried.append("HOME is not set")
    elif not os.path.isabs(raw_home_env) or has_unexpanded_tilde(raw_home_env):
        skipped.append("HOME was not absolute")
        tried.append("HOME={!r} is not an absolute path without a '~'".format(raw_home_env))
    else:
        ok, why = _probe(raw_home_env, create=False)
        if ok:
            return HomeChoice(path=raw_home_env, source="HOME", note=_note("from HOME", skipped), skipped=tuple(skipped))
        skipped.append("HOME was unusable")
        tried.append("HOME={}: {}".format(raw_home_env, why))

    # 3. os.path.expanduser("~"): only when it really expanded to an absolute path.
    try:
        expanded = os.path.expanduser("~")
    except Exception as exc:  # noqa: BLE001 - a hostile expanduser must not be fatal
        expanded = ""
        tried.append("os.path.expanduser('~') raised {}: {}".format(type(exc).__name__, exc))
    else:
        if not expanded or expanded == "~" or not os.path.isabs(expanded) or has_unexpanded_tilde(expanded):
            skipped.append("the home directory was unresolved")
            tried.append("os.path.expanduser('~') returned {!r} (not an absolute path)".format(expanded))
        else:
            ok, why = _probe(expanded, create=False)
            if ok:
                return HomeChoice(
                    path=expanded,
                    source="expanduser",
                    note=_note("from ~ (os.path.expanduser)", skipped),
                    skipped=tuple(skipped),
                )
            skipped.append("the home directory was unusable")
            tried.append("os.path.expanduser('~')={}: {}".format(expanded, why))

    # 4. The folders Pyto runs scripts from: cwd first, then the folder holding run.py.
    #    Each is probed through the state directory that will actually be created inside.
    cwd = _cwd()
    if cwd:
        ok, why = _probe(cwd, create=False, subdir=probe_subdir)
        if ok:
            origin = "from cwd" + (" (Pyto)" if _on_pyto() else "")
            return HomeChoice(path=cwd, source="cwd", note=_note(origin, skipped), skipped=tuple(skipped))
        tried.append("{} (the folder Pyto opened): {}".format(cwd, why))
    else:  # pragma: no cover - only when the working directory was deleted
        tried.append("os.getcwd() failed")

    for directory, label in entry_point_dirs():
        ok, why = _probe(directory, create=False, subdir=probe_subdir)
        if ok:
            return HomeChoice(
                path=directory,
                source="runpy",
                note=_note("from {} ({})".format(label, os.path.join(directory, probe_subdir)), skipped),
                skipped=tuple(skipped),
            )
        tried.append("{} ({}): {}".format(directory, label, why))

    # 5. The system temp directory: works, but iOS can purge it without warning.
    try:
        temp_root = _temp_root()
    except Exception as exc:  # noqa: BLE001 - a broken tempdir must not be fatal
        temp_root = ""
        tried.append("tempfile.gettempdir() raised {}: {}".format(type(exc).__name__, exc))
    if temp_root:
        candidate = os.path.join(temp_root, TEMP_HOME_DIR_NAME)
        ok, why = _probe(candidate, create=True)
        if ok:
            _warn_temporary(candidate)
            return HomeChoice(
                path=candidate,
                source="tempdir",
                note=_note("from {} (TEMPORARY: iOS can purge it)".format(candidate), skipped),
                skipped=tuple(skipped),
                temporary=True,
            )
        tried.append("{} (temporary folder): {}".format(candidate, why))

    # 6. Nothing writable: say what to do about it.
    return HomeChoice(error=no_home_message(tried, folder_name=probe_subdir), skipped=tuple(skipped))


def _note(origin: str, skipped: List[str]) -> str:
    if not skipped:
        return origin
    return "{}; {}".format(origin, "; ".join(skipped))


def no_home_message(tried: List[str], *, folder_name: str = STATE_DIR_NAME, cwd: Optional[str] = None) -> str:
    """The actionable text a user sees when no candidate directory is writable."""
    folder = _cwd() if cwd is None else cwd
    where = folder or "<os.getcwd() failed>"
    lines = [
        "cannot find a writable folder for ~/{}. This device has no home directory, and".format(folder_name),
        "every fallback the harness tried is unwritable or cannot be created:",
    ]
    lines.extend("  - {}".format(item) for item in tried)
    lines.append("The folder Pyto opened is {}.".format(where))
    lines.append(WORKAROUND)
    return "\n".join(lines)


def _warn_temporary(path: str) -> None:
    global _WARNED_TEMPORARY
    if _WARNED_TEMPORARY:
        return
    _WARNED_TEMPORARY = True
    text = TEMPORARY_WARNING.format(
        path=path,
        env=ENV_HOME,
        example='import os; os.environ["{}"] = os.getcwd()'.format(ENV_HOME),
    )
    try:
        print(text, file=sys.stderr)
    except Exception:  # pragma: no cover - a closed stderr must not break the run
        pass


def temporary_home_warning() -> Optional[str]:
    """The loud warning text when the temp fallback won, else ``None`` (for reports)."""
    choice = home_choice()
    if choice.ok and choice.temporary:
        return TEMPORARY_WARNING.format(
            path=choice.path,
            env=ENV_HOME,
            example='import os; os.environ["{}"] = os.getcwd()'.format(ENV_HOME),
        )
    return None

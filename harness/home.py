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

Precedence, first *usable* candidate wins:

1. ``PYTO_HARNESS_HOME`` — the documented escape hatch (created if missing);
2. ``PYTO_HARNESS_STATE_DIR`` / ``PYTO_HARNESS_CONFIG`` — when the user has named the
   state folder (or the config file) explicitly, the folder that owns it is the home;
3. the **remembered choice**: the visible pointer file written next to the entry point
   (``<install>/pyto_harness_home.txt``).  It is read first, validated for writability,
   and ignored — with a report — when it no longer names a usable folder;
4. **an existing state folder**: when exactly one of the known candidates (``HOME``, a
   genuinely expanded ``~``, the install's parent, the install itself, the current
   directory) already holds ``pyto_harness/config.json``, that folder is adopted and the
   pointer file records it.  Nothing is ever moved, copied, merged or deleted here.  Two
   or more candidates with a config: the first in this order wins, and a plain warning
   names every one of them and says how to switch;
5. ``HOME`` from the environment when it is absolute and writable, then
   ``os.path.expanduser("~")`` when the result is absolute (i.e. really expanded);
6. the folder **above the install** — ``<parent of the folder holding run.py>``, probed
   through the ``pyto_harness`` directory inside it.  This is the default: it survives
   replacing the install directory and never depends on where the script was started;
7. the install folder itself, then the folder Pyto opened (the current working
   directory) — each probed through a ``pyto_harness`` directory inside it;
8. :func:`tempfile.gettempdir` with a ``pyto_harness`` subdirectory.  This one is
   **temporary**: iOS can purge it at any time, so a loud warning is printed (and the
   pointer file is *not* written: a purgeable folder must not be remembered);
9. nothing writable → :class:`ConfigError` naming the folder Pyto opened, every candidate
   that was tried and the exact line to run.

The state folder therefore never lands inside the code search path by accident, the choice
is stable across runs started from different directories, and nothing is created, moved or
deleted anywhere except inside the folder :func:`resolve_home` returns.

The state directory is :data:`STATE_DIR_NAME` (``pyto_harness``), deliberately without a
leading dot: the iOS Files app hides dot-folders, so a hidden state directory could not be
seen, saved into, backed up or deleted from the device.  Installs made before the rename
kept it under the legacy hidden name, :data:`LEGACY_STATE_DIR_NAME`;
:func:`migrate_legacy_state` moves it out of hiding exactly once, at startup.

The move has to happen *before the resolver probes anything*, because a probe **creates**
``<home>/pyto_harness`` — and an empty new directory on disk is what used to make the move
a no-op and strand the old data.  The startup hooks therefore use
:func:`candidate_homes`, a guess that only reads the environment, the pointer file,
``argv[0]`` and the current directory and creates nothing, and call
:func:`migrate_legacy_state` for each candidate.  When the new directory is already there, :func:`migrate_legacy_state` moves the
old entries in one by one without overwriting anything, or (when the new directory already
owns a ``config.json``) leaves the old one alone and says so — it never merges two configs
and never deletes a non-empty folder.

The winner is cached per process (one probe, not one per call) and keyed by the values
that can change the answer, so setting ``PYTO_HARNESS_HOME`` in a running interpreter is
honoured.  :func:`reset_home_cache` clears it for tests.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError
from .security import mkdir_private, open_private

#: The directory the harness keeps its own state in, inside the resolved home.  Visible in
#: the iOS Files app: ``pyto_harness``, no leading dot.
STATE_DIR_NAME = "pyto_harness"
#: The config file inside the state directory.  Spelled here once so the resolver can ask
#: "does this candidate already hold a config?" without importing :mod:`harness.config`
#: (which imports this module).
CONFIG_FILE_NAME = "config.json"
#: The remembered choice: a **visible** file (no leading dot, so the Files app shows it)
#: written next to the entry point, holding the home directory that was chosen.  One line
#: of path, ``#`` comments allowed.
POINTER_FILE_NAME = "pyto_harness_home.txt"
#: Never read more than this from a pointer file: it is a path, not a document.
POINTER_MAX_BYTES = 4096
#: The header line of the pointer file, so a human who opens it knows what it is.
POINTER_HEADER = (
    "# pyto-harness: the folder that holds {state}/. Delete this file to choose again.".format(
        state=STATE_DIR_NAME
    )
)

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
class StateCandidate:
    """One folder that already holds ``<home>/pyto_harness/config.json``."""

    home: str = ""
    config: str = ""
    label: str = ""
    has_api_key: bool = False

    def describe(self) -> str:
        return "{} ({}; config with an api_key: {})".format(
            self.config, self.label or "candidate", "yes" if self.has_api_key else "no"
        )


@dataclass(frozen=True)
class PointerRecord:
    """What the visible pointer file next to the entry point says, if anything."""

    path: str = ""
    home: str = ""
    #: Why the file was ignored ("" when it was not read or was usable).
    problem: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.home) and not self.problem


@dataclass(frozen=True)
class HomeChoice:
    """How the home directory was chosen, for diagnostics and for the failure path."""

    path: str = ""
    #: Machine-readable winner: ``PYTO_HARNESS_HOME``, ``PYTO_HARNESS_STATE_DIR``,
    #: ``PYTO_HARNESS_CONFIG``, ``pointer``, ``adopted``, ``HOME``, ``expanduser``,
    #: ``install_parent``, ``runpy``, ``cwd``, ``tempdir`` or ``""``.
    source: str = ""
    #: Human suffix for the doctor line, e.g. ``from cwd; HOME was unusable``.
    note: str = ""
    #: Short reasons earlier candidates lost, in order.
    skipped: Tuple[str, ...] = ()
    #: True when the winner is the purgeable temp directory.
    temporary: bool = False
    #: The full actionable message when nothing was writable (``path`` is empty then).
    error: str = field(default="")
    #: The pointer file that recorded (or is about to record) the choice, when there is one.
    pointer: str = ""
    #: True when the choice came *from* the pointer file rather than being written to it.
    pointer_used: bool = False
    #: Every candidate that already held a ``pyto_harness/config.json``, in precedence
    #: order -- including the ones that were not chosen.
    candidates: Tuple[StateCandidate, ...] = ()
    #: ``pyto_harness 2``-style folders and duplicated install folders: likely iCloud/Files
    #: copies.  Reported, never touched.
    duplicates: Tuple[str, ...] = ()
    #: The plain warning to print once per process (ambiguity, stale pointer, duplicates).
    warning: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path) and not self.error

    @property
    def adopted_from(self) -> str:
        """The candidate folder whose config was adopted (``""`` when not adopted)."""
        return self.path if self.source == SOURCE_ADOPTED else ""

    def describe(self) -> str:
        """``/path (from cwd; HOME was unusable)`` — the doctor's one line."""
        if not self.path:
            return "<unresolved>"
        return "{} ({})".format(self.path, self.note) if self.note else self.path

    def unchosen_candidates(self) -> Tuple[StateCandidate, ...]:
        """Candidates that hold a config but are not the home in use."""
        if not self.path:
            return self.candidates
        return tuple(item for item in self.candidates if os.path.abspath(item.home) != os.path.abspath(self.path))


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


# --------------------------------------------------------------------------------------
# The remembered choice, the folder above the install, and existing state
# --------------------------------------------------------------------------------------

#: Machine-readable sources, kept in one place so tests and reports never spell them out.
SOURCE_ENV_HOME = ENV_HOME
SOURCE_ENV_STATE = ENV_STATE_DIR
SOURCE_ENV_CONFIG = ENV_CONFIG
SOURCE_POINTER = "pointer"
SOURCE_ADOPTED = "adopted"
SOURCE_HOME = "HOME"
SOURCE_EXPANDUSER = "expanduser"
SOURCE_INSTALL_PARENT = "install_parent"
SOURCE_INSTALL = "runpy"
SOURCE_CWD = "cwd"
SOURCE_TEMPDIR = "tempdir"


def config_path_in(home_dir: str) -> str:
    """``<home_dir>/pyto_harness/config.json`` — the file that makes a home *the* home."""
    return os.path.join(home_dir, STATE_DIR_NAME, CONFIG_FILE_NAME)


def owner_home(path: str) -> str:
    """The home that owns ``path``: its parent when it is itself a ``pyto_harness`` folder."""
    cleaned = path.rstrip(os.sep) or path
    parent = os.path.dirname(cleaned)
    if os.path.basename(cleaned) == STATE_DIR_NAME and parent:
        return parent
    return path


def override_home(environ: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, str, str]]:
    """``(home, source, description)`` for ``PYTO_HARNESS_STATE_DIR`` / ``PYTO_HARNESS_CONFIG``.

    ``None`` when neither is set.  The folder that *owns* the state directory (or the config
    file) is the home, so with the documented layout — ``$HOME/pyto_harness/config.json`` —
    the home is ``$HOME`` and ``<home>/pyto_harness`` is the very folder the user named.
    Raises :class:`ConfigError` for an unexpandable ``~``, exactly like the path helpers this
    mirrors (``config.default_state_dir`` / ``config.default_config_path``); the caller
    decides whether that is fatal.
    """
    env = environment(environ)
    raw_state = str(env.get(ENV_STATE_DIR) or "").strip()
    if raw_state:
        state_path = expand_user_path(raw_state, what=ENV_STATE_DIR)
        return (owner_home(state_path), SOURCE_ENV_STATE, "{}={}".format(ENV_STATE_DIR, state_path))
    raw_config = str(env.get(ENV_CONFIG) or "").strip()
    if raw_config:
        config_path = expand_user_path(raw_config, what=ENV_CONFIG)
        return (
            owner_home(os.path.dirname(config_path)),
            SOURCE_ENV_CONFIG,
            "{}={}".format(ENV_CONFIG, config_path),
        )
    return None


def pointer_file_paths() -> List[str]:
    """Every place the remembered choice may live, best first (next to the entry point)."""
    paths: List[str] = []
    for directory, _label in entry_point_dirs():
        candidate = os.path.join(directory, POINTER_FILE_NAME)
        if candidate not in paths:
            paths.append(candidate)
    return paths


def primary_pointer_path() -> str:
    """Where the remembered choice is written: next to the running entry point."""
    paths = pointer_file_paths()
    return paths[0] if paths else ""


def first_path_line(text: str) -> str:
    """The first non-empty, non-comment line of a pointer file (``""`` when none)."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def read_pointer() -> PointerRecord:
    """Read the pointer file next to the entry point, if there is one.

    **Reads only**: nothing is created and nothing is written, so the startup hooks can call
    this before anything resolves a home.  A missing file returns an empty record.  A file
    that is empty, or that names a relative path or a path with an unexpanded ``~``, is
    *ignored* — the reason lands in :attr:`PointerRecord.problem`, the resolver re-resolves,
    and nothing is ever raised.  Whether the folder it names is still writable is the
    resolver's question (it is the one that probes).
    """
    for path in pointer_file_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(POINTER_MAX_BYTES)
        except OSError as exc:
            return PointerRecord(path=path, problem="it could not be read ({})".format(_oserror_text(exc)))
        named = first_path_line(text)
        if not named:
            return PointerRecord(path=path, problem="it does not name a folder")
        if has_unexpanded_tilde(named) or not os.path.isabs(named):
            return PointerRecord(
                path=path, problem="it names {!r}, which is not an absolute path".format(named)
            )
        return PointerRecord(path=path, home=os.path.abspath(named))
    return PointerRecord()


def record_pointer(home_dir: str) -> str:
    """Write (or refresh) the pointer file next to the entry point.  Never raises.

    Returns the path written, or ``""`` when there is nowhere to write it.  The file is
    written ``0600`` through a temporary file and an atomic replace, so a torn pointer can
    never become the remembered choice, and a path that is not a regular file is left alone
    rather than written through.  A pointer that already names ``home_dir`` is not touched.
    """
    if not home_dir:
        return ""
    path = primary_pointer_path()
    if not path:
        return ""
    if os.path.lexists(path) and not os.path.isfile(path):
        return ""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                current = first_path_line(handle.read(POINTER_MAX_BYTES))
            if (
                current
                and not has_unexpanded_tilde(current)
                and os.path.isabs(current)
                and os.path.abspath(current) == os.path.abspath(home_dir)
            ):
                return path
        temporary = path + ".tmp"
        with open_private(temporary, truncate=True) as handle:
            handle.write("{}\n{}\n".format(POINTER_HEADER, os.path.abspath(home_dir)))
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(path + ".tmp")
        except OSError:
            pass
        return ""
    return path


def looks_like_install(directory: str) -> bool:
    """True when ``directory`` really is an install folder (holds ``run.py`` or ``harness``).

    The folder above an install is a home candidate; the folder above an unrelated
    ``sys.argv[0]`` (``python -m unittest``, a wrapper in ``/usr/lib``) must never be one.
    """
    if not directory or not os.path.isdir(directory):
        return False
    return os.path.isfile(os.path.join(directory, "run.py")) or os.path.isdir(
        os.path.join(directory, "harness")
    )


def install_parent_dirs() -> List[Tuple[str, str]]:
    """``(directory, label)`` for the folder above each install folder, best first.

    The folder above the install is the new default home: it survives replacing the install
    directory (the common way to update on iOS) and never moves with the working directory.
    The filesystem root is never a candidate.
    """
    pairs: List[Tuple[str, str]] = []
    for directory, _label in entry_point_dirs():
        absolute = os.path.abspath(directory)
        if not looks_like_install(absolute):
            continue
        parent = os.path.dirname(absolute)
        if not parent or parent == absolute or parent == os.path.dirname(parent):
            continue  # the filesystem root has no parent worth probing
        if not os.path.isdir(parent):
            continue
        if any(os.path.abspath(existing) == parent for existing, _label in pairs):
            continue
        pairs.append((parent, "the folder above the install"))
    return pairs


def state_candidate_dirs(environ: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    """``(home, label)`` for the folders that may already hold ``pyto_harness``, in order.

    The order is the resolver's own precedence among them — ``HOME``, a genuinely expanded
    ``~``, the folder above the install, the install itself, then the working directory — so
    "the highest-precedence candidate" means the same thing here and in the fallback rules.
    This function only *reads*: nothing is created, moved or deleted.
    """
    env = environment(environ)
    pairs: List[Tuple[str, str]] = []

    def remember(path: str, label: str) -> None:
        text = str(path or "").strip()
        if not text or has_unexpanded_tilde(text) or not os.path.isabs(text):
            return
        absolute = os.path.abspath(text)
        if any(existing == absolute for existing, _label in pairs):
            return
        pairs.append((absolute, label))

    remember(str(env.get("HOME") or ""), "HOME")
    try:
        expanded = os.path.expanduser("~")
    except Exception:  # noqa: BLE001 - a hostile expanduser must not stop the scan
        expanded = ""
    if expanded and expanded != "~":
        remember(expanded, "a genuinely expanded ~")
    for directory, label in install_parent_dirs():
        remember(directory, label)
    for directory, label in entry_point_dirs():
        remember(directory, label)
    remember(_cwd(), "the current directory")
    return pairs


def config_has_api_key(path: str) -> bool:
    """True when the config file holds a non-empty ``api_key``.  The value is never kept."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    key = payload.get("api_key")
    return isinstance(key, str) and bool(key.strip())


def candidates_with_config(environ: Optional[Mapping[str, str]] = None) -> List[StateCandidate]:
    """Every known candidate that already holds ``pyto_harness/config.json``, in order."""
    found: List[StateCandidate] = []
    for home_dir, label in state_candidate_dirs(environ):
        path = config_path_in(home_dir)
        if not os.path.isfile(path):
            continue
        found.append(
            StateCandidate(home=home_dir, config=path, label=label, has_api_key=config_has_api_key(path))
        )
    return found


def is_duplicate_name(name: str, stem: str) -> bool:
    """True for ``stem`` plus a numeric suffix: ``pyto_harness 2``, ``pyto_harness3``."""
    if not name.startswith(stem):
        return False
    tail = name[len(stem) :].strip()
    return bool(tail) and tail.isdigit()


def duplicate_state_dirs(home_dir: str = "", state_dir: str = "") -> List[str]:
    """``pyto_harness 2``-style siblings of the state folder, sorted.

    This is what the Files app and iCloud produce when the same folder is saved twice: a
    second copy beside the first.  They are reported, never merged into and never deleted —
    only the user can decide which copy to keep.
    """
    roots: List[str] = []
    for root in (home_dir, os.path.dirname(state_dir) if state_dir else ""):
        if root and os.path.isdir(root) and root not in roots:
            roots.append(root)
    found: List[str] = []
    for root in roots:
        for name in _listdir(root) or []:
            if is_duplicate_name(name, STATE_DIR_NAME):
                path = os.path.join(root, name)
                if path not in found:
                    found.append(path)
    return sorted(found)


def duplicate_install_dirs() -> List[str]:
    """Copies of the install folder (``pyto-agent 2``) sitting next to it, sorted."""
    found: List[str] = []
    for directory, _label in entry_point_dirs():
        absolute = os.path.abspath(directory)
        stem = os.path.basename(absolute)
        parent = os.path.dirname(absolute)
        if not stem or not os.path.isdir(parent):
            continue
        for name in _listdir(parent) or []:
            path = os.path.join(parent, name)
            if (
                name != stem
                and is_duplicate_name(name, stem)
                and os.path.isdir(path)
                and not os.path.islink(path)
                and path not in found
            ):
                found.append(path)
    return sorted(found)


def likely_icloud_copies(home_dir: str = "", state_dir: str = "") -> List[str]:
    """The likely iCloud/Files copies worth reporting: state-folder and install copies."""
    found = duplicate_state_dirs(home_dir, state_dir)
    for path in duplicate_install_dirs():
        if path not in found:
            found.append(path)
    return sorted(found)


def stray_state_dirs(home_dir: str = "", state_dir: str = "") -> List[str]:
    """``<install>/pyto_harness`` folders that are *not* the state folder in use.

    An install that was once started from inside its own folder left its state there --
    exactly the bug this release fixes.  Such a folder usually has sessions, memory and a
    health file but no ``config.json``, so it cannot hold the key; it is named for the user
    and never touched.  A folder that *is* the state folder in use is not listed.
    """
    in_use = os.path.abspath(state_dir) if state_dir else ""
    found: List[str] = []
    for directory, _label in entry_point_dirs():
        candidate = os.path.abspath(state_dir_in(directory))
        if in_use and candidate == in_use:
            continue
        if os.path.isdir(candidate) and not os.path.islink(candidate) and candidate not in found:
            found.append(candidate)
    return sorted(found)


def duplicates_warning(copies: Sequence[str]) -> str:
    """The short, plain warning for likely iCloud/Files copies (which are never touched)."""
    lines = [
        "[warning] {} folder(s) next to the harness look like iCloud/Files copies:".format(len(copies))
    ]
    lines.extend("[warning]   {}".format(path) for path in copies)
    lines.append("[warning] the harness uses only the folder it resolved; keep one copy, and")
    lines.append("[warning] move or delete the others yourself (nothing is ever merged).")
    return "\n".join(lines)


def ambiguity_warning(chosen: str, chosen_label: str, others: Sequence[StateCandidate]) -> str:
    """Two or more candidates hold a config: name them all, say which is in use, how to switch."""
    lines = [
        "[warning] {} folders hold a {}/{}:".format(
            len(others) + 1, STATE_DIR_NAME, CONFIG_FILE_NAME
        ),
        "[warning]   in use: {} ({})".format(config_path_in(chosen), chosen_label or "chosen"),
    ]
    for item in others:
        lines.append("[warning]   also  : {} ({})".format(item.config, item.label or "candidate"))
    lines.append(
        "[warning] to use another one: {}={} (two configs are never merged)".format(
            ENV_HOME, others[0].home
        )
    )
    return "\n".join(lines)


def _warn_once(text: str) -> None:
    """Print a warning once per process (a run must not repeat itself)."""
    if not text or text in _WARNED:
        return
    _WARNED.add(text)
    try:
        print(text, file=sys.stderr)
    except Exception:  # pragma: no cover - a closed stderr must not break the run
        pass


def _write_pointer_for(source: str, home_dir: str) -> str:
    """Record an implicitly-chosen home; an explicit override needs no remembering.

    The temporary folder is never remembered (iOS can purge it), and neither are the
    explicit overrides (``PYTO_HARNESS_HOME``, the state/config variables): the user said
    what they want, and a stale pointer must not outlive that decision.
    """
    if source in (SOURCE_ENV_HOME, SOURCE_ENV_STATE, SOURCE_ENV_CONFIG, SOURCE_TEMPDIR, SOURCE_POINTER):
        return ""
    return record_pointer(home_dir)


#: Warnings already printed by :func:`_warn_once`; cleared by :func:`reset_home_cache`.
_WARNED: set = set()


def candidate_homes(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The plausible home directories, in resolver order, guessed **without writing**.

    This is the cheap guess the startup hooks use to find a legacy ``.pyto_harness``
    *before* anything resolves the home: :func:`resolve_home` proves a candidate with a
    real write probe, and that probe creates ``<home>/pyto_harness`` -- which is exactly
    what used to defeat the one-time move.  So this function reads the environment, the
    pointer file, ``argv[0]`` and the current directory and nothing else: it creates no
    directory, writes no probe file and never calls :func:`resolve_home`.

    The candidates mirror the resolver's order:

    1. ``PYTO_HARNESS_HOME`` — absolutised like the resolver does, so a relative escape
       hatch is honoured;
    2. the folder that owns ``PYTO_HARNESS_STATE_DIR`` / ``PYTO_HARNESS_CONFIG``;
    3. the remembered choice in the pointer file next to the entry point;
    4. ``HOME`` — only when it is already an absolute path (never a literal ``~``);
    5. ``os.path.expanduser("~")`` — only when it genuinely expanded;
    6. the folder above the install, then the install folder itself, then the current
       directory.

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
    try:
        override = override_home(env)
    except ConfigError:
        override = None
    if override is not None:
        remember(override[0])
    remember(read_pointer().home)
    remember(str(env.get("HOME") or ""))
    try:
        expanded = os.path.expanduser("~")
    except Exception:  # noqa: BLE001 - a hostile expanduser must not stop the guess
        expanded = ""
    if expanded and expanded != "~":
        remember(expanded)
    for directory, _label in install_parent_dirs():
        remember(directory)
    for directory, _label in entry_point_dirs():
        remember(directory)
    remember(_cwd())
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

#: Cache keyed by the values that can change the answer — the purpose, the home/state/config
#: environment, ``HOME`` and the working directory — so a changed environment re-resolves
#: while repeated calls in one run do not re-probe.
_CACHE: Dict[Tuple[str, str, str, str], HomeChoice] = {}

#: Set once the temporary-folder warning has been printed.
_WARNED_TEMPORARY = False


def _cache_key(purpose: str, environ: Mapping[str, str]) -> Tuple[str, str, str, str]:
    # The working directory is part of the key: it is a candidate, and a process that
    # chdirs (or a test that does) must not keep a stale winner or a stale failure.  The
    # pointer file is deliberately *not* in the key: resolving writes it, and a write of
    # our own must not invalidate the answer we just gave (a new process re-reads it).
    return (
        purpose,
        str(environ.get(ENV_HOME) or ""),
        str(environ.get(ENV_STATE_DIR) or ""),
        str(environ.get(ENV_CONFIG) or ""),
        str(environ.get("HOME") or ""),
        _cwd(),
    )


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
    _WARNED.clear()
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
    warnings: List[str] = []
    # Every candidate that already holds a config, in precedence order.  Pure reads, and
    # computed before the probes so the doctor can report a config that was *not* chosen.
    candidates = candidates_with_config(environ)

    def finish(
        path: str,
        source: str,
        origin: str,
        *,
        pointer: str = "",
        pointer_used: bool = False,
        temporary: bool = False,
        warning: str = "",
    ) -> HomeChoice:
        """Record the winner (remembered choice, warnings) and build the choice."""
        copies = tuple(likely_icloud_copies(path))
        parts = [item for item in ([warning] if warning else []) + warnings if item]
        if copies:
            parts.append(duplicates_warning(copies))
        text = "\n".join(parts)
        if not temporary:
            pointer = _write_pointer_for(source, path) or pointer
        _warn_once(text)
        return HomeChoice(
            path=path,
            source=source,
            note=_note(origin, skipped),
            skipped=tuple(skipped),
            temporary=temporary,
            pointer=pointer,
            pointer_used=pointer_used,
            candidates=tuple(candidates),
            duplicates=copies,
            warning=text,
        )

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
            return finish(
                candidate,
                ENV_HOME,
                "from {} (rule 1: the escape hatch)".format(ENV_HOME),
            )
        skipped.append("{0} was not writable".format(ENV_HOME))
        tried.append("{0}={1}: {2}".format(ENV_HOME, candidate, why))

    # 2. PYTO_HARNESS_STATE_DIR / PYTO_HARNESS_CONFIG: the user named the state folder (or
    #    the config file) explicitly, so the folder that owns it is the home.  Nothing else
    #    is probed: an explicit override is the answer, not a hint.
    try:
        override = override_home(environ)
    except ConfigError as exc:
        override = None
        skipped.append("the state/config override was unusable")
        tried.append(str(exc).splitlines()[0])
    if override is not None:
        candidate, source, description = override
        ok, why = _probe(candidate, create=True)
        if ok:
            return finish(
                candidate,
                source,
                "from {} (rule 2: the folder that owns the state; {})".format(candidate, description),
            )
        skipped.append("{} was not writable".format(source))
        tried.append("{}={}: {}".format(source, candidate, why))
    elif not raw_home:
        tried.append("{0}/{1} are not set".format(ENV_STATE_DIR, ENV_CONFIG))

    # 3. The remembered choice: the visible pointer file next to the entry point.
    pointer = read_pointer()
    if pointer.problem:
        skipped.append("the remembered choice was ignored")
        tried.append("{}: {}".format(pointer.path, pointer.problem))
        warnings.append(
            "[warning] ignoring the remembered choice in {}: {}\n"
            "[warning] resolving the home again; nothing was moved or deleted.".format(
                pointer.path, pointer.problem
            )
        )
    elif pointer.home:
        ok, why = _probe(pointer.home, create=False)
        if ok:
            return finish(
                pointer.home,
                SOURCE_POINTER,
                "from the remembered choice (rule 3: {})".format(pointer.path),
                pointer=pointer.path,
                pointer_used=True,
            )
        problem = "it names {}, which is not usable ({})".format(pointer.home, why)
        skipped.append("the remembered choice was ignored")
        tried.append("{}: {}".format(pointer.path, problem))
        warnings.append(
            "[warning] ignoring the remembered choice in {}: {}\n"
            "[warning] resolving the home again; nothing was moved or deleted.".format(pointer.path, problem)
        )

    # 4. An existing state folder: adopt the highest-precedence candidate that already has
    #    a config.json.  Nothing is moved, copied or merged -- the folder is simply used.
    if candidates:
        usable: List[StateCandidate] = []
        for item in candidates:
            ok, why = _probe(item.home, create=False, subdir=probe_subdir)
            if ok:
                usable.append(item)
            else:
                tried.append("{} ({}): {}".format(item.home, item.label, why))
        if usable:
            chosen = usable[0]
            others = tuple(item for item in candidates if item.config != chosen.config)
            if others:
                skipped.append(
                    "{} other candidate(s) hold {}/{}".format(len(others), STATE_DIR_NAME, CONFIG_FILE_NAME)
                )
            return finish(
                chosen.home,
                SOURCE_ADOPTED,
                "adopted {} (rule 4: it already holds {}/{})".format(
                    chosen.home, STATE_DIR_NAME, CONFIG_FILE_NAME
                ),
                warning=ambiguity_warning(chosen.home, chosen.label, others) if others else "",
            )
        skipped.append("the folder(s) holding a config were not writable")

    # 5. HOME from the environment: only when it is already an absolute path.
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
            return finish(raw_home_env, SOURCE_HOME, "from HOME (rule 5: the HOME directory)")
        skipped.append("HOME was unusable")
        tried.append("HOME={}: {}".format(raw_home_env, why))

    # 5 (continued). os.path.expanduser("~"): only when it really expanded to an absolute path.
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
                return finish(expanded, SOURCE_EXPANDUSER, "from ~ (rule 5: os.path.expanduser)")
            skipped.append("the home directory was unusable")
            tried.append("os.path.expanduser('~')={}: {}".format(expanded, why))

    # 6. The folder above the install: the default, because it does not move with the working
    #    directory and survives replacing the install folder.  Probed through the state
    #    directory that will actually be created inside it.
    for directory, label in install_parent_dirs():
        ok, why = _probe(directory, create=False, subdir=probe_subdir)
        if ok:
            return finish(
                directory,
                SOURCE_INSTALL_PARENT,
                "from {} ({}) (rule 6: the install's parent)".format(label, directory),
            )
        tried.append("{} ({}): {}".format(directory, label, why))

    # 7. The install folder itself, then the folder Pyto opened (the current directory).
    for directory, label in entry_point_dirs():
        ok, why = _probe(directory, create=False, subdir=probe_subdir)
        if ok:
            return finish(
                directory,
                SOURCE_INSTALL,
                "from {} ({}) (rule 7: the install folder)".format(
                    label, os.path.join(directory, probe_subdir)
                ),
            )
        tried.append("{} ({}): {}".format(directory, label, why))

    cwd = _cwd()
    if cwd:
        ok, why = _probe(cwd, create=False, subdir=probe_subdir)
        if ok:
            origin = "from cwd" + (" (Pyto)" if _on_pyto() else "")
            return finish(cwd, SOURCE_CWD, "{} (rule 7: the current directory)".format(origin))
        tried.append("{} (the folder Pyto opened): {}".format(cwd, why))
    else:  # pragma: no cover - only when the working directory was deleted
        tried.append("os.getcwd() failed")

    # 8. The system temp directory: works, but iOS can purge it without warning.
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
            return finish(
                candidate,
                SOURCE_TEMPDIR,
                "from {} (rule 8: TEMPORARY, iOS can purge it)".format(candidate),
                temporary=True,
            )
        tried.append("{} (temporary folder): {}".format(candidate, why))

    # 9. Nothing writable: say what to do about it, candidate by candidate.
    for item in candidates:
        tried.append("{} holds {}/{} but could not be used".format(item.home, STATE_DIR_NAME, CONFIG_FILE_NAME))
    return HomeChoice(
        error=no_home_message(tried, folder_name=probe_subdir),
        skipped=tuple(skipped),
        candidates=tuple(candidates),
        warning="\n".join(warnings),
    )


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
    root = harness_root()
    if root:
        lines.append("The harness itself is installed in {}.".format(root))
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

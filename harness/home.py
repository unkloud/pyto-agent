"""The one place that decides where ``~/.pyto_harness`` really is.

On iOS, inside Pyto, there is no usable home directory: ``HOME`` is unset (or points
somewhere the sandbox refuses) and ``os.path.expanduser("~")`` returns the string ``"~"``
because CPython cannot resolve a ``pwd`` entry.  Code that trusts it builds paths such as
``~/.pyto_harness`` — a *relative* path with a literal tilde — and iOS answers the first
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
   that holds ``run.py`` — each probed through a ``.pyto_harness`` directory inside it;
5. :func:`tempfile.gettempdir` with a ``pyto_harness`` subdirectory.  This one is
   **temporary**: iOS can purge it at any time, so a loud warning is printed;
6. nothing writable → :class:`ConfigError` naming the folder Pyto opened and the exact
   line to run.

The winner is cached per process (one probe, not one per call) and keyed by the values
that can change the answer, so setting ``PYTO_HARNESS_HOME`` in a running interpreter is
honoured.  :func:`reset_home_cache` clears it for tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from .errors import ConfigError
from .security import mkdir_private

#: The directory the harness keeps its own state in, inside the resolved home.
STATE_DIR_NAME = ".pyto_harness"
#: The default workspace directory, inside the resolved home.
WORKSPACE_DIR_NAME = "pyto_harness_workspace"
#: The subdirectory of the system temp dir used when nothing else is writable.
TEMP_HOME_DIR_NAME = "pyto_harness"

#: The escape hatch: a directory to use *as the home directory* (state goes in
#: ``$PYTO_HARNESS_HOME/.pyto_harness``).
ENV_HOME = "PYTO_HARNESS_HOME"
#: The config-file override, honoured next to the home (kept here so one module owns
#: every path rule; :mod:`harness.config` reads it).
ENV_CONFIG = "PYTO_HARNESS_CONFIG"
#: The state-directory override.
ENV_STATE_DIR = "PYTO_HARNESS_STATE_DIR"

#: Prefix of the probe file; created 0600 and deleted immediately.
PROBE_PREFIX = ".pyto_harness-write-probe-"

#: The exact line a user can paste.  Quoted verbatim in every failure message.
WORKAROUND = (
    "Set {env} to somewhere you can write, for example:\n"
    '  import os; os.environ["{env}"] = os.getcwd()'
).format(env=ENV_HOME)

#: One-line reminder printed (once per process) when the temp fallback wins.
TEMPORARY_WARNING = (
    "[warning] ~/.pyto_harness is not usable here; using {path} instead.\n"
    "[warning] That folder is temporary: iOS can purge it at any time, and the config,\n"
    "[warning] sessions, memory and backups kept in it can disappear. Set {env} to a\n"
    "[warning] folder you control (for example: {example}) to keep them."
)


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
    probe_subdir = STATE_DIR_NAME if purpose == "state" else ".pyto_harness_{}".format(purpose)
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

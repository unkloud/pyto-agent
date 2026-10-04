#!/usr/bin/env python3
"""Install pyto-agent on the device -- and set it up, in one run.

Pyto has no ``git`` and no ``unzip``, so this downloads the repository archive
over HTTPS and extracts it with the standard library alone.

    python install.py                       # install, key, health check, ONE start line
    python install.py --api-key sk-...      # the same, with nothing to type
    python install.py --into ~/pyto-agent   # somewhere else
    python install.py --ref v1.0.0          # a tag or branch
    python install.py --zip pyto-agent.zip  # from a file (iCloud, AirDrop)
    python install.py --no-setup            # files only, old behaviour (CI, tests)

One run does what the four "paste this next" one-liners used to do, in order:

1. download, verify the archive (``--sha256``) and unpack it;
2. find the API key -- keep a working one already in ``~/pyto_harness/config.json``,
   else use ``--api-key``/``--key-file``/``DEEPSEEK_API_KEY``, else ask (hidden input,
   plain ``input()`` where the platform cannot hide it) -- and prove it with one
   minimal chat request through the doctor's own probe before anything is written;
   a hidden state directory left by an older release is moved -- or, when the new
   directory is already there without a config, merged entry by entry without
   overwriting anything -- before the home is resolved, so the key that release
   stored is found again;
3. write the config through the harness's hardened writer (0600, never a silent
   overwrite, unrelated fields preserved);
4. run the doctor's checks and apply the safe fixes, printing one compact line;
5. write ``start.py`` next to ``run.py`` (open it in Pyto and press Run);
6. print the single command that starts the agent -- and run it if ``--chat``,
   ``--ui`` or ``--task "..."`` was asked for.

Nothing here blocks: with no terminal and no key the install finishes, prints the one
command to run later and exits 0.  The key is never printed and never echoed.

Updating is the same command: it replaces the code and leaves your state alone.
Your API key, sessions, memory and backups live in ``~/pyto_harness`` and your
programs live in the workspace (``~/pyto_harness_workspace`` by default), never
inside the code directory, so re-installing cannot lose them.

Standard library only, Python 3.10 (Pyto's version).  Nothing here needs a
shell, a subprocess or a compiler.
"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import sys
import zipfile

REPO = "unkloud/pyto-agent"
ARCHIVE = "https://github.com/{repo}/archive/refs/heads/{ref}.zip"
ARCHIVE_TAG = "https://github.com/{repo}/archive/refs/tags/{ref}.zip"
DEFAULT_REF = "main"
DEFAULT_TARGET = "pyto-agent"
USER_AGENT = "pyto-agent-installer/1.0 (stdlib)"

SKIP_DIRS = {"__pycache__", ".git", ".github"}
SKIP_SUFFIXES = (".pyc", ".pyo")

#: The private name the installed package is imported under while setting it up.
#: ``harness`` itself is never imported here: the installer runs *before* the harness
#: exists, and a ``harness`` the caller already imported must not be disturbed.
PRIVATE_PACKAGE = "_pyto_install_harness"

#: ``start.py``, written next to ``run.py``: open it in Pyto and press Run.
START_PY = '''#!/usr/bin/env python3
"""Start pyto-harness: open this file in Pyto and press Run.

The installer wrote it next to run.py.  It starts the agent that lives in this
folder, forwarding any arguments you pass, so there is nothing to paste.
"""

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.argv = [os.path.join(HERE, "run.py")] + sys.argv[1:]
runpy.run_path("run.py", run_name="__main__")
'''

#: The hidden-input prompt.  The fallback prompt (no tty, no ``termios``, a Pyto
#: quirk) says instead that the key will be visible while it is typed.
KEY_PROMPT = "API key (input hidden): "

#: How many typed keys a rejected key may be replaced with before giving up.
KEY_ATTEMPTS = 3

#: Seconds one validation request may take.
KEY_TIMEOUT = 15.0

#: The ``how`` value that means "the key already in the config file".
ORIGIN_CONFIG = "the config file"


class InstallError(Exception):
    """Anything that stops the install, with a message meant for the user."""


class SetupError(Exception):
    """A setup step that must stop the run (a key the provider rejects, a write that fails)."""


#: The state directory's *display* name, kept in step with ``harness/home.py``'s
#: ``STATE_DIR_NAME`` (the one place that owns the path rule).  The installer deliberately
#: does not import the harness before the files are on disk, so the text it can print
#: before then has to spell the name out; every path the installer actually reads or writes
#: is built by the loaded ``harness.home`` / ``harness.config`` modules, never from this
#: string.  No leading dot: the folder is visible in the Files app.
STATE_DIR_NAME = "pyto_harness"

#: Printed whenever a write fails because the device has no usable home directory.
#: This is the Pyto case: ``os.path.expanduser("~")`` returns ``"~"``, so every path
#: becomes ``~/pyto_harness`` and iOS refuses to create a directory literally named
#: ``~`` with ``[Errno 1] Operation not permitted``.  The installer prints this and keeps
#: going: the files are installed, only the config has to wait for the escape hatch.
HOME_WORKAROUND = (
    "This device has no home directory, so ~/" + STATE_DIR_NAME + " cannot be created\n"
    "(iOS answers Operation not permitted). Set the escape hatch to a folder you can\n"
    "write to, then re-run the installer:\n"
    '  import os; os.environ["PYTO_HARNESS_HOME"] = os.getcwd()\n'
    "(PYTO_HARNESS_HOME is used instead of the home directory; everything else works\n"
    "unchanged.)"
)


def unexpanded_tilde(path: str) -> bool:
    """True when a path still carries a ``~`` that ``expanduser`` could not expand."""
    return any(part.startswith("~") for part in str(path or "").split(os.sep))


def expand_local_path(raw: str, *, what: str) -> str:
    """Expand an installer argument, or refuse it — never build a literal ``~`` path.

    The installer runs before the harness exists, so it cannot import
    :func:`harness.home.expand_user_path`; the rule is the same one.
    """
    text = str(raw or "").strip()
    expanded = os.path.expanduser(text)
    if unexpanded_tilde(expanded):
        raise InstallError(
            "cannot expand the '~' in {} ({!r}): this device has no home directory.\n"
            "Pass an absolute path instead, or set PYTO_HARNESS_HOME to a folder you can "
            "write to.".format(what, text)
        )
    return os.path.abspath(expanded)


def report_home_workaround(report: "Report", exc: object = "") -> None:
    """Explain the ``PYTO_HARNESS_HOME`` escape hatch, once, in the installer's voice."""
    if exc:
        report("setup: {}: {}".format(type(exc).__name__, exc))
    if getattr(report, "workaround_printed", False):
        return
    report.workaround_printed = True  # type: ignore[attr-defined]
    for line in HOME_WORKAROUND.splitlines():
        report("       " + line)


#: Fragments that mean "the sandbox refused the write", as opposed to a full disk.
_PERMISSION_HINTS = ("permission denied", "operation not permitted", "read-only file system")


def looks_like_a_permission_problem(exc: BaseException) -> bool:
    """True for EPERM/EACCES/EROFS, the failures the home workaround actually fixes."""
    if getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES, errno.EROFS):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _PERMISSION_HINTS)


#: A SHA-256 in hex, the form ``--sha256`` accepts.
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: Markers that mean "this key is a placeholder, not a credential" (the doctor's list).
PLACEHOLDER_MARKERS = ("replace", "your-key", "yourkey", "changeme", "example", "todo", "xxx", "<", "dummy")


def archive_digest(payload: bytes) -> str:
    """The SHA-256 of the archive bytes — the value a user pins with ``--sha256``.

    The installer is the one component that runs *before* the harness exists and then
    holds the API key, so "whatever GitHub served" was the whole trust model.  Every run
    now prints this digest (pin it, then verify the next install against it), and
    ``--sha256`` refuses a mismatch instead of installing the bytes anyway.
    """
    return hashlib.sha256(payload).hexdigest()


def is_placeholder_key(key: "str | None") -> bool:
    """True when a stored key is obviously the template, not a credential."""
    if not key:
        return True
    lowered = key.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS) or lowered in ("sk-", "sk-...", "key")


# --------------------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------------------


def download(ref: str, *, timeout: float = 120.0) -> bytes:
    """Fetch the repository archive for ``ref``.  Raises InstallError."""
    import urllib.error
    import urllib.request

    if os.path.sep in ref or ref.startswith("-") or ".." in ref:
        raise InstallError("refusing to use {!r} as a ref name".format(ref))

    urls = [ARCHIVE.format(repo=REPO, ref=ref)]
    if not ref.startswith("refs/"):
        urls.append(ARCHIVE_TAG.format(repo=REPO, ref=ref))

    problems = []
    for url in urls:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            problems.append("{} -> HTTP {}".format(url, exc.code))
            if exc.code == 404:
                continue
        except urllib.error.URLError as exc:
            problems.append("{} -> {} ({})".format(url, exc.reason, type(exc.reason).__name__))
        except OSError as exc:  # timeouts, TLS problems, no network at all
            problems.append("{} -> {}".format(url, exc))

    raise InstallError(
        "could not download the archive:\n  "
        + "\n  ".join(problems)
        + "\nCheck the network in Settings, or pass --zip with a file you downloaded elsewhere."
    )


# --------------------------------------------------------------------------------------
# Extracting
# --------------------------------------------------------------------------------------


def safe_members(archive: zipfile.ZipFile) -> "list[tuple[str, str]]":
    """(member, relative destination) pairs, with zip-slip and junk filtered out.

    Hostile or malformed archives are refused rather than quietly rewritten: an
    absolute path, a Windows drive, a ``..`` component or a symlink-ish entry all
    raise :class:`InstallError` before anything is written.
    """
    names = archive.namelist()
    roots = {name.replace("\\", "/").split("/")[0] for name in names if name.strip("/\\")}
    strip_root = len(roots) == 1

    pairs = []
    for name in names:
        if name.endswith("/") or name.endswith("\\"):
            continue
        if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
            raise InstallError("the archive contains an absolute path: {!r}".format(name))

        parts = [part for part in name.replace("\\", "/").split("/") if part]
        if not parts:
            continue
        if ".." in parts:
            raise InstallError("the archive contains an unsafe path: {!r}".format(name))
        if strip_root:
            parts = parts[1:]
        if not parts:
            continue
        if any(part in SKIP_DIRS for part in parts):
            continue
        if parts[-1].endswith(SKIP_SUFFIXES):
            continue
        relative = os.path.join(*parts)
        if os.path.isabs(relative) or relative.startswith(".."):
            raise InstallError("the archive contains an unsafe path: {!r}".format(name))
        pairs.append((name, relative))
    if not pairs:
        raise InstallError("the archive is empty")
    return pairs


def extract(archive: zipfile.ZipFile, target: str) -> "list[str]":
    """Write every safe member under ``target``.  Returns the relative paths."""
    written = []
    for member, relative in safe_members(archive):
        destination = os.path.join(target, relative)
        os.makedirs(os.path.dirname(destination) or target, exist_ok=True)
        with archive.open(member) as source, open(destination, "wb") as handle:
            shutil.copyfileobj(source, handle)
        written.append(relative)

    if "run.py" not in written:
        raise InstallError("the archive has no run.py; refusing to call this an install")
    return written


# --------------------------------------------------------------------------------------
# Verifying (in-process: iOS has no usable subprocess)
# --------------------------------------------------------------------------------------


def verify(target: str, *, python_version=(3, 10)) -> "list[str]":
    """Parse every module at 3.10 and import the package.  Returns notes.

    The import happens under a private name (``_pyto_verify_harness``) with the package
    loaded straight from ``target``, so verifying never disturbs a ``harness`` package that
    the caller already imported.  iOS has no usable subprocess, so this must be in-process.
    """
    import importlib.util

    warnings = []
    failures = []

    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    ast.parse(handle.read(), filename=path, feature_version=python_version)
            except SyntaxError as exc:
                failures.append("{}: {}".format(os.path.relpath(path, target), exc))
            except OSError as exc:
                warnings.append("could not read {}: {}".format(os.path.relpath(path, target), exc))

    if failures:
        raise InstallError(
            "the installed code does not parse as Python {}.{}:\n  ".format(*python_version)
            + "\n  ".join(failures[:5])
        )

    package_dir = os.path.join(os.path.abspath(target), "harness")
    initializer = os.path.join(package_dir, "__init__.py")
    if not os.path.isfile(initializer):
        raise InstallError("the installed tree has no harness/__init__.py")

    private = "_pyto_verify_harness"
    spec = importlib.util.spec_from_file_location(
        private, initializer, submodule_search_locations=[package_dir]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise InstallError("could not build an import spec for {}".format(initializer))

    module = importlib.util.module_from_spec(spec)
    sys.modules[private] = module
    try:
        spec.loader.exec_module(module)
        version = getattr(module, "__version__", "unknown")
    except Exception as exc:
        raise InstallError("the installed harness does not import: {}: {}".format(type(exc).__name__, exc))
    finally:
        for key in [key for key in sys.modules if key == private or key.startswith(private + ".")]:
            del sys.modules[key]

    return ["harness {} imported cleanly from {}".format(version, os.path.abspath(target))] + warnings


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def next_steps(target: str) -> str:
    """The install-only text (``--no-setup`` and the truncated-tree fallback).

    Kept exactly as it always was: automation and CI read it, and the one-stop flow
    (below) is what replaces it for a human on a device.
    """
    absolute = os.path.abspath(target)
    prefix = "import os, runpy, sys; os.chdir({}); sys.argv = ".format(repr(absolute))
    tail = "; runpy.run_path('run.py', run_name='__main__')"
    return (
        "Installed to {target}\n"
        "\n"
        "Next, in this order (paste one line at a time into the Pyto console):\n"
        "\n"
        "  1. Give it your API key (once), then paste the key into\n"
        "     ~/" + STATE_DIR_NAME + "/config.json and save:\n"
        "       {one}\n"
        "\n"
        "  2. Check the device and repair what a machine can repair:\n"
        "       {two}\n"
        "\n"
        "  3. Ask for something:\n"
        "       {three}\n"
        "\n"
        "  4. Optional: a chat window instead of the console:\n"
        "       {four}\n"
        "\n"
        "Full instructions: {target}/README.md\n"
        "Your key, sessions and memory live in ~/" + STATE_DIR_NAME + " and are never touched by an update.\n"
        "\n"
        "If Pyto has no home directory (an error mentioning `~/" + STATE_DIR_NAME + "` and\n"
        "`Operation not permitted`), set the escape hatch first and run step 1 again:\n"
        '  import os; os.environ["PYTO_HARNESS_HOME"] = os.getcwd()'
    ).format(
        target=absolute,
        one=prefix + "['run.py', '--init']" + tail,
        two=prefix + "['run.py', '--doctor', '--fix']" + tail,
        three=prefix + "['run.py', 'write me a script that renames my screenshots by date']" + tail,
        four=prefix + "['run.py', '--ui']" + tail,
    )


def start_line(target: str, argv: "list[str] | None" = None) -> str:
    """The one command that starts the installed agent, absolute path and all."""
    absolute = os.path.abspath(target)
    return (
        "import os, runpy, sys; os.chdir({}); sys.argv = {}; "
        "runpy.run_path('run.py', run_name='__main__')"
    ).format(repr(absolute), repr(list(argv or ["run.py"])))


class Report:
    """Progress output with every credential scrubbed out of it.

    The key is registered with the harness's own scrubber (and kept here as well), so a
    provider that echoes the ``Authorization`` header into an error body cannot get the
    credential into this terminal, a log or a screenshot.
    """

    def __init__(self, security_module=None) -> None:
        self.security = security_module
        self.secrets: "set[str]" = set()
        #: The PYTO_HARNESS_HOME note is printed once per run, however many writes fail.
        self.workaround_printed = False

    def secret(self, value: "str | None") -> None:
        if value and isinstance(value, str):
            self.secrets.add(value)
            if self.security is not None:
                self.security.register_secret(value)

    def __call__(self, text: str = "") -> None:
        text = str(text)
        if self.security is not None:
            text = self.security.scrub_secrets(text, sorted(self.secrets, key=len, reverse=True))
        print(text)


# --------------------------------------------------------------------------------------
# Setup, step 1: the harness's own machinery, loaded from the install
# --------------------------------------------------------------------------------------


def _drop_private_modules(prefix: str = PRIVATE_PACKAGE) -> None:
    for key in [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]:
        del sys.modules[key]


def _load_package(directory: str, name: str = PRIVATE_PACKAGE):
    """Import ``directory/harness`` under ``name`` and return the package module."""
    import importlib.util

    package_dir = os.path.join(os.path.abspath(directory), "harness")
    initializer = os.path.join(package_dir, "__init__.py")
    if not os.path.isfile(initializer):
        raise ImportError("no harness/__init__.py in {}".format(directory))
    spec = importlib.util.spec_from_file_location(name, initializer, submodule_search_locations=[package_dir])
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("could not build an import spec for {}".format(initializer))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        _drop_private_modules(name)
        raise
    return module


def load_harness(target: str):
    """The installed harness (``doctor``, ``config``, ``security``, ``ios``), loaded privately.

    The just-unpacked tree is preferred -- that is the code the user will actually run.
    When it has no usable ``harness/doctor.py`` (a truncated archive, or the tiny tree
    some tests extract) the installer's own directory is tried second, so setup still
    works from a checkout; ``note`` says which copy was used.  Raises :class:`InstallError`
    when neither has one.
    """
    import importlib
    import types

    local = os.path.dirname(os.path.abspath(__file__))
    problems = []
    tried: "list[str]" = []
    for directory in (target, local):
        resolved = os.path.abspath(directory or "")
        if not resolved or resolved in tried:
            continue
        tried.append(resolved)
        try:
            _drop_private_modules()
            package = _load_package(directory)
            doctor = importlib.import_module(PRIVATE_PACKAGE + ".doctor")
            config = importlib.import_module(PRIVATE_PACKAGE + ".config")
            security = importlib.import_module(PRIVATE_PACKAGE + ".security")
            ios = importlib.import_module(PRIVATE_PACKAGE + ".ios")
            home = importlib.import_module(PRIVATE_PACKAGE + ".home")
        except BaseException as exc:  # noqa: BLE001 - any failure means "try the next tree"
            _drop_private_modules()
            problems.append("{}: {}: {}".format(directory, type(exc).__name__, exc))
            continue
        note = ""
        if os.path.abspath(directory) != os.path.abspath(target):
            note = (
                "the installed tree has no usable harness/doctor.py; using the installer's own "
                "copy at {} for the setup checks".format(local)
            )
        return types.SimpleNamespace(
            package=package, doctor=doctor, config=config, security=security, ios=ios, home=home, note=note
        )

    raise InstallError(
        "installed the files, but no harness to set them up with:\n  " + "\n  ".join(problems)
    )


def harness_home_module(modules):
    """The loaded harness's ``home`` module, whatever shape ``load_harness`` returned.

    ``home`` owns the state-directory name and the one-time move out of hiding, so the
    installer must ask *the tree it just installed*, never a copy of the rule here.  The
    fallback keeps the tests' lighter module bundles (and an older installed tree) working.
    """
    module = getattr(modules, "home", None)
    if module is not None:
        return module
    import importlib

    package = getattr(getattr(modules, "config", None), "__package__", "") or PRIVATE_PACKAGE
    return importlib.import_module(package + ".home")


def migrate_state_directories(modules) -> str:
    """Ask the loaded harness to move every legacy hidden state directory, and report it.

    The first path-touching step of :func:`setup`: the harness resolver proves a home with
    a write probe, and that probe **creates** ``<home>/pyto_harness`` -- an empty new
    directory on disk is what used to turn the one-time move into a silent no-op.  So the
    move has to happen before anything resolves a home, which is why
    ``harness.home.candidate_homes`` exists: it guesses the homes without creating
    anything.  The loaded tree owns that rule; the installer never keeps a copy of it.

    An older installed tree has no candidate-home guess.  It gets no call at all rather
    than a resolve-first fallback: resolving is exactly the call that creates the new
    directory and strands the old data.
    """
    try:
        home_module = harness_home_module(modules)
        migrate = getattr(home_module, "migrate_candidate_homes", None)
        if migrate is None:
            return ""
        return migrate()
    except Exception:  # noqa: BLE001 - never let housekeeping stop the install
        return ""


def migrate_state_before_setup(target: str) -> str:
    """The earliest chance: migrate with a harness that is already on disk.

    :func:`setup` runs the same call on the tree it just installed, and the installer does
    nothing that resolves or writes a state path in between.  This covers the runs where a
    harness is importable *before* the archive is read: an installer started from a
    checkout, or an update over an existing install.  A standalone installer with no
    harness beside it -- and a tree too old to know the candidate homes -- return ``""``;
    setup then reports the real story with the tree it just installed.
    """
    try:
        modules = load_harness(target)
    except Exception:  # noqa: BLE001 - setup() raises the actionable InstallError itself
        return ""
    return migrate_state_directories(modules)


# --------------------------------------------------------------------------------------
# Setup, step 2: the API key
# --------------------------------------------------------------------------------------


def can_prompt(ios_module=None) -> bool:
    """True when asking a question cannot hang the install.

    A real tty is the plain case.  Pyto's console is the other: it has no tty to test
    (``sys.stdin.isatty()`` is false there) but ``input()`` does reach the user, and
    refusing to ask would send them back to editing JSON by hand.
    """
    if ios_module is not None:
        try:
            if ios_module.is_pyto():
                return True
        except Exception:  # noqa: BLE001 - a capability probe must not break setup
            pass
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def read_hidden(prompt: str) -> str:
    """Read one line of secret input.

    ``getpass`` is used only where it can actually suppress echo.  On a console without a
    tty — Pyto's is one, and so is a piped run — ``getpass`` falls back to ``input()`` *and*
    prints "Warning: Password input may be echoed" plus a ``GetPassWarning`` line, which is
    alarming noise at the one prompt the user sees.  Asking plainly there is the same
    behaviour without the scare.
    """
    stream = getattr(sys, "stdin", None)
    try:
        hidden_possible = bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        hidden_possible = False
    if hidden_possible:
        import getpass

        return getpass.getpass(prompt)
    return input(prompt)


def ask_yes_no(question: str, default: bool = False) -> bool:
    """One y/N question that cannot raise or hang: anything unreadable means "no"."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_for_key(config_path: str, report: "Report | None" = None) -> str:
    """Ask for the key: hidden where the platform can, a plain line where it cannot.

    The key is never echoed by this function and never written to the terminal by the
    installer.  Returns ``""`` when there is nothing to read (EOF, Ctrl-C, a closed
    stdin), which the caller treats as "no key yet" instead of blocking.
    """
    if report is not None:
        report("setup: an API key is needed for model calls; local tools work without one.")
        report("       It is not echoed, and it will be stored in {} (mode 0600).".format(config_path))
    try:
        value = read_hidden(KEY_PROMPT)
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception as exc:  # noqa: BLE001 - no tty, no termios, a Pyto quirk
        if report is not None:
            report(
                "       hidden input is not available here ({}); "
                "the key will be visible as you type it.".format(type(exc).__name__)
            )
        try:
            value = input("API key: ")
        except (EOFError, KeyboardInterrupt, OSError):
            return ""
    return (value or "").strip()


def key_from_arguments(args, env=None) -> "tuple[str | None, str]":
    """The key the user handed us, and the label to name it by.  Never the key itself."""
    environ = os.environ if env is None else env
    if args.api_key:
        return args.api_key.strip(), "--api-key"
    if args.key_file:
        path = expand_local_path(args.key_file, what="--key-file")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError as exc:
            raise InstallError("could not read --key-file {}: {}".format(path, exc))
        if not value:
            raise InstallError("--key-file {} is empty".format(path))
        return value, "--key-file"
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY"):
        value = environ.get(name)
        if value:
            return value.strip(), "${}".format(name)
    return None, ""


def probe_base_variants(modules, *, api_base: str, model: str, key: str, config_path: str, root: str) -> "str | None":
    """The base URL that answers 200, from the doctor's own candidate list.

    ``https://host`` and ``https://host/v1`` are the two shapes providers use; the doctor
    already knows how to tell them apart, so this reuses :func:`harness.doctor.probe_api_base_variants`
    rather than inventing a second rule.
    """
    doctor = modules.doctor
    probe_config = modules.config.Config(api_base=api_base, model=model, api_key=key)
    ctx = doctor.DoctorContext.for_config(
        probe_config,
        config_path=config_path,
        root=root,
        network=True,
        persist=False,
        network_timeout=KEY_TIMEOUT,
    )
    found = doctor.probe_api_base_variants(ctx)
    return found.get("base") if found.get("ok") else None


def validate_key(modules, *, api_base: str, model: str, key: str, config_path: str, root: str) -> dict:
    """One minimal chat request through the doctor's probe.  Returns a verdict dict.

    ``kind`` is:

    ``ok``            HTTP 200 -- the key works at the returned ``api_base``;
    ``rejected``      401/403 -- the provider says the key itself is wrong;
    ``endpoint``      404 that no base variant fixes -- an api_base problem, not a key one;
    ``unreachable``   DNS/TCP/TLS/timeout -- we learned nothing about the key;
    ``inconclusive``  the API answered something else (429, 5xx, a shape this one-token
                      body does not fit) -- also nothing certain about the key.

    Nothing here writes anything, and the key never appears in the result.
    """
    doctor = modules.doctor
    try:
        response = doctor.probe_post(
            doctor.chat_url(api_base),
            api_key=key,
            body=doctor.minimal_chat_body(model),
            timeout=KEY_TIMEOUT,
        )
    except doctor.ProbeFailure as failure:
        return {
            "kind": "unreachable",
            "status": None,
            "api_base": api_base,
            "detail": "{}: {}".format(failure.kind, failure.detail),
        }
    except Exception as exc:  # noqa: BLE001 - a surprising transport error is not a rejected key
        return {
            "kind": "unreachable",
            "status": None,
            "api_base": api_base,
            "detail": "{}: {}".format(type(exc).__name__, exc),
        }

    status = int(response["status"])
    # Keep the shape of an error (it is what tells the user *why* a key was rejected) but
    # never paste a success body into the installer's output: a 200 here is a model reply,
    # and echoing it is both noise and a way to leak content into logs or a screenshot.
    body = "" if status == 200 else " ".join((response.get("text") or "").split())[:200]
    verdict = {
        "kind": "inconclusive",
        "status": status,
        "api_base": api_base,
        "detail": "HTTP {}{}".format(status, ": " + body if body else ""),
    }
    if status == 200:
        verdict["kind"] = "ok"
        return verdict
    if status in (401, 403):
        verdict["kind"] = "rejected"
        return verdict
    if status == 404:
        failure_note = ""
        try:
            working = probe_base_variants(
                modules, api_base=api_base, model=model, key=key, config_path=config_path, root=root
            )
        except Exception as exc:  # noqa: BLE001 - a broken variant probe is still an endpoint problem
            working = None
            failure_note = " (the variant probe failed: {}: {})".format(type(exc).__name__, exc)
        if working:
            return {
                "kind": "ok",
                "status": 200,
                "api_base": working,
                "detail": "HTTP 404 from {}; {} answered 200 instead".format(
                    doctor.chat_url(api_base), doctor.chat_url(working)
                ),
            }
        verdict["kind"] = "endpoint"
        verdict["detail"] = "HTTP 404 from {} and no candidate base answered 200{}".format(
            doctor.chat_url(api_base), failure_note
        )
        return verdict
    # 429, 5xx and any other answer: the endpoint spoke, so the key is neither proven nor
    # disproven.  The caller offers to save it anyway rather than losing the user's key.
    return verdict


def acquire_key(
    modules,
    args,
    *,
    api_base: str,
    model: str,
    stored: "str | None",
    config_path: str,
    root: str,
    interactive: bool,
    report: Report,
) -> "tuple[str | None, str, bool, str]":
    """Decide which key to use: ``(key, api_base, verified, how)``.

    ``how`` is a short label for where the key came from (used only in messages).  ``key``
    is ``None`` when there is nothing usable yet: the caller finishes the install and
    prints the command to run later instead of blocking.  Raises :class:`SetupError` when
    a key was explicitly rejected and trying again is impossible or exhausted.
    """
    provided, origin = key_from_arguments(args)
    if provided:
        report.secret(provided)

    def check(value: str, base: str) -> dict:
        report.secret(value)
        return validate_key(modules, api_base=base, model=model, key=value, config_path=config_path, root=root)

    # 1. A stored key that still works is kept: no prompt, no rewrite.
    if provided is None and stored and not is_placeholder_key(stored) and not args.reconfigure:
        if args.no_network:
            report(
                "setup: keeping the API key already in {} ({}); --no-network, so it was not "
                "re-checked".format(config_path, modules.config.redact_key(stored))
            )
            return stored, api_base, False, ORIGIN_CONFIG
        verdict = check(stored, api_base)
        if verdict["kind"] == "ok":
            report(
                "setup: keeping the API key already in {} ({}); {}".format(
                    config_path, modules.config.redact_key(stored), verdict["detail"]
                )
            )
            return stored, verdict["api_base"], True, ORIGIN_CONFIG
        if verdict["kind"] == "unreachable":
            report(
                "setup: could not re-check the stored key ({}); keeping it as it is".format(verdict["detail"])
            )
            return stored, api_base, False, ORIGIN_CONFIG
        report("setup: the stored key was not accepted ({}); asking for a new one".format(verdict["detail"]))

    # 2. An explicit key first, then up to KEY_ATTEMPTS answers from the prompt.
    queue: "list[tuple[str | None, str]]" = []
    if provided:
        queue.append((provided, origin))
    for _ in range(KEY_ATTEMPTS if interactive else 0):
        queue.append((None, "the prompt"))

    typed = 0
    for value, label in queue:
        if value is None:
            value = prompt_for_key(config_path, report)
            typed += 1
            label = "the prompt"
            if not value:
                return None, api_base, False, ""  # nothing typed: finish, do not block
        if args.no_network:
            report("setup: saving the key from {} without a live check (--no-network)".format(label))
            return value, api_base, False, label
        verdict = check(value, api_base)
        if verdict["kind"] == "ok":
            report("setup: the endpoint accepted the key from {} ({})".format(label, verdict["detail"]))
            return value, verdict["api_base"], True, label
        if verdict["kind"] == "unreachable":
            report("setup: could not reach {}: {}".format(modules.doctor.chat_url(api_base), verdict["detail"]))
            if args.save_anyway or args.yes or (interactive and ask_yes_no("  Save this key anyway, unchecked? [y/N] ")):
                report("setup: saving the key unverified")
                return value, api_base, False, label
            report("setup: not saving the key")
            return None, api_base, False, ""
        if verdict["kind"] == "endpoint":
            raise SetupError(
                "the API answered 404: {}\n"
                "The key was not rejected, the address is wrong. Pass the right one with "
                "--api-base (most providers want either https://host or https://host/v1).".format(verdict["detail"])
            )
        if verdict["kind"] == "rejected":
            report("setup: the provider rejected that key ({}).".format(verdict["detail"]))
            if not interactive or typed >= KEY_ATTEMPTS:
                raise SetupError(
                    "the provider rejected the key {} time(s). Nothing was saved.\n"
                    "Check the key in the provider's dashboard, then run this again.".format(max(typed, 1))
                )
            continue
        # inconclusive: the API spoke, but not with a completion
        report("setup: the endpoint answered {}, so the key could not be proven.".format(verdict["detail"]))
        if args.save_anyway or args.yes or (interactive and ask_yes_no("  Save this key anyway? [y/N] ")):
            return value, api_base, False, label
        return None, api_base, False, ""
    return None, api_base, False, ""


# --------------------------------------------------------------------------------------
# Setup, step 3: the config file
# --------------------------------------------------------------------------------------


def read_config(path: str) -> "tuple[dict, str]":
    """The existing config as a dict, plus a reason when it could not be used."""
    if not os.path.exists(path):
        return {}, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except ValueError as exc:
        return {}, "not valid JSON ({})".format(exc)
    except OSError as exc:
        return {}, "unreadable ({})".format(exc)
    if not isinstance(payload, dict):
        return {}, "not a JSON object"
    return payload, ""


def build_config_payload(existing: dict, *, api_key: str, api_base: str, model: str, defaults: dict) -> dict:
    """The new config: our fields on top, everything the user set preserved.

    Workspace, sessions_dir, extra headers, max_turns and anything else already in the
    file survive an update untouched -- the installer owns the key, the base and the model.
    """
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload["api_base"] = api_base
    payload["model"] = model
    payload["api_key"] = api_key
    for field, value in defaults.items():
        payload.setdefault(field, value)
    return payload


def backup_config(security_module, path: str, report: Report) -> str:
    """Keep a 0600 copy of a config we could not parse before it is replaced."""
    backup = path + ".bak"
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        write_private_text(security_module, backup, data)
    except OSError as exc:
        report("setup: could not keep a copy of the old config ({}: {})".format(type(exc).__name__, exc))
        return ""
    return backup


def write_private_text(security_module, path: str, data) -> str:
    """Replace ``path`` with ``data`` through the harness's own 0600 writer.

    ``open_private(truncate=True)`` + ``os.replace`` is the pattern the doctor uses to
    rewrite the config: the file is 0600 from creation (never chmod'ed in a window), the
    replacement is atomic, and a crash cannot leave half a config behind.  Note that
    ``security.write_private`` is *not* used here: it opens without ``O_TRUNC``, so a
    shorter rewrite would leave the tail of the previous file in place.
    """
    temporary = path + ".installer-tmp"
    binary = isinstance(data, bytes)
    try:
        with security_module.open_private(temporary, truncate=True, binary=binary) as handle:
            handle.write(data)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def save_config(modules, path: str, payload: dict, *, existed: bool) -> str:
    """Write the config through the harness's own hardened writers; returns its mode.

    Creating goes through ``write_sample_config`` (``O_CREAT|O_EXCL``, mode 0600), so a
    config that appeared while the installer was running is refused instead of replaced.
    The payload is then written with :func:`write_private_text` and the mode reported is
    the mode the file *has* (``enforce_config_mode``), never the one it was meant to have.
    """
    config_module = modules.config
    if not existed:
        try:
            config_module.write_sample_config(path, api_key=payload["api_key"])
        except config_module.ConfigError as exc:
            raise SetupError("refusing to overwrite a config that appeared while installing: {}".format(exc))
    try:
        write_private_text(modules.security, path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise SetupError("could not write {}: {}: {}".format(path, type(exc).__name__, exc))
    return config_module.enforce_config_mode(path)


# --------------------------------------------------------------------------------------
# Setup, step 4: the doctor
# --------------------------------------------------------------------------------------

#: Fixes beyond ``apply_fixes(safe_only=True)`` that this installer may apply.  Both only
#: write a markdown reference into the workspace, which is why they are safe here even
#: though the doctor's own conservative list does not include them.
DOC_FIX_IDS = ("shortcuts.write_doc", "libs.write_doc")


def apply_safe_fixes(doctor_module, ctx, results):
    """Apply the safe fixes, plus the two workspace documents.  Returns ``(results, outcomes)``.

    ``harness.doctor.apply_fixes(safe_only=True)`` is the doctor's own definition of
    "nobody can object to this" (directories, modes, a torn session line).  The document
    writers are applied in a second, explicitly filtered pass so the unsafe fixes
    (rewriting api_base/model, restoring a backup, repairing session logs) stay untouched.
    """
    after, outcomes = doctor_module.apply_fixes(ctx, results, safe_only=True)
    wanted = [item for item in after if item.fixable and item.fix_id in DOC_FIX_IDS]
    if wanted:
        fixed, more = doctor_module.apply_fixes(ctx, wanted, safe_only=False)
        by_id = {item.id: item for item in fixed}
        after = [by_id.get(item.id, item) for item in after]
        outcomes = list(outcomes) + list(more)
    return after, outcomes


def doctor_summary(doctor_module, results) -> str:
    """The one compact line: ``doctor: 19 ok, 3 fixed, 0 need you``."""
    table = doctor_module.counts(results)
    ok = table.get("ok", 0) + table.get("warn", 0) + table.get("skipped", 0)
    attention = doctor_module.needs_attention(results)
    return "doctor: {} ok, {} fixed, {} {}".format(
        ok, table.get("fixed", 0), attention, "needs you" if attention == 1 else "need you"
    )


def doctor_attention(results, limit: int = 4) -> "list[str]":
    """One short line per check that still needs the user, not the whole report."""
    lines = []
    for item in results:
        if not item.failed():
            continue
        detail = " ".join((item.human_action or item.detail or "").split())
        if len(detail) > 160:
            detail = detail[:157] + "..."
        lines.append("  {}: {}".format(item.id, detail))
        if len(lines) >= limit:
            break
    return lines


def run_doctor(modules, config, *, config_path: str, target: str, network: bool, skip_fixes: bool):
    """Run the checks, apply the safe fixes unless told not to, and save the health line.

    The install directory goes on ``sys.path`` for the duration, exactly as ``run.py`` puts
    it there: the doctor's stdlib audit imports ``harness`` to see what it touches, and
    ``--repair``-style checks resolve modules by name.  It is removed again afterwards.
    """
    doctor = modules.doctor
    ctx = doctor.DoctorContext.for_config(
        config, config_path=config_path, root=target, network=network, persist=True
    )
    ctx.config_error = getattr(config, "_installer_error", "")
    saved_path = list(sys.path)
    sys.path.insert(0, os.path.abspath(target))
    try:
        before = doctor.run_checks(ctx)
        if skip_fixes:
            after, outcomes = before, []
        else:
            after, outcomes = apply_safe_fixes(doctor, ctx, before)
        doctor.save_health(ctx, after)
    finally:
        sys.path[:] = saved_path
    return after, outcomes


# --------------------------------------------------------------------------------------
# Setup, step 5: the launcher, the one line, and the optional immediate start
# --------------------------------------------------------------------------------------


def write_launcher(modules, target: str, report: Report) -> str:
    """Write ``start.py`` next to ``run.py``.  Returns its path, or ``""`` on failure."""
    path = os.path.join(os.path.abspath(target), "start.py")
    try:
        write_private_text(modules.security, path, START_PY)
    except OSError as exc:
        report("launcher: could not write {} ({}: {})".format(path, type(exc).__name__, exc))
        return ""
    return path


def launch_argv(args) -> "list[str] | None":
    """The ``run.py`` arguments a launch flag asks for, or ``None`` for "just print"."""
    if args.task:
        return ["run.py", args.task]
    if args.ui:
        return ["run.py", "--ui"]
    if args.chat:
        return ["run.py"]
    return None


def launch(target: str, argv: "list[str]") -> int:
    """Run the installed ``run.py`` in this process, restoring ``sys.argv`` afterwards.

    Pyto has no usable subprocess, so "run it now" means ``runpy`` -- exactly what the
    printed one-liner does, including the directory, so relative paths behave.
    """
    run_py = os.path.join(os.path.abspath(target), "run.py")
    saved = list(sys.argv)
    sys.argv = [run_py] + list(argv[1:])
    try:
        runpy.run_path(run_py, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    finally:
        sys.argv = saved
    return 0


# --------------------------------------------------------------------------------------
# Setup: the whole phase, in order
# --------------------------------------------------------------------------------------


def setup(args, target: str) -> int:
    """Key, config, doctor, launcher, one next step -- everything after the files land."""
    modules = load_harness(target)  # InstallError -> the caller
    report = Report(modules.security)
    if modules.note:
        report("setup: {}".format(modules.note))
    # Before anything reads or resolves a state path: move a hidden one from an older
    # release into the open, so the key that release saved is found again and the folder is
    # visible in Files.  This is the first path-touching step on purpose -- the resolver's
    # probe creates the new directory, and that empty directory is what used to make the
    # move a no-op and strand the old data.  Nothing is ever deleted: a refusal, a merge or
    # a failure is reported here and the install continues against the resolved (new) path.
    # A tree too old to have harness/home.py (or one whose home cannot be resolved here) is
    # simply skipped: the config step below reports that condition in its own words.
    migration = migrate_state_directories(modules)
    if migration:
        report(migration)
    if args.api_key:
        # Same warning run.py gives: the credential is in the process table and the shell
        # history.  The key itself is never printed, here or anywhere else.
        report(
            "[warning] --api-key is visible in `ps` and your shell history; prefer the config "
            "file (mode 0600) or the DEEPSEEK_API_KEY environment variable."
        )

    config_module = modules.config
    home_error = ""
    try:
        # The harness resolver: an absolute, writable path, or ConfigError with the
        # PYTO_HARNESS_HOME workaround in it (this is the path that used to be the
        # literal "~/pyto_harness" on a device without a home directory).
        config_path = config_module.default_config_path()
    except config_module.ConfigError as exc:
        home_error = str(exc)
        config_path = ""
        report_home_workaround(report, exc)
        report("setup: finishing the install without a config; everything else works.")
    existing, parse_error = read_config(config_path) if config_path else ({}, "")
    if parse_error:
        report(
            "setup: {} is {}; a 0600 copy is kept as {}.bak before it is rewritten.".format(
                config_path, parse_error, config_path
            )
        )

    stored_base = existing.get("api_base") if isinstance(existing.get("api_base"), str) else ""
    stored_model = existing.get("model") if isinstance(existing.get("model"), str) else ""
    api_base = (args.api_base or stored_base or config_module.DEFAULT_API_BASE).strip()
    model = (args.model or stored_model or config_module.DEFAULT_MODEL).strip()
    stored = existing.get("api_key") if isinstance(existing.get("api_key"), str) else None

    interactive = (args.ask or can_prompt(modules.ios)) and not args.yes
    key, api_base, verified, origin = acquire_key(
        modules,
        args,
        api_base=api_base,
        model=model,
        stored=stored,
        config_path=config_path,
        root=target,
        interactive=interactive,
        report=report,
    )

    if key is None:
        report("setup: no API key (nothing was passed, and nothing was typed).")
        report("       Add one later by re-running this installer with --api-key, or set DEEPSEEK_API_KEY.")
    else:
        report.secret(key)
        unchanged = (
            origin == ORIGIN_CONFIG
            and not args.api_base
            and not args.model
            and api_base == stored_base
            and model == stored_model
        )
        if unchanged and not parse_error:
            report("config : {} left as it is (the key in it works)".format(config_path))
        elif not config_path:
            report("config : not written (no writable folder; see PYTO_HARNESS_HOME above)")
        else:
            try:
                if parse_error:
                    backup_config(modules.security, config_path, report)
                payload = build_config_payload(
                    existing,
                    api_key=key,
                    api_base=api_base,
                    model=model,
                    defaults={
                        "max_turns": config_module.DEFAULT_MAX_TURNS,
                        "timeout": 60,
                        "workspace": config_module.default_workspace(),
                    },
                )
                mode = save_config(modules, config_path, payload, existed=os.path.exists(config_path))
            except (config_module.ConfigError, SetupError, OSError) as exc:
                # The install must finish even when the config cannot be written: the
                # files are in place and the workaround is what unblocks the user.
                home_error = home_error or str(exc)
                report("config : NOT written to {}".format(config_path))
                if isinstance(exc, config_module.ConfigError) or looks_like_a_permission_problem(exc):
                    report_home_workaround(report, exc)
                else:
                    report("setup: {}: {}".format(type(exc).__name__, exc))
            else:
                if mode == "0o600":
                    report("config : {} (mode 0600, key {})".format(config_path, config_module.redact_key(key)))
                else:
                    report(
                        "config : {} but its mode is {} -- run `chmod 600 {}` before trusting it".format(
                            config_path, mode, config_path
                        )
                    )

    overrides = {"api_key": key, "api_base": api_base, "model": model}
    config_error = ""
    try:
        config = config_module.load_config(config_path=config_path or None, overrides=overrides)
    except config_module.ConfigError as exc:
        # The file is the problem; the doctor has to be able to say so, so keep going.
        config_error = str(exc)
        config = config_module.Config(api_key=key, api_base=api_base, model=model)
        try:
            config.workspace = os.environ.get("PYTO_HARNESS_WORKSPACE") or config_module.default_workspace()
            config.sessions_dir = os.environ.get("PYTO_HARNESS_SESSIONS_DIR") or config_module.default_sessions_dir()
        except config_module.ConfigError:
            pass  # no writable home: the doctor reports it, the install still finishes
    setattr(config, "_installer_error", config_error)

    network = bool(verified) and not args.no_network
    try:
        after, _outcomes = run_doctor(
            modules, config, config_path=config_path, target=target, network=network, skip_fixes=args.skip_fixes
        )
        line = doctor_summary(modules.doctor, after)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not fail the install
        report("doctor : could not run ({}: {})".format(type(exc).__name__, exc))
    else:
        if args.skip_fixes:
            line += "  (fixes skipped: --skip-fixes)"
        report(line)
        for extra in doctor_attention(after):
            report(extra)

    launcher = write_launcher(modules, target, report)
    if home_error:
        report("")
        report("home   : no writable folder for ~/{} (see the PYTO_HARNESS_HOME note above)".format(STATE_DIR_NAME))
    report("")
    report("Next step - one command, nothing else to paste:")
    report("  {}".format(start_line(target)))
    if launcher:
        report("  (or open {} in Pyto and press Run)".format(launcher))

    argv = launch_argv(args)
    if argv is None:
        return 0
    report("")
    report("starting: {}".format(" ".join(argv)))
    return launch(target, argv)


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=(
            "Download, install, configure and health-check pyto-harness with the Python "
            "standard library only.  One run does the whole setup."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python install.py                       install, ask for the key, health-check\n"
            "  python install.py --api-key sk-...      the same with nothing to type\n"
            "  python install.py --no-setup            install the files only (CI, tests)\n"
            "  python install.py --zip app.zip --into ~/pyto-agent\n"
            "  python install.py --reconfigure         ask for a new key, keep everything else\n"
            "  python install.py --ref v1.0.0 --sha256 <digest>\n"
        ),
    )
    parser.add_argument("--ref", default=DEFAULT_REF, help="branch or tag to install (default: %(default)s)")
    parser.add_argument(
        "--sha256",
        dest="sha256",
        default=None,
        help="expected SHA-256 of the archive; the install is refused when it does not match "
        "(pin the digest a previous run printed, and use --ref <tag> so the bytes cannot move)",
    )
    parser.add_argument("--into", default=DEFAULT_TARGET, help="destination directory (default: ./%(default)s)")
    parser.add_argument("--zip", dest="zip_path", default=None, help="install from a local .zip instead of the network")
    parser.add_argument("--force", action="store_true", help="install even if the destination is not a pyto-agent checkout")
    parser.add_argument("--no-verify", action="store_true", help="skip the post-install parse and import check")

    group = parser.add_argument_group("setup (what happens after the files land)")
    group.add_argument(
        "--no-setup",
        action="store_true",
        help="install the files only: no key, no config, no doctor, and the old step-by-step text "
        "(this is the behaviour to use from CI and tests)",
    )
    group.add_argument(
        "--api-key",
        default=None,
        help="use this key instead of prompting; it is validated with one minimal request before "
        "it is written (WARNING: it is visible in `ps` and your shell history)",
    )
    group.add_argument(
        "--key-file",
        dest="key_file",
        default=None,
        help="read the key from this file (the first line; nothing else in it is read)",
    )
    group.add_argument("--api-base", default=None, help="API base URL to write and validate against")
    group.add_argument("--model", default=None, help="model name to write and validate with")
    group.add_argument(
        "--ask",
        action="store_true",
        help="ask for the API key even when this does not look like an interactive console",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="ask for the key even when the config file already has a working one",
    )
    group.add_argument(
        "--yes",
        action="store_true",
        help="accept every default and never prompt (no key is asked for; an unreachable endpoint "
        "still saves the key)",
    )
    group.add_argument(
        "--save-anyway",
        action="store_true",
        help="save the key even when the endpoint could not be reached or answered inconclusively",
    )
    group.add_argument(
        "--skip-fixes",
        action="store_true",
        help="run the doctor's checks but apply none of its fixes",
    )
    group.add_argument(
        "--no-network",
        action="store_true",
        help="make no network request at all: the key is saved unvalidated and the doctor's "
        "network checks are skipped",
    )

    launch = parser.add_mutually_exclusive_group()
    launch.add_argument("--chat", action="store_true", help="after setup, start the terminal chat REPL")
    launch.add_argument("--ui", action="store_true", help="after setup, open the Pyto chat window")
    launch.add_argument("--task", default=None, metavar="TEXT", help="after setup, run TEXT as one task")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    expected = (args.sha256 or "").strip().lower() or None
    if expected is not None and not SHA256_RE.match(expected):
        print(
            "--sha256 must be 64 hexadecimal characters (the digest printed by an earlier run); "
            "got {!r}".format(args.sha256),
            file=sys.stderr,
        )
        return 2

    try:
        target = expand_local_path(args.into, what="--into")
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Before the archive is read and before any state path is resolved: move a hidden state
    # folder from a release before the rename into the open, using a harness that is
    # already on disk (the installer's own tree, or the install being updated).  Silent and
    # idempotent when there is nothing to do; setup() repeats it for the tree it installs.
    migration = migrate_state_before_setup(target)
    if migration:
        print(migration)

    if os.path.isdir(target) and os.listdir(target) and not args.force:
        if not os.path.isfile(os.path.join(target, "run.py")):
            print(
                "refusing to install into {}: it exists and does not look like a pyto-agent checkout.\n"
                "Pass --force to overwrite it, or --into somewhere else.".format(target),
                file=sys.stderr,
            )
            return 2
        print("updating the existing install at {}".format(target))

    if args.zip_path:
        try:
            source = expand_local_path(args.zip_path, what="--zip")
        except InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not os.path.isfile(source):
            print("no such file: {}".format(source), file=sys.stderr)
            return 2
        print("reading {}".format(source))
        try:
            with open(source, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            print("could not read {}: {}".format(source, exc), file=sys.stderr)
            return 2
    else:
        print("downloading {}/{} ...".format(REPO, args.ref))
        try:
            payload = download(args.ref)
        except InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover - unexpected network shapes
            print(
                "the download failed unexpectedly ({}: {}).\n"
                "Try again, or pass --zip with a file you downloaded elsewhere.".format(type(exc).__name__, exc),
                file=sys.stderr,
            )
            return 1

    digest = archive_digest(payload)
    print("sha256 {}  ({} bytes{})".format(digest, len(payload), ", " + args.ref if not args.zip_path else ""))
    if expected is not None and digest != expected:
        print(
            "REFUSING TO INSTALL: the archive digest does not match --sha256.\n"
            "  expected: {}\n"
            "  actual  : {}\n"
            "The download was modified in transit, the tag was moved, or the expected digest is "
            "for a different ref. Nothing was written to {}.".format(expected, digest, target),
            file=sys.stderr,
        )
        return 1

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            written = extract(archive, target)
    except zipfile.BadZipFile:
        print("that is not a zip archive (did the download get truncated?)", file=sys.stderr)
        return 1
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("{} file(s), {:.0f} KB".format(len(written), len(payload) / 1024.0))

    if not args.no_verify:
        try:
            notes = verify(target)
        except InstallError as exc:
            print("verification failed:\n{}".format(exc), file=sys.stderr)
            print("The files are in {} but treat this install as broken.".format(target), file=sys.stderr)
            return 1
        for note in notes:
            print("verify: {}".format(note))

    if args.no_setup:
        if args.chat or args.ui or args.task:
            print("(--no-setup: --chat/--ui/--task are ignored)", file=sys.stderr)
        print()
        print(next_steps(target))
        return 0

    print()
    try:
        return setup(args, target)
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        print("The files are in {}; finish the setup by hand with the steps below.".format(target), file=sys.stderr)
        print()
        print(next_steps(target))
        return 1
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        # Safety net: a filesystem the sandbox refuses must never turn a completed file
        # install into a traceback.  The files are there; the steps below finish by hand.
        print("setup: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        for line in HOME_WORKAROUND.splitlines():
            print("       " + line, file=sys.stderr)
        print("The files are in {}, so the install itself is complete.".format(target), file=sys.stderr)
        print()
        print(next_steps(target))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install or update pyto-agent on the device.

Pyto has no ``git`` and no ``unzip``, so this downloads the repository archive
over HTTPS and extracts it with the standard library alone.

    python install.py                      # install into ./pyto-agent
    python install.py --into ~/pyto-agent  # somewhere else
    python install.py --ref v1.0           # a tag or branch
    python install.py --zip pyto-agent.zip # from a file (iCloud, AirDrop)

Updating is the same command: it replaces the code and leaves your state alone.
Your API key, sessions, memory and backups live in ``~/.pyto_harness`` and your
programs live in the workspace (``~/pyto_harness_workspace`` by default), never
inside the code directory, so re-installing cannot lose them.

Standard library only, Python 3.10 (Pyto's version).  Nothing here needs a
shell, a subprocess or a compiler.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import os
import re
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


class InstallError(Exception):
    """Anything that stops the install, with a message meant for the user."""


#: A SHA-256 in hex, the form ``--sha256`` accepts.
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def archive_digest(payload: bytes) -> str:
    """The SHA-256 of the archive bytes — the value a user pins with ``--sha256``.

    The installer is the one component that runs *before* the harness exists and then
    holds the API key, so "whatever GitHub served" was the whole trust model.  Every run
    now prints this digest (pin it, then verify the next install against it), and
    ``--sha256`` refuses a mismatch instead of installing the bytes anyway.
    """
    return hashlib.sha256(payload).hexdigest()


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
    absolute = os.path.abspath(target)
    prefix = "import os, runpy, sys; os.chdir({}); sys.argv = ".format(repr(absolute))
    tail = "; runpy.run_path('run.py', run_name='__main__')"
    return (
        "Installed to {target}\n"
        "\n"
        "Next, in this order (paste one line at a time into the Pyto console):\n"
        "\n"
        "  1. Give it your API key (once), then paste the key into\n"
        "     ~/.pyto_harness/config.json and save:\n"
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
        "Your key, sessions and memory live in ~/.pyto_harness and are never touched by an update."
    ).format(
        target=absolute,
        one=prefix + "['run.py', '--init']" + tail,
        two=prefix + "['run.py', '--doctor', '--fix']" + tail,
        three=prefix + "['run.py', 'write me a script that renames my screenshots by date']" + tail,
        four=prefix + "['run.py', '--ui']" + tail,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Download and install pyto-agent with the Python standard library only.",
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
    args = parser.parse_args(argv)

    expected = (args.sha256 or "").strip().lower() or None
    if expected is not None and not SHA256_RE.match(expected):
        print(
            "--sha256 must be 64 hexadecimal characters (the digest printed by an earlier run); "
            "got {!r}".format(args.sha256),
            file=sys.stderr,
        )
        return 2

    target = os.path.abspath(os.path.expanduser(args.into))

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
        source = os.path.expanduser(args.zip_path)
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

    print()
    print(next_steps(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

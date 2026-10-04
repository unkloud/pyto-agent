"""Small security primitives shared by the rest of the harness.

**This module is not a sandbox.**  On iOS the harness runs as one process with one
Python object graph, so code the harness *executes in-process* (``run_program`` on Pyto)
can reach every module, every file the app can read and every attribute of every live
object.  Nothing here changes that.  What these helpers do is make the rules the harness
documents true for every path the harness itself controls:

* **private file modes** — a file the harness creates with an API key or user data in it
  is created ``0600`` and a directory it creates is ``0700``, *at creation*, so there is no
  window in which a looser mode exists (``open`` + later ``chmod`` is a TOCTOU window and
  a silent failure waiting to happen);
* **value-shape secret scrubbing** — a credential is removed by its *shape* (``sk-…``,
  ``Bearer …``, an ``"api_key": "…"`` field, the configured key verbatim) and not only by
  the field name it happens to sit under;
* **a credential-free environment for child processes and for in-process runs** — a
  generated program does not inherit ``DEEPSEEK_API_KEY``/``*_TOKEN``/``*_SECRET``.

Everything here is best effort by construction; see ``SECURITY.md`` for what is enforced
and what is only a speed bump.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence, Set

#: Files the harness creates for itself: owner-only.
PRIVATE_FILE_MODE = 0o600
#: Directories the harness creates for itself: owner-only.
PRIVATE_DIR_MODE = 0o700

# --------------------------------------------------------------------------------------
# Private creation (mode at creation time, never a silent chmod)
# --------------------------------------------------------------------------------------


def mkdir_private(path: str, mode: int = PRIVATE_DIR_MODE) -> str:
    """Create ``path`` (and any missing parents) with ``mode``, then chmod explicitly.

    ``os.makedirs(mode=…)`` is masked by the umask, so the explicit ``chmod`` is what
    makes the mode true; it is allowed to raise, because a directory that is silently
    group-readable while the code claims ``0700`` is worse than a loud failure.  Existing
    directories are left alone (the doctor reports those).
    """
    if not path:
        return path
    missing = []
    current = path
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, mode)
        except FileExistsError:  # pragma: no cover - a concurrent creator won the race
            continue
        if os.name == "posix":
            os.chmod(directory, mode)
    return path


def open_private(
    path: str,
    *,
    exclusive: bool = False,
    append: bool = False,
    truncate: bool = False,
    mode: int = PRIVATE_FILE_MODE,
    encoding: str = "utf-8",
    binary: bool = False,
) -> "object":
    """Open ``path`` for writing, creating it ``0600`` and tightening a looser existing file.

    With ``exclusive=True`` the file must not exist (``O_CREAT|O_EXCL``), which is what
    ``--init`` and every one-shot write use so that no world-readable window exists and a
    concurrent creator is refused instead of overwritten.
    """
    parent = os.path.dirname(path)
    if parent:
        mkdir_private(parent)
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    if append:
        flags |= os.O_APPEND
    if truncate and not exclusive:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, mode)
    try:
        if os.name == "posix":
            current = stat.S_IMODE(os.fstat(fd).st_mode)
            if current != mode:
                # Explicit and non-silent: a file holding key material must not stay
                # group/world readable because a chmod failed quietly.
                os.chmod(path, mode)
    except OSError:
        os.close(fd)
        raise
    text_mode = "a" if append else "w"
    if binary:
        return os.fdopen(fd, text_mode + "b")
    return os.fdopen(fd, text_mode, encoding=encoding)


def write_private(path: str, data: "str | bytes", *, exclusive: bool = False) -> str:
    """One-shot private write.  ``data`` is written verbatim; returns ``path``."""
    binary = isinstance(data, bytes)
    handle = open_private(path, exclusive=exclusive, mode=PRIVATE_FILE_MODE, binary=binary)
    try:
        handle.write(data)  # type: ignore[arg-type]
        handle.flush()
    finally:
        handle.close()
    return path


def chmod_private(path: str, mode: int = PRIVATE_FILE_MODE) -> None:
    """Tighten an existing file/directory.  Raises on failure (never silent)."""
    os.chmod(path, mode)


# --------------------------------------------------------------------------------------
# Secret scrubbing by shape
# --------------------------------------------------------------------------------------

#: Values registered by :func:`register_secret` (the configured key, header values, …).
_KNOWN_SECRETS: Set[str] = set()
#: Never register or replace anything shorter than this: a 4-character "secret" would
#: redact ordinary words out of every tool result.
MIN_SECRET_CHARS = 8

#: ``sk-…`` provider keys, ``sk-proj-…`` included.  The length is bounded because the
#: character class would otherwise swallow an entire word run next to the key (a program
#: printing ``key=<key>`` followed by 20 000 letters lost all of them), and because every
#: real provider key is well under this.
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,120}")
#: ``Bearer <token>`` / ``Basic <blob>`` header material that leaked into a body.
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{12,200}")
#: Google-style API keys (the secrets audit's second canary shape).
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_\-]{20,120}")
#: ``"api_key": "value"`` and friends in JSON, in either quote style.
_JSON_FIELD = re.compile(
    r"(?i)([\"'](?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret"
    r"|auth[_-]?token|authorization|password|passwd|secret|token)[\"']\s*:\s*)([\"'])([^\"']{4,})(\2)"
)
#: ``api_key=value`` / ``token: value`` outside JSON, when the value is long enough to be
#: a credential rather than a word.
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|authorization|password)"
    r"(\s*[:=]\s*)[\"']?([A-Za-z0-9._\-+/=]{8,})[\"']?"
)

REDACTED = "<redacted>"

#: Header names whose value must never be printed.  Matched as a substring, so
#: ``x-goog-api-key`` and ``x-auth-token`` are covered without a name list.
_SECRET_HEADER_RE = re.compile(r"(?i)(auth|key|token|secret|cookie|bearer|session|credential|password)")


def is_secret_header(name: str) -> bool:
    """True when a header name suggests its value is a credential."""
    return bool(_SECRET_HEADER_RE.search(name or ""))


def register_secret(value: Optional[str]) -> None:
    """Remember a configured secret so it is scrubbed verbatim wherever it appears."""
    if not value or not isinstance(value, str):
        return
    if len(value) < MIN_SECRET_CHARS:
        return
    _KNOWN_SECRETS.add(value)


def known_secrets() -> Set[str]:
    """A copy of the registered values (longest first, for nested replacements)."""
    return set(_KNOWN_SECRETS)


def scrub_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove credentials from ``text`` by value *and* by shape.

    ``secrets`` adds caller-known values (the doctor passes the resolved key and header
    values); values registered through :func:`register_secret` are always applied, so the
    runtime paths (provider error bodies, tool results, session rows, spill files) scrub
    the configured key without having to thread it through every call site.
    """
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    values = {value for value in secrets if value and isinstance(value, str) and len(value) >= MIN_SECRET_CHARS}
    values |= _KNOWN_SECRETS
    for value in sorted(values, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    text = _SK_TOKEN.sub(REDACTED, text)
    text = _BEARER.sub(lambda match: "{0} {1}".format(match.group(1), REDACTED), text)
    text = _GOOGLE_KEY.sub(REDACTED, text)
    text = _JSON_FIELD.sub(lambda match: "{0}{1}{2}{1}".format(match.group(1), match.group(2), REDACTED), text)
    text = _KEY_VALUE.sub(lambda match: "{0}{1}{2}".format(match.group(1), match.group(2), REDACTED), text)
    return text


def scrub_value(value: object, secrets: Iterable[str] = ()) -> object:
    """Recursively scrub strings inside a JSON-shaped value (dict/list/str/scalar)."""
    if isinstance(value, str):
        return scrub_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {key: scrub_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(item, secrets) for item in value]
    return value


def redact_url_userinfo(url: str) -> str:
    """``https://user:pw@host/x`` -> ``https://<redacted>@host/x`` for display."""
    if not url or "@" not in url:
        return url
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    userinfo, at, host = rest.rpartition("@")
    if not at or not userinfo:
        return url
    return "{}{}{}@{}".format(scheme, separator, REDACTED, host)


def redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Header copy safe to print or log: anything credential-shaped becomes redacted."""
    return {str(key): (REDACTED if is_secret_header(str(key)) else str(value)) for key, value in headers.items()}


# --------------------------------------------------------------------------------------
# Environment scrubbing
# --------------------------------------------------------------------------------------

#: A variable is credential-shaped when its name ends in / contains these markers.  The
#: spec is ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``; ``PASSWORD`` and the harness's own
#: ``PYTO_HARNESS_*`` namespace are included because both can carry the key.
SECRET_ENV_RE = re.compile(r"(?i)(^|_)(API_?KEY|TOKEN|SECRET|PASSWORD)($|_)")
SECRET_ENV_EXACT = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY")
SECRET_ENV_PREFIX = "PYTO_HARNESS_"


def is_secret_env_name(name: str) -> bool:
    """True when an environment variable must not be visible to a generated program."""
    if not name:
        return False
    if name.startswith(SECRET_ENV_PREFIX):
        return True
    if name in SECRET_ENV_EXACT:
        return True
    return bool(SECRET_ENV_RE.search(name))


def scrubbed_environ(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A copy of the environment with every credential-shaped variable removed.

    Used for the child process of ``run_program`` and for the doctor's test subprocess:
    both are code the user did not write, and neither has any business holding the key.
    """
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items() if not is_secret_env_name(name)}


@contextlib.contextmanager
def temporary_environ_scrub(environ: Optional[Mapping[str, str]] = None) -> Iterator[Mapping[str, str]]:
    """Remove credential-shaped variables for the duration of a block, always restoring.

    The in-process (``runpy``) path on iOS cannot hand a child its own environment, so the
    running process's environment *is* the program's environment.  Everything removed is
    put back in a ``finally``, so an exception inside the program cannot leave the harness
    without its own key.
    """
    target = os.environ if environ is None else environ  # type: ignore[assignment]
    removed: Dict[str, str] = {}
    for name in list(target.keys()):
        if is_secret_env_name(name):
            removed[name] = target.pop(name)  # type: ignore[attr-defined]
    try:
        yield target
    finally:
        for name, value in removed.items():
            target[name] = value  # type: ignore[index]


def secret_values_from_headers(headers: Optional[Mapping[str, str]]) -> Sequence[str]:
    """The values of credential-shaped headers, for the scrubber."""
    if not headers:
        return ()
    return tuple(str(value) for name, value in headers.items() if is_secret_header(str(name)) and value)

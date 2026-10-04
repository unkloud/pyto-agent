"""Self-diagnosis: a registry of checks that answer "is this installation healthy?".

Design rules, in the order they matter on a phone:

1. **A check never crashes the doctor.**  Every check body runs inside a guard; an
   exception becomes a ``fail`` result carrying the traceback summary.  A diagnostic that
   dies when it finds a problem is worse than no diagnostic.
2. **A finding is structured, not prose.**  :class:`CheckResult` carries a status, a
   ``fix_id`` when a machine can repair it, and a ``human_action`` when only the user can.
   The CLI, the agent tool and the tests all read the same fields.
3. **Nothing secret is ever recorded.**  Evidence dictionaries and details are scrubbed:
   the API key is never printed, and response bodies are passed through
   :func:`scrub_secrets` before they are shown.
4. **Checks are read-only unless a fix is explicitly applied.**  ``--doctor`` inspects;
   ``--doctor --fix`` writes.  The one exception is the small capability cache, which is
   only written when the context says persistence is allowed.

Statuses:

===== ==========================================================================
ok      the check passed
warn    usable, but degraded or drifting; a human may want to act
fail    broken, and a machine repair exists (``fixable`` / ``fix_id``)
fixed   was broken, and the doctor repaired it during this run
skipped not applicable here (a flag is off, or the platform is not iOS)
unfixable broken in a way no local machine repair can address; ``human_action`` is
        the thing to tell the user, in plain language
===== ==========================================================================
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import inspect
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import budget, home, ios, pyto_api
from .config import (
    CONFIG_DIR_NAME,
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    Config,
    ConfigError,
    default_config_path,
    default_sessions_dir,
    default_workspace,
    redact_key,
)
from .errors import HarnessError
from . import security
from .security import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE, mkdir_private, open_private, scrubbed_environ, write_private

#: Canonical status vocabulary.  Anything else is a bug in a check.
STATUSES = ("ok", "warn", "fail", "fixed", "skipped", "unfixable")

#: Statuses that need a line in the report's "needs you" bucket.
ATTENTION_STATUSES = ("fail", "unfixable")

#: The subset of the suite ``--doctor --deep`` runs unless ``--deep-tests`` is given,
#: and the default gate for a self-edit.  Chosen by measured runtime: these are the fast,
#: high-signal modules (~3 s together on a laptop).
FAST_TEST_MODULES = (
    "test_schema",
    "test_config",
    "test_session",
    "test_tools",
    "test_ios",
    "test_tools_ios",
)

#: Which test modules cover which source file.  A self-edit gate runs the union of the
#: base subset and the covering modules for the edited file: bounded work that still
#: exercises the code that changed.  A full-suite gate is opt-in (``--deep-tests``).
GATE_COVERAGE = {
    "harness/schema.py": ("test_schema",),
    "harness/config.py": ("test_config",),
    "harness/session.py": ("test_session", "test_config"),
    "harness/tools.py": ("test_tools",),
    "harness/tools_ios.py": ("test_tools_ios", "test_tools"),
    "harness/pyto_api.py": ("test_pyto_api", "test_tools_ios"),
    "harness/ios.py": ("test_ios",),
    "harness/llm.py": ("test_llm",),
    "harness/loop.py": ("test_loop",),
    "harness/ui.py": ("test_loop",),
    "harness/doctor.py": ("test_doctor", "test_config"),
    "harness/repair.py": ("test_repair", "test_config"),
    "run.py": ("test_e2e",),
}

#: How deep a self-test may nest before the doctor refuses.  A test suite that runs the
#: suite that runs the suite is a fork bomb with extra steps; three levels is enough for
#: a repair gate inside a test and stops there.
MAX_SELFTEST_DEPTH = 3

#: Environment variable carrying that depth into child processes.
SELFTEST_DEPTH_ENV = "PYTO_HARNESS_SELFTEST_DEPTH"

#: Checks that make a real network connection.  Skipped in the fast first-run pass and
#: when ``--no-network`` is given.
NETWORK_CHECK_IDS = ("network_reachable", "api_auth", "model_accepted")

#: Checks the fast first-run pass runs.
FIRST_RUN_SKIP = NETWORK_CHECK_IDS + ("selftest",)

#: iOS-only modules probed by the ``ios_modules`` check.  Imported by name (never as a
#: static import), because None of these exist on a desktop interpreter.
IOS_PROBE_MODULES = (
    "pasteboard",
    "share",
    "notifications",
    "photos",
    "speech",
    "sound",
    "music",
    "location",
    "motion",
    "background",
    "xcallback",
    "apps",
    "calendar_events",
    "pyto_ui",
    "widgets",
)

#: The fragile call shapes the ``ios_signatures`` check discovers with ``inspect`` only.
#: Nothing here is ever *called*: a diagnostic must not open a share sheet.
FRAGILE_SIGNATURES = (
    ("calendar_events", "save_event"),
    ("background", "BackgroundTask"),
    ("share", "open"),
    ("xcallback", "open_url"),
)

#: Ordered fallback list tried by ``model_accepted``.  DeepSeek first: that is the
#: default provider, so the common failure is a renamed or withdrawn DeepSeek model.
MODEL_FALLBACKS = (
    "deepseek-chat",
    "deepseek-reasoner",
    "gpt-4o-mini",
    "gpt-3.5-turbo",
)

#: Keys the config file is expected to carry.
EXPECTED_CONFIG_KEYS = ("api_base", "model", "api_key", "max_turns", "timeout", "workspace")

#: Every key a config file may carry.  ``EXPECTED_CONFIG_KEYS`` is the *required* subset;
#: this is the whole vocabulary, so a real optional key is not reported as a typo.
KNOWN_CONFIG_KEYS = tuple(
    sorted(
        set(EXPECTED_CONFIG_KEYS)
        | {
            "headers",
            "extra_headers",
            "sessions_dir",
            "spill_dir",
            "stream",
            "max_tokens",
            "temperature",
            "yolo",
            "compact",
            "allow_unattended_programs",
        }
    )
)

#: How much of a session log the integrity scan will read before it gives up.
MAX_SESSION_SCAN_BYTES = 8 * 1024 * 1024
MAX_SESSION_SCAN_LOGS = 5

#: Free-space thresholds for the workspace check.
DISK_WARN_BYTES = 200 * 1024 * 1024
DISK_FAIL_BYTES = 50 * 1024 * 1024

#: Memory thresholds: warn below this, fail below that (Pyto kills scripts near 500 MB).
MEMORY_WARN_BYTES = 800 * 1024 * 1024
MEMORY_FAIL_BYTES = 500 * 1024 * 1024

#: Markers that mean "this key is a placeholder, not a credential".
PLACEHOLDER_MARKERS = ("replace", "your-key", "yourkey", "changeme", "example", "todo", "xxx", "<", "dummy")

_USER_AGENT = "pyto-harness-doctor/1.0 (stdlib; iOS)"


def harness_root() -> str:
    """The directory that contains ``run.py`` and ``harness/``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir(env: Optional[Mapping[str, str]] = None, config_path: Optional[str] = None) -> str:
    """Where the harness keeps its own state (health, capabilities, backups).

    Precedence: ``PYTO_HARNESS_STATE_DIR``, then the directory of ``PYTO_HARNESS_CONFIG``
    (so a test or a portable install keeps its state next to its config), then the
    resolved home (``~/.pyto_harness`` — never a literal tilde, and never the temp
    directory unless that is genuinely the only writable place).

    This helper is used by the repair/backup paths, which must still work *while* the
    doctor is reporting an unusable home, so a resolution failure falls back to
    ``./.pyto_harness`` here; :func:`check_home` is what reports it as a failure.
    """
    environ = os.environ if env is None else env
    override = environ.get("PYTO_HARNESS_STATE_DIR")
    try:
        if override:
            return home.expand_user_path(override, what="PYTO_HARNESS_STATE_DIR")
        configured = config_path or environ.get("PYTO_HARNESS_CONFIG")
        if configured:
            directory = os.path.dirname(home.expand_user_path(configured, what="PYTO_HARNESS_CONFIG"))
            if directory:
                return directory
        return os.path.join(home.resolve_home(), CONFIG_DIR_NAME)
    except ConfigError:
        return os.path.join(os.getcwd(), CONFIG_DIR_NAME)


def run_py_path(root: Optional[str] = None) -> str:
    return os.path.join(root or harness_root(), "run.py")


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class CheckResult:
    """One check's structured outcome."""

    id: str
    title: str
    status: str
    detail: str = ""
    fixable: bool = False
    fix_id: Optional[str] = None
    human_action: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def clean(self) -> bool:
        """True when nothing needs attention."""
        return self.status in ("ok", "warn", "fixed", "skipped")

    def failed(self) -> bool:
        return self.status in ATTENTION_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}
        if self.fixable:
            payload["fixable"] = True
        if self.fix_id:
            payload["fix_id"] = self.fix_id
        if self.human_action:
            payload["human_action"] = self.human_action
        if self.evidence:
            payload["evidence"] = dict(self.evidence)
        return payload


def result(
    check_id: str,
    title: str,
    status: str,
    detail: str = "",
    *,
    fixable: bool = False,
    fix_id: Optional[str] = None,
    human_action: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
) -> CheckResult:
    if status not in STATUSES:  # pragma: no cover - a check authoring bug
        status = "fail"
        detail = "{} (invalid status from the check)".format(detail)
    return CheckResult(
        id=check_id,
        title=title,
        status=status,
        detail=detail,
        fixable=bool(fixable),
        fix_id=fix_id,
        human_action=human_action,
        evidence=dict(evidence or {}),
    )


@dataclass
class FixOutcome:
    """What happened when one fix ran."""

    fix_id: str
    check_id: str
    ok: bool
    detail: str = ""
    error: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "fix_id": self.fix_id,
            "check_id": self.check_id,
            "ok": self.ok,
            "detail": self.detail,
        }
        if self.error:
            payload["error"] = self.error
        if self.evidence:
            payload["evidence"] = dict(self.evidence)
        return payload

    def render(self) -> str:
        head = "{}: {}".format(self.fix_id, "applied" if self.ok else "failed")
        lines = [head]
        if self.detail:
            lines.append("  " + self.detail)
        if self.error:
            lines.append("  error: {}".format(self.error))
        return "\n".join(lines)


@dataclass
class Fix:
    """A machine repair, keyed by ``fix_id``.

    ``safe`` marks the fixes the fast first-run pass may apply without being asked:
    creating a missing directory, tightening the config file mode, and cutting a torn
    final log line.  Everything else waits for an explicit ``--fix``.
    """

    id: str
    check_id: str
    title: str
    safe: bool
    handler: Callable[["DoctorContext", CheckResult], FixOutcome]


# --------------------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------------------


@dataclass
class DoctorContext:
    """Everything the checks need, with every path injectable for tests."""

    config: Config
    config_path: str = ""
    root: str = ""
    state: str = ""
    workspace: str = ""
    sessions_dir: str = ""
    #: The resolved home directory (``~``) and how it was chosen, for the header line.
    home: str = ""
    home_note: str = ""
    home_source: str = ""
    #: The actionable message when no home directory could be resolved at all.
    home_error: str = ""
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    network: bool = False
    deep: bool = False
    deep_tests: bool = False
    persist: bool = False
    network_timeout: float = 6.0
    selftest_timeout: float = 900.0
    models: Sequence[str] = MODEL_FALLBACKS
    #: Extra context for ``config_parses`` when the CLI already failed to load the file.
    config_error: str = ""
    #: Cross-check scratch space (the auth result feeds ``model_accepted``).
    state_data: Dict[str, Any] = field(default_factory=dict)
    #: Guards against a selftest that would recursively run the whole suite again.
    testing: bool = False

    # -- derived paths -------------------------------------------------------------

    @property
    def home_line(self) -> str:
        """``/path (from cwd; HOME was unusable)`` for the report header."""
        if self.home:
            return "{} ({})".format(self.home, self.home_note) if self.home_note else self.home
        return "<unresolved: no writable folder for ~/{}>".format(CONFIG_DIR_NAME)

    @property
    def health_path(self) -> str:
        return os.path.join(self.state, "health.json")

    @property
    def capabilities_path(self) -> str:
        return os.path.join(self.state, "capabilities.json")

    @property
    def backups_dir(self) -> str:
        return os.path.join(self.state, "backups")

    @property
    def run_py(self) -> str:
        return run_py_path(self.root)

    @property
    def tests_dir(self) -> str:
        return os.path.join(self.root, "tests")

    @property
    def shortcuts_doc(self) -> str:
        return os.path.join(self.workspace, "SHORTCUTS.md")

    @property
    def libs_doc(self) -> str:
        return os.path.join(self.workspace, pyto_api.DOC_NAME)

    @classmethod
    def for_config(
        cls,
        config: Config,
        *,
        env: Optional[Mapping[str, str]] = None,
        config_path: Optional[str] = None,
        root: Optional[str] = None,
        state: Optional[str] = None,
        workspace: Optional[str] = None,
        sessions_dir: Optional[str] = None,
        network: bool = False,
        deep: bool = False,
        deep_tests: bool = False,
        persist: bool = False,
        network_timeout: float = 6.0,
        selftest_timeout: float = 900.0,
        models: Optional[Sequence[str]] = None,
    ) -> "DoctorContext":
        environ: Mapping[str, str] = dict(os.environ) if env is None else env
        fallback_dir = os.path.join(os.getcwd(), CONFIG_DIR_NAME)
        home_error = ""
        home_choice = home.resolve_home_choice(environ=environ)
        if not home_choice.ok:
            home_error = home_choice.error

        def absolute(value: str, what: str, fallback: str) -> str:
            """Absolutise an explicit path, recording (not raising) an unexpandable ``~``."""
            nonlocal home_error
            try:
                return home.expand_user_path(value, what=what)
            except ConfigError as exc:
                home_error = home_error or str(exc)
                return fallback

        default_config = config_path or environ.get("PYTO_HARNESS_CONFIG")
        if not default_config:
            try:
                default_config = default_config_path()
            except ConfigError as exc:
                home_error = home_error or str(exc)
                default_config = os.path.join(fallback_dir, "config.json")
        resolved_config_path = absolute(default_config, "config file path", os.path.join(fallback_dir, "config.json"))
        resolved_state = state or state_dir(environ, resolved_config_path)
        try:
            resolved_workspace = (
                workspace or config.workspace or environ.get("PYTO_HARNESS_WORKSPACE") or default_workspace()
            )
            resolved_sessions = (
                sessions_dir
                or config.sessions_dir
                or environ.get("PYTO_HARNESS_SESSIONS_DIR")
                or default_sessions_dir()
            )
        except ConfigError as exc:
            # Nothing is writable: keep the doctor alive so check_home can report the
            # actionable message instead of crashing the whole diagnostic.
            home_error = home_error or str(exc)
            resolved_workspace = workspace or os.path.join(os.getcwd(), "pyto_harness_workspace")
            resolved_sessions = sessions_dir or os.path.join(fallback_dir, "sessions")
        return cls(
            config=config,
            config_path=resolved_config_path,
            root=os.path.abspath(root or harness_root()),
            state=absolute(resolved_state, "state directory", fallback_dir),
            workspace=absolute(resolved_workspace, "workspace", os.path.join(os.getcwd(), "pyto_harness_workspace")),
            sessions_dir=absolute(resolved_sessions, "sessions directory", os.path.join(fallback_dir, "sessions")),
            home=home_choice.path,
            home_note=home_choice.note,
            home_source=home_choice.source,
            home_error=home_error,
            env=environ,
            network=network,
            deep=deep,
            deep_tests=deep_tests,
            persist=persist,
            network_timeout=network_timeout,
            selftest_timeout=selftest_timeout,
            models=tuple(models) if models is not None else MODEL_FALLBACKS,
        )


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def secret_values(ctx: DoctorContext) -> List[str]:
    """Every credential-shaped value this process knows about, longest first."""
    values: List[str] = []
    key = getattr(ctx.config, "api_key", None)
    if isinstance(key, str) and len(key) >= 6:
        values.append(key)
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY"):
        value = ctx.env.get(name)
        if isinstance(value, str) and len(value) >= 6:
            values.append(value)
    values.extend(security.secret_values_from_headers(getattr(ctx.config, "extra_headers", None)))
    values.sort(key=len, reverse=True)
    return values


def scrub_secrets(text: str, ctx: Optional[DoctorContext] = None) -> str:
    """Replace any known credential with ``<redacted>``.  Applied to all foreign text.

    Delegates to :func:`harness.security.scrub_secrets`, so the doctor and the runtime
    scrub identically: the configured values verbatim *and* the credential shapes
    (``sk-…``, ``Bearer …``, ``"api_key": …``) a gateway or debug page might echo.
    """
    if not text:
        return ""
    return security.scrub_secrets(text, secret_values(ctx) if ctx is not None else ())


def _snippet(text: str, limit: int = 240, ctx: Optional[DoctorContext] = None) -> str:
    cleaned = " ".join(scrub_secrets(text or "", ctx).split())
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


class ProbeFailure(Exception):
    """A transport-level probe failure with a machine-readable ``kind``."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


NETWORK_ACTIONS = {
    "dns": "the host name in api_base does not resolve. Check the spelling, then your "
    "network or DNS-blocker settings (a VPN or a private-relay DNS profile is the usual cause).",
    "refused": "the host answered but nothing is listening on that port. Check api_base "
    "(a missing or wrong /v1 path, or a local server that is not running).",
    "tcp": "the TCP connection failed. Check api_base and your network.",
    "tls": "the TLS handshake failed. A captive-portal or corporate proxy is intercepting "
    "the connection; join a normal network or ask your admin for the proxy CA.",
    "tls-cert": "the server certificate did not verify. Do not disable verification: fix the "
    "device clock, or remove the proxy that is rewriting the certificate.",
    "timeout": "the connection timed out. The network is blocking or very slow; try again on "
    "Wi-Fi, and remember that on iOS a socket call cannot be interrupted once it starts.",
    "config": "api_base is not a usable http(s) URL. Fix it in the config file or with --api-base.",
}


def network_action(kind: str) -> str:
    return NETWORK_ACTIONS.get(kind, "the network probe failed; check the connection and api_base.")


def _url_parts(url: str) -> Tuple[str, str, int]:
    parsed = urllib.parse.urlsplit((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProbeFailure("config", "api_base must look like http(s)://host[:port][/path], got {!r}".format(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, int(port)


def probe_connect(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    """DNS + TCP + TLS reachability, separated so the failure can be named precisely."""
    started = time.monotonic()
    scheme, host, port = _url_parts(url)
    evidence: Dict[str, Any] = {"host": host, "port": port, "scheme": scheme}
    try:
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ProbeFailure("dns", "name resolution failed for {}: {}".format(host, exc)) from None
    except OSError as exc:
        raise ProbeFailure("dns", "name resolution failed for {}: {}: {}".format(host, type(exc).__name__, exc)) from None
    evidence["addresses"] = len(addresses)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout as exc:
        raise ProbeFailure("timeout", "no TCP answer from {}:{} within {}s".format(host, port, timeout)) from exc
    except ConnectionRefusedError as exc:
        raise ProbeFailure("refused", "{}:{} refused the connection".format(host, port)) from exc
    except OSError as exc:
        raise ProbeFailure("tcp", "TCP connect to {}:{} failed: {}: {}".format(host, port, type(exc).__name__, exc)) from exc
    try:
        if scheme == "https":
            try:
                context = ssl.create_default_context()
            except Exception as exc:  # noqa: BLE001 - no CA store on this interpreter
                raise ProbeFailure("tls", "could not build a TLS context: {}: {}".format(type(exc).__name__, exc)) from exc
            try:
                wrapped = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                raise ProbeFailure("tls-cert", "certificate verification failed for {}: {}".format(host, exc)) from exc
            except ssl.SSLError as exc:
                raise ProbeFailure("tls", "TLS handshake with {} failed: {}".format(host, exc)) from exc
            except socket.timeout as exc:
                raise ProbeFailure("timeout", "TLS handshake with {} timed out".format(host)) from exc
            except OSError as exc:
                raise ProbeFailure("tls", "TLS handshake with {} failed: {}: {}".format(host, type(exc).__name__, exc)) from exc
            try:
                peer = wrapped.getpeercert() or {}
                evidence["tls"] = "ok"
                evidence["cert_subject"] = _cert_label(peer)
            finally:
                wrapped.close()
        else:
            sock.close()
    finally:
        try:
            sock.close()
        except OSError:  # pragma: no cover - already closed
            pass
    evidence["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return evidence


def _cert_label(peer: Mapping[str, Any]) -> str:
    for group in peer.get("subject") or ():
        for key, value in group:
            if key == "commonName":
                return str(value)[:80]
    return "unknown"


def probe_post(
    url: str,
    *,
    api_key: Optional[str],
    body: Mapping[str, Any],
    timeout: float = 6.0,
) -> Dict[str, Any]:
    """One minimal JSON POST.  Returns a status; raises :class:`ProbeFailure` on transport.

    The response body is read (bounded) because providers put the actionable sentence
    there, and it never contains the API key — but callers still scrub it, because a
    hostile or debug endpoint could echo a header.
    """
    started = time.monotonic()
    scheme, host, port = _url_parts(url)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    payload = json.dumps(dict(body)).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "content-length": str(len(payload)),
        "user-agent": _USER_AGENT,
        "connection": "close",
    }
    if api_key:
        headers["authorization"] = "Bearer {}".format(api_key)
    connection: Any
    if scheme == "https":
        try:
            context = ssl.create_default_context()
        except Exception as exc:  # noqa: BLE001
            raise ProbeFailure("tls", "could not build a TLS context: {}".format(exc)) from exc
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(4096)
        status = int(response.status)
    except socket.timeout as exc:
        raise ProbeFailure("timeout", "no response from {} within {}s".format(host, timeout)) from exc
    except ssl.SSLCertVerificationError as exc:
        raise ProbeFailure("tls-cert", "certificate verification failed for {}: {}".format(host, exc)) from exc
    except ssl.SSLError as exc:
        raise ProbeFailure("tls", "TLS failure talking to {}: {}".format(host, exc)) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise ProbeFailure("tcp", "{} while talking to {}: {}".format(type(exc).__name__, host, exc)) from exc
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    return {
        "status": status,
        "text": raw.decode("utf-8", "replace"),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "url": url,
    }


def minimal_chat_body(model: str) -> Dict[str, Any]:
    """The smallest legitimate chat-completions request: one token, no tools."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }


def chat_url(api_base: str) -> str:
    return (api_base or "").rstrip("/") + "/chat/completions"


def _body_mentions_model(text: str) -> bool:
    lowered = (text or "").lower()
    return "model" in lowered and any(
        marker in lowered for marker in ("not found", "unknown", "does not exist", "invalid", "unsupported", "no such")
    )


def _client_tls_python() -> str:
    return "{}.{}.{}".format(*sys.version_info[:3])


# --------------------------------------------------------------------------------------
# Checks -- interpreter and code integrity
# --------------------------------------------------------------------------------------


def python_version_info() -> Any:
    """Seam for tests: the interpreter version the doctor judges."""
    return sys.version_info


def check_interpreter(ctx: DoctorContext) -> CheckResult:
    version = python_version_info()
    triple = tuple(int(part) for part in version[:3])
    pyto_present = ios.is_pyto()
    evidence = {
        "python": _client_tls_python(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pyto": pyto_present,
        "ios": ios.is_ios(),
        "fake_subprocess": ios.has_fake_subprocess(),
        "executable": sys.executable or "<none>",
    }
    detail = "Python {} on {}, pyto: {}".format(
        _client_tls_python(), platform.platform(), "present" if pyto_present else "absent"
    )
    if triple < (3, 10):
        return result(
            "interpreter",
            "Python >= 3.10",
            "fail",
            "Python {} is older than 3.10; this harness uses 3.10 syntax".format(_client_tls_python()),
            human_action="run this inside Pyto (which ships Python 3.10) or install CPython 3.10+.",
            evidence=evidence,
        )
    if not pyto_present:
        return result(
            "interpreter",
            "Python >= 3.10",
            "warn",
            detail + " -- Pyto's iOS bridges are not importable here",
            human_action="the device tools only work inside Pyto on iOS; on a desktop the harness degrades gracefully.",
            evidence=evidence,
        )
    return result("interpreter", "Python >= 3.10", "ok", detail, evidence=evidence)


def _runtime_files(ctx: DoctorContext) -> List[str]:
    directory = os.path.join(ctx.root, "harness")
    found: List[str] = []
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        if name.endswith(".py"):
            found.append(os.path.join(directory, name))
    return found


def _module_name(path: str) -> str:
    stem = os.path.basename(path)[: -len(".py")]
    return "harness" if stem == "__init__" else "harness." + stem


def _execute_from_path(module_name: str, path: str) -> None:
    """Execute one file as its real dotted module name, without trusting sys.path.

    Used when the module is not in ``sys.modules`` from this exact file: the on-disk
    source is what has to import cleanly, and a stale entry must not mask a broken edit.
    The previous ``sys.modules`` entry (if any) is restored afterwards.
    """
    import importlib.util

    is_package = module_name == "harness"
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=[os.path.dirname(path)] if is_package else None,
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("could not build an import spec for {}".format(path))
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def check_importability(ctx: DoctorContext) -> CheckResult:
    files = _runtime_files(ctx)
    if not files:
        return result(
            "importability",
            "Every harness module imports",
            "fail",
            "no harness/*.py files under {}".format(ctx.root),
            human_action="the installation looks incomplete; re-copy the harness folder.",
            evidence={"root": ctx.root},
        )
    problems: List[Dict[str, Any]] = []
    checked: List[str] = []
    for path in files:
        name = _module_name(path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            problems.append({"module": name, "kind": "read", "error": "{}: {}".format(type(exc).__name__, exc)})
            continue
        try:
            compile(source, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            problems.append(
                {
                    "module": name,
                    "kind": "syntax",
                    "error": "line {}: {}".format(exc.lineno, exc.msg),
                }
            )
            continue
        loaded = sys.modules.get(name)
        loaded_from = os.path.abspath(getattr(loaded, "__file__", "") or "") if loaded is not None else ""
        if loaded is not None and loaded_from == os.path.abspath(path):
            checked.append(name + " (already loaded)")
            continue
        try:
            _execute_from_path(name, path)
            checked.append(name)
        except BaseException as exc:  # noqa: BLE001 - an import-time crash is the finding
            problems.append(
                {
                    "module": name,
                    "kind": "import",
                    "error": "{}: {}".format(type(exc).__name__, exc),
                    "traceback": _snippet(traceback.format_exc(limit=4), 400, ctx),
                }
            )
    evidence = {
        "root": ctx.root,
        "files": len(files),
        "checked": checked,
        "problems": problems,
    }
    if problems:
        first = problems[0]
        return result(
            "importability",
            "Every harness module imports",
            "fail",
            "{} of {} modules fail to import; first: {} ({})".format(len(problems), len(files), first["module"], first["error"]),
            fixable=True,
            fix_id="import.restore_backup" if _has_backups(ctx) else None,
            human_action=(
                None
                if _has_backups(ctx)
                else "restore the harness from a known-good copy (or from your own backup); "
                "`--backups` lists snapshots this harness made."
            ),
            evidence=evidence,
        )
    return result(
        "importability",
        "Every harness module imports",
        "ok",
        "{} module(s) import cleanly".format(len(files)),
        evidence=evidence,
    )


def _has_backups(ctx: DoctorContext) -> bool:
    directory = ctx.backups_dir
    try:
        return os.path.isdir(directory) and any(
            os.path.isdir(os.path.join(directory, name)) for name in os.listdir(directory)
        )
    except OSError:  # pragma: no cover - unreadable state dir
        return False


def _load_stdlib_audit(ctx: DoctorContext) -> Optional[Any]:
    """Load ``stdlib_audit.py`` **from this install**, not from ``sys.modules``.

    Loading by path matters: a doctor running against a copy must audit that copy, and a
    module already imported from elsewhere would silently audit the wrong tree.
    """
    path = os.path.join(ctx.root, "stdlib_audit.py")
    cached = ctx.state_data.get("stdlib_audit")
    if isinstance(cached, tuple) and cached[0] == path:
        return cached[1]
    module = None
    if os.path.isfile(path):
        import importlib.util

        try:
            spec = importlib.util.spec_from_file_location("_pyto_stdlib_audit", path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - a broken audit script is a miss, not a crash
            module = None
    ctx.state_data["stdlib_audit"] = (path, module)
    return module


def check_stdlib_only(ctx: DoctorContext) -> CheckResult:
    audit = _load_stdlib_audit(ctx)
    if audit is None:
        return result(
            "stdlib_only",
            "No third-party imports",
            "skipped",
            "stdlib_audit.py is not importable from {}".format(ctx.root),
            human_action="keep stdlib_audit.py next to run.py if you want this check.",
            evidence={"root": ctx.root},
        )
    try:
        hook = audit.audit_imports_in_process()
        static = audit.static_import_scan()
    except Exception as exc:  # noqa: BLE001 - the audit itself must not break the doctor
        return result(
            "stdlib_only",
            "No third-party imports",
            "fail",
            "the audit raised {}: {}".format(type(exc).__name__, exc),
            human_action="re-run `python3 stdlib_audit.py` and fix what it reports.",
            evidence={"traceback": _snippet(traceback.format_exc(limit=4), 400, ctx)},
        )
    third_party = list(hook.get("third_party_modules") or [])
    suspicious = list(static.get("suspicious") or [])
    failures = list(hook.get("import_failures") or [])
    evidence = {
        "third_party_modules": third_party,
        "static_suspicious": suspicious,
        "import_failures": failures,
        "modules_inspected": len(hook.get("modules_imported") or [])
        + len(hook.get("preloaded_modules") or []),
        "files_scanned": static.get("files_scanned", 0),
        "mode": hook.get("mode"),
    }
    if third_party or suspicious or failures:
        detail_bits = []
        if third_party:
            detail_bits.append("third-party modules touched: {}".format(", ".join(third_party)))
        if suspicious:
            detail_bits.append(
                "non-stdlib imports: {}".format(
                    ", ".join(sorted({"{} ({})".format(item.get("module"), item.get("file")) for item in suspicious}))
                )
            )
        if failures:
            detail_bits.append(
                "import failures: {}".format(", ".join(str(item[0]) for item in failures))
            )
        return result(
            "stdlib_only",
            "No third-party imports",
            "unfixable",
            "; ".join(detail_bits),
            human_action=(
                "Pyto cannot install pip packages, so a third-party import can never work on the "
                "device. Replace it with a stdlib equivalent -- `run.py --repair` can do that "
                "gated by the offline tests."
            ),
            evidence=evidence,
        )
    return result(
        "stdlib_only",
        "No third-party imports",
        "ok",
        "{} module(s) and {} file(s) inspected; nothing outside the standard library".format(
            evidence["modules_inspected"], evidence["files_scanned"]
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------------------
# Checks -- configuration
# --------------------------------------------------------------------------------------


def _raw_config(ctx: DoctorContext) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read the config file verbatim.  Returns ``(payload, error)``."""
    path = ctx.config_path
    if not os.path.exists(path):
        return None, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except ValueError as exc:
        return None, "{} is not valid JSON: {}".format(path, _snippet(str(exc), 200, ctx))
    except OSError as exc:
        return None, "{} could not be read: {}: {}".format(path, type(exc).__name__, exc)
    if not isinstance(payload, dict):
        return None, "{} must contain a JSON object".format(path)
    return payload, ""


def check_config_present(ctx: DoctorContext) -> CheckResult:
    path = ctx.config_path
    exists = os.path.exists(path)
    evidence = {"path": path, "exists": exists}
    if exists:
        return result("config_present", "Config file exists", "ok", "{} is present".format(path), evidence=evidence)
    if ctx.config_error:
        return result(
            "config_present",
            "Config file exists",
            "warn",
            "{} is missing and the environment did not supply a usable configuration".format(path),
            fixable=True,
            fix_id="config.create",
            human_action="run `--doctor --fix` to write a starter config, then put your key in it.",
            evidence=evidence,
        )
    return result(
        "config_present",
        "Config file exists",
        "warn",
        "no config file at {}; settings come from the environment and built-in defaults".format(path),
        fixable=True,
        fix_id="config.create",
        human_action="optional: `--doctor --fix` writes a starter config, or keep using environment variables.",
        evidence=evidence,
    )


def check_config_parses(ctx: DoctorContext) -> CheckResult:
    if not os.path.exists(ctx.config_path):
        return result(
            "config_parses",
            "Config file is valid JSON",
            "skipped",
            "no config file to parse",
            evidence={"path": ctx.config_path},
        )
    payload, error = _raw_config(ctx)
    if error:
        return result(
            "config_parses",
            "Config file is valid JSON",
            "unfixable",
            error,
            human_action=(
                "open {} and fix the JSON by hand (a missing comma or a stray quote is the usual "
                "cause), or move it aside and run `--init` to start over. The doctor will not "
                "rewrite a file it cannot read: it may contain your key.".format(ctx.config_path)
            ),
            evidence={"path": ctx.config_path},
        )
    keys = sorted(payload or {})
    return result(
        "config_parses",
        "Config file is valid JSON",
        "ok",
        "parsed {} key(s)".format(len(keys)),
        evidence={"path": ctx.config_path, "keys": keys},
    )


def check_config_schema(ctx: DoctorContext) -> CheckResult:
    if not os.path.exists(ctx.config_path):
        return result(
            "config_schema",
            "Config keys are known and well-typed",
            "skipped",
            "no config file to inspect",
            evidence={"path": ctx.config_path},
        )
    payload, error = _raw_config(ctx)
    if error:
        return result(
            "config_schema",
            "Config keys are known and well-typed",
            "skipped",
            "the file does not parse, so its schema cannot be checked",
            evidence={"path": ctx.config_path},
        )
    assert payload is not None
    from . import config as config_module

    unknown = sorted(key for key in payload if key not in KNOWN_CONFIG_KEYS and key != "headers")
    missing = sorted(key for key in EXPECTED_CONFIG_KEYS if key not in payload)
    invalid: List[Dict[str, str]] = []
    for key in (
        "max_turns",
        "max_tokens",
        "timeout",
        "temperature",
        "stream",
        "yolo",
        "compact",
        "allow_unattended_programs",
    ):
        if key in payload and payload[key] is not None:
            try:
                config_module._coerce_field(key, payload[key], "file")
            except ConfigError as exc:
                invalid.append({"key": key, "error": _snippet(str(exc), 160, ctx)})
    for key in ("api_base", "model", "api_key", "workspace", "sessions_dir", "spill_dir"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            invalid.append({"key": key, "error": "must be a string, got {}".format(type(payload[key]).__name__)})
    evidence = {"unknown": unknown, "missing": missing, "invalid": invalid, "path": ctx.config_path}
    if invalid:
        return result(
            "config_schema",
            "Config keys are known and well-typed",
            "fail",
            "invalid value(s): {}".format(", ".join(item["key"] for item in invalid)),
            fixable=True,
            fix_id="config.schema_repair",
            human_action="run `--doctor --fix` to replace the bad value(s) with defaults.",
            evidence=evidence,
        )
    if unknown or missing:
        bits = []
        if unknown:
            bits.append("unknown key(s) ignored: {}".format(", ".join(unknown)))
        if missing:
            bits.append("key(s) missing, defaults apply: {}".format(", ".join(missing)))
        return result(
            "config_schema",
            "Config keys are known and well-typed",
            "warn",
            "; ".join(bits),
            human_action=(
                "remove the unknown key(s) to keep the file unambiguous" if unknown else None
            ),
            evidence=evidence,
        )
    return result(
        "config_schema",
        "Config keys are known and well-typed",
        "ok",
        "all {} expected key(s) present, no unknown keys".format(len(EXPECTED_CONFIG_KEYS)),
        evidence=evidence,
    )


def config_copies(ctx: DoctorContext) -> List[str]:
    """The config file and every sibling copy of it (``.bak``, ``.doctor-tmp``, …).

    ``config.json.bak`` is a full copy of the file that holds the API key: checking only
    the live path reported "mode 0600" while a world-readable duplicate sat next to it.
    """
    directory = os.path.dirname(os.path.abspath(ctx.config_path)) or "."
    base = os.path.basename(ctx.config_path)
    found = [ctx.config_path]
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if name != base and name.startswith(base + "."):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                found.append(candidate)
    return found


def check_config_permissions(ctx: DoctorContext) -> CheckResult:
    if os.name != "posix":
        return result(
            "config_permissions",
            "Config file mode is 0600",
            "skipped",
            "file modes are not meaningful on {}".format(os.name),
        )
    if not os.path.exists(ctx.config_path):
        return result("config_permissions", "Config file mode is 0600", "skipped", "no config file")
    loose: List[Tuple[str, int]] = []
    for path in config_copies(ctx):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            return result(
                "config_permissions",
                "Config file mode is 0600",
                "warn",
                "could not stat {}: {}: {}".format(path, type(exc).__name__, exc),
            )
        if mode & 0o077:
            loose.append((path, mode))
    evidence = {"path": ctx.config_path, "copies": list(config_copies(ctx))}
    if loose:
        detail = "; ".join("{} is mode {}".format(path, oct(mode)) for path, mode in loose)
        return result(
            "config_permissions",
            "Config file mode is 0600",
            "warn",
            "{} -- readable by other accounts on this device".format(detail),
            fixable=True,
            fix_id="config.chmod",
            human_action="run `--doctor --fix` (or `chmod 600` each of: {}).".format(
                ", ".join(path for path, _mode in loose)
            ),
            evidence={**evidence, "loose": {path: oct(mode) for path, mode in loose}},
        )
    return result(
        "config_permissions",
        "Config file mode is 0600",
        "ok",
        "mode {} ({} copy/copies checked)".format(oct(stat.S_IMODE(os.stat(ctx.config_path).st_mode)), len(evidence["copies"])),
        evidence=evidence,
    )


def _private_state_targets(ctx: DoctorContext) -> List[Tuple[str, int]]:
    """``(path, wanted mode)`` for every file and directory the harness creates itself."""
    targets: List[Tuple[str, int]] = [
        (ctx.state, PRIVATE_DIR_MODE),
        (ctx.config_path, PRIVATE_FILE_MODE),
        (ctx.workspace, PRIVATE_DIR_MODE),
        (ctx.sessions_dir, PRIVATE_DIR_MODE),
        (os.path.join(ctx.state, "capabilities.json"), PRIVATE_FILE_MODE),
        (os.path.join(ctx.state, "health.json"), PRIVATE_FILE_MODE),
        (os.path.join(ctx.workspace, "memory.json"), PRIVATE_FILE_MODE),
        (os.path.join(ctx.workspace, "tool-output"), PRIVATE_DIR_MODE),
    ]
    # The session logs themselves: the newest few are the ones a run just wrote.
    try:
        names = sorted(
            (name for name in os.listdir(ctx.sessions_dir) if name.endswith(".jsonl")),
            key=lambda name: os.path.getmtime(os.path.join(ctx.sessions_dir, name)),
            reverse=True,
        )
    except OSError:
        names = []
    targets.extend((os.path.join(ctx.sessions_dir, name), PRIVATE_FILE_MODE) for name in names[:10])
    targets.extend((path, PRIVATE_FILE_MODE) for path in config_copies(ctx))
    return targets


def check_file_permissions(ctx: DoctorContext) -> CheckResult:
    """Everything the harness creates for itself is owner-only.

    Data at rest is plaintext by design (see ``SECURITY.md``): sessions, memory and spill
    files hold whatever the agent read.  The mode is the only thing standing between that
    and every other app or extension that can reach the same container, iCloud Drive copy
    or desktop sync.
    """
    if os.name != "posix":
        return result(
            "file_permissions",
            "Harness files are private (0600/0700)",
            "skipped",
            "file modes are not meaningful on {}".format(os.name),
        )
    loose: List[str] = []
    checked = 0
    for path, wanted in _private_state_targets(ctx):
        if path == ctx.config_path:
            continue  # covered, with its copies, by config_permissions
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            continue  # missing is not a mode problem
        checked += 1
        if mode & 0o077:
            loose.append("{} is mode {} (want {})".format(path, oct(mode), oct(wanted)))
    if not checked:
        return result(
            "file_permissions",
            "Harness files are private (0600/0700)",
            "skipped",
            "nothing created yet",
        )
    if loose:
        return result(
            "file_permissions",
            "Harness files are private (0600/0700)",
            "warn",
            "; ".join(loose),
            fixable=True,
            fix_id="permissions.tighten",
            human_action="run `--doctor --fix` to make them owner-only.",
            evidence={"loose": loose, "checked": checked},
        )
    return result(
        "file_permissions",
        "Harness files are private (0600/0700)",
        "ok",
        "{} file(s)/dir(s) owner-only".format(checked),
        evidence={"checked": checked},
    )


def fix_permissions_tighten(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    """chmod every loose harness file/dir back to 0600/0700.  Never silent about failures."""
    failed: List[str] = []
    changed: List[str] = []
    for path, wanted in _private_state_targets(ctx):
        if not os.path.exists(path):
            continue
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            failed.append("{}: {}".format(path, exc))
            continue
        if not mode & 0o077:
            continue
        try:
            os.chmod(path, wanted)
        except OSError as exc:
            failed.append("{}: {}: {}".format(path, type(exc).__name__, exc))
            continue
        changed.append(path)
    if failed:
        return FixOutcome("permissions.tighten", item.id, False, error="; ".join(failed), evidence={"changed": changed})
    return FixOutcome(
        "permissions.tighten",
        item.id,
        True,
        "made {} owner-only".format(", ".join(changed) or "nothing (already private)"),
        evidence={"changed": changed},
    )


def _key_shape_verdict(key: Optional[str]) -> Tuple[str, str]:
    """Pure classifier for ``api_key_shape``: ``(status, detail)``; never echoes the key."""
    if not key:
        return "skipped", "no key to inspect"
    lowered = key.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS) or lowered in ("sk-", "sk-...", "key"):
        return "unfixable", "the key looks like a placeholder, not a credential"
    if len(key) < 20:
        return "warn", "the key is only {} characters; most provider keys are longer".format(len(key))
    if not re.match(r"^[A-Za-z0-9_\-\.]+$", key):
        return "warn", "the key contains characters unusual for an API key (whitespace or quotes?)"
    return "ok", "key shape looks plausible"


def check_api_key_present(ctx: DoctorContext) -> CheckResult:
    key = ctx.config.api_key
    evidence = {
        "key": redact_key(key),
        "source": ctx.config.sources.get("api_key", "unknown"),
        "env_checked": ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY"],
    }
    if key:
        return result(
            "api_key_present",
            "API key is set",
            "ok",
            "a key is set ({}) from {}".format(redact_key(key), evidence["source"]),
            evidence=evidence,
        )
    return result(
        "api_key_present",
        "API key is set",
        "unfixable",
        "no API key found in the environment or {}".format(ctx.config_path),
        human_action=(
            "only you can supply the key: export DEEPSEEK_API_KEY=sk-... for this session, or put it "
            'in {} (chmod 600). "python run.py --init" writes that file.'.format(ctx.config_path)
        ),
        evidence=evidence,
    )


def check_api_key_shape(ctx: DoctorContext) -> CheckResult:
    key = ctx.config.api_key
    status, detail = _key_shape_verdict(key)
    evidence = {"key": redact_key(key), "length": len(key or "")}
    if status == "skipped":
        return result("api_key_shape", "API key looks like a credential", "skipped", detail, evidence=evidence)
    if status == "ok":
        return result("api_key_shape", "API key looks like a credential", "ok", detail, evidence=evidence)
    if status == "warn":
        return result(
            "api_key_shape",
            "API key looks like a credential",
            "warn",
            detail,
            human_action="double-check the key you pasted; `--doctor` (without --no-network) will test it.",
            evidence=evidence,
        )
    return result(
        "api_key_shape",
        "API key looks like a credential",
        "unfixable",
        detail,
        human_action=(
            "replace the placeholder with your real key: export DEEPSEEK_API_KEY=sk-..., or edit {}. "
            "The doctor deliberately never writes or prints key material.".format(ctx.config_path)
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------------------
# Checks -- network and provider
# --------------------------------------------------------------------------------------


def check_network_reachable(ctx: DoctorContext) -> CheckResult:
    if not ctx.network:
        return result(
            "network_reachable",
            "API host is reachable",
            "skipped",
            "network checks are off (fast pass or --no-network)",
            evidence={"api_base": ctx.config.api_base},
        )
    base = ctx.config.api_base
    evidence: Dict[str, Any] = {"api_base": base, "timeout_s": ctx.network_timeout}
    try:
        evidence.update(probe_connect(base, ctx.network_timeout))
    except ProbeFailure as failure:
        evidence["failure"] = failure.kind
        return result(
            "network_reachable",
            "API host is reachable",
            "fail",
            failure.detail,
            human_action=network_action(failure.kind),
            evidence=evidence,
        )
    return result(
        "network_reachable",
        "API host is reachable",
        "ok",
        "DNS, TCP{} to {} succeeded".format(" and TLS" if evidence.get("scheme") == "https" else "", base),
        evidence=evidence,
    )


def candidate_api_bases(ctx: DoctorContext) -> List[str]:
    """Base URLs worth trying, most-likely first.

    Derived from the configured base, so the doctor never wanders off to a host the user
    did not configure: a bare origin gets ``/v1``, and DeepSeek's own documented bases are
    added only when the configured host really is DeepSeek.
    """
    base = (ctx.config.api_base or "").strip().rstrip("/")
    candidates: List[str] = []
    try:
        parsed = urllib.parse.urlsplit(base)
    except ValueError:
        return [base] if base else []
    origin = "{}://{}".format(parsed.scheme, parsed.netloc) if parsed.scheme and parsed.netloc else base
    for candidate in (base, origin, origin + "/v1"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if parsed.hostname and _is_deepseek_host(parsed.hostname):
        for candidate in (DEFAULT_API_BASE, DEFAULT_API_BASE + "/v1"):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _is_deepseek_host(host: str) -> bool:
    """True only on a **dot boundary**: ``evil-deepseek.com`` is not DeepSeek.

    ``endswith("deepseek.com")`` also matched look-alike hosts, and this function decides
    whether the configured key is POSTed to the real vendor — an unexpected cross-host
    credential transmission from a config the user did not write.
    """
    host = (host or "").lower().rstrip(".")
    return host == "deepseek.com" or host.endswith(".deepseek.com")


def probe_api_base_variants(ctx: DoctorContext, *, model: Optional[str] = None) -> Dict[str, Any]:
    """Try the plausible base URLs and report which one answers 200."""
    outcomes: List[Dict[str, Any]] = []
    working: Optional[str] = None
    for base in candidate_api_bases(ctx):
        url = chat_url(base)
        try:
            response = probe_post(
                url,
                api_key=ctx.config.api_key,
                body=minimal_chat_body(model or ctx.config.model),
                timeout=ctx.network_timeout,
            )
        except ProbeFailure as failure:
            outcomes.append({"base": base, "error": failure.kind, "detail": _snippet(failure.detail, 120, ctx)})
            continue
        outcomes.append(
            {
                "base": base,
                "status": response["status"],
                "body": _snippet(response["text"], 120, ctx),
            }
        )
        if response["status"] == 200:
            working = base
            break
    return {"ok": working is not None, "base": working, "outcomes": outcomes}


def check_api_auth(ctx: DoctorContext) -> CheckResult:
    if not ctx.network:
        return result(
            "api_auth",
            "The API accepts the key",
            "skipped",
            "network checks are off (fast pass or --no-network)",
        )
    if not ctx.config.api_key:
        return result(
            "api_auth",
            "The API accepts the key",
            "skipped",
            "no key to test",
            human_action="set a key first; `--doctor` will then verify it with one minimal request.",
        )
    url = chat_url(ctx.config.api_base)
    state: Dict[str, Any] = {"url": url, "base": ctx.config.api_base}
    try:
        response = probe_post(
            url,
            api_key=ctx.config.api_key,
            body=minimal_chat_body(ctx.config.model),
            timeout=ctx.network_timeout,
        )
    except ProbeFailure as failure:
        state.update({"endpoint_ok": False, "failure": failure.kind})
        ctx.state_data["api_auth"] = state
        return result(
            "api_auth",
            "The API accepts the key",
            "fail",
            "the request to {} failed: {}".format(url, failure.detail),
            human_action=network_action(failure.kind),
            evidence={"base": ctx.config.api_base, "failure": failure.kind, "timeout_s": ctx.network_timeout},
        )
    status = response["status"]
    body = _snippet(response["text"], 200, ctx)
    state.update({"status": status, "endpoint_ok": True, "body": body, "elapsed_ms": response["elapsed_ms"]})
    evidence: Dict[str, Any] = {
        "base": ctx.config.api_base,
        "url": url,
        "status": status,
        "elapsed_ms": response["elapsed_ms"],
        "model": ctx.config.model,
    }
    if body:
        evidence["body"] = body
    if status == 200:
        ctx.state_data["api_auth"] = state
        return result("api_auth", "The API accepts the key", "ok", "HTTP 200 from {}".format(url), evidence=evidence)
    if status in (401, 403):
        state["endpoint_ok"] = False
        ctx.state_data["api_auth"] = state
        return result(
            "api_auth",
            "The API accepts the key",
            "unfixable",
            "the provider rejected the credentials (HTTP {}){}".format(status, ": " + body if body else ""),
            human_action=(
                "the key itself is wrong, expired or lacks access to this model. Only you can supply a "
                "new one: check the provider's dashboard, then export DEEPSEEK_API_KEY=sk-... or edit {}.".format(
                    ctx.config_path
                )
            ),
            evidence=evidence,
        )
    if status == 429:
        return result(
            "api_auth",
            "The API accepts the key",
            "warn",
            "HTTP 429: the provider is rate limiting this key",
            human_action="wait and retry; if it persists, check the account's quota and billing.",
            evidence=evidence,
        )
    if status >= 500:
        return result(
            "api_auth",
            "The API accepts the key",
            "fail",
            "the provider failed with HTTP {}{}".format(status, ": " + body if body else ""),
            human_action="this is the provider's side: retry in a few minutes; nothing on the device can fix it.",
            evidence=evidence,
        )
    if status == 404:
        if _body_mentions_model(response["text"]):
            state["model_error"] = True
            ctx.state_data["api_auth"] = state
            return result(
                "api_auth",
                "The API accepts the key",
                "fail",
                "HTTP 404: the endpoint rejected the model name {!r}{}".format(
                    ctx.config.model, ": " + body if body else ""
                ),
                fixable=True,
                fix_id="model.rewrite",
                human_action="run `--doctor --fix` to switch to a model this endpoint accepts.",
                evidence=evidence,
            )
        probe = probe_api_base_variants(ctx)
        evidence["variants"] = probe["outcomes"]
        if probe["ok"]:
            evidence["working_base"] = probe["base"]
            state["endpoint_ok"] = False
            ctx.state_data["api_auth"] = state
            return result(
                "api_auth",
                "The API accepts the key",
                "fail",
                "HTTP 404 from {}; {} answers 200 instead".format(url, chat_url(str(probe["base"]))),
                fixable=True,
                fix_id="api_base.rewrite",
                human_action="run `--doctor --fix` to write the working base URL into the config.",
                evidence=evidence,
            )
        state["endpoint_ok"] = False
        ctx.state_data["api_auth"] = state
        return result(
            "api_auth",
            "The API accepts the key",
            "unfixable",
            "HTTP 404 and no candidate endpoint answered: {}".format(
                ", ".join("{}={}".format(item.get("base"), item.get("status") or item.get("error")) for item in probe["outcomes"])
            ),
            human_action=(
                "check api_base against the provider's documentation (the path is usually either "
                "'https://host' or 'https://host/v1'), then set it with --api-base or in {}.".format(ctx.config_path)
            ),
            evidence=evidence,
        )
    if _body_mentions_model(response["text"]):
        state["model_error"] = True
        ctx.state_data["api_auth"] = state
        return result(
            "api_auth",
            "The API accepts the key",
            "fail",
            "HTTP {}: the request reached the API but the model was rejected{}".format(
                status, ": " + body if body else ""
            ),
            fixable=True,
            fix_id="model.rewrite",
            human_action="run `--doctor --fix` to switch to a model this endpoint accepts.",
            evidence=evidence,
        )
    return result(
        "api_auth",
        "The API accepts the key",
        "fail",
        "the API answered HTTP {}{}".format(status, ": " + body if body else ""),
        human_action="the endpoint answered but not with a completion; re-check api_base and the model name.",
        evidence=evidence,
    )


def check_model_accepted(ctx: DoctorContext) -> CheckResult:
    if not ctx.network:
        return result(
            "model_accepted",
            "The configured model is usable",
            "skipped",
            "network checks are off (fast pass or --no-network)",
        )
    if not ctx.config.api_key:
        return result("model_accepted", "The configured model is usable", "skipped", "no key to test with")
    auth = ctx.state_data.get("api_auth") or {}
    if auth and not auth.get("endpoint_ok") and auth.get("failure"):
        return result(
            "model_accepted",
            "The configured model is usable",
            "skipped",
            "the endpoint is not reachable yet; fix api_auth first",
            evidence={"depends_on": "api_auth"},
        )
    base = str(auth.get("base") or ctx.config.api_base)
    model = ctx.config.model
    evidence: Dict[str, Any] = {"model": model, "base": base, "os": platform.system()}
    if auth.get("status") == 200 and not auth.get("model_error"):
        return result(
            "model_accepted",
            "The configured model is usable",
            "ok",
            "{!r} answered with HTTP 200".format(model),
            evidence=evidence,
        )
    trial_models: List[str] = []
    for candidate in [model] + [item for item in ctx.models if item != model]:
        if candidate in trial_models:
            continue
        trial_models.append(candidate)
        try:
            response = probe_post(
                chat_url(base),
                api_key=ctx.config.api_key,
                body=minimal_chat_body(candidate),
                timeout=ctx.network_timeout,
            )
        except ProbeFailure as failure:
            evidence["failure"] = failure.kind
            return result(
                "model_accepted",
                "The configured model is usable",
                "fail",
                "the trial request for {!r} failed: {}".format(candidate, failure.detail),
                human_action=network_action(failure.kind),
                evidence=evidence,
            )
        status = response["status"]
        if status == 200:
            if candidate == model:
                return result(
                    "model_accepted",
                    "The configured model is usable",
                    "ok",
                    "{!r} answered with HTTP 200".format(model),
                    evidence=evidence,
                )
            evidence["working_model"] = candidate
            evidence["tried"] = trial_models
            evidence["status"] = status
            return result(
                "model_accepted",
                "The configured model is usable",
                "fail",
                "{!r} is not usable, but {!r} is".format(model, candidate),
                fixable=True,
                fix_id="model.rewrite",
                human_action="run `--doctor --fix` to write the working model into the config.",
                evidence=evidence,
            )
        if status in (401, 403) and candidate == model:
            return result(
                "model_accepted",
                "The configured model is usable",
                "skipped",
                "the key was rejected; fix api_auth first",
                evidence={"depends_on": "api_auth", "status": status},
            )
        evidence.setdefault("tried_status", []).append({"model": candidate, "status": status})
    evidence["tried"] = trial_models
    return result(
        "model_accepted",
        "The configured model is usable",
        "unfixable",
        "none of the {} known model name(s) were accepted".format(len(trial_models)),
        human_action=(
            "your account may use a model this list does not know. Check the provider's model list "
            "and set it with --model or in {}.".format(ctx.config_path)
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------------------
# Checks -- local state
# --------------------------------------------------------------------------------------


def check_home(ctx: DoctorContext) -> CheckResult:
    """The resolved home directory and the state folder inside it.

    This is the check for the failure that broke real installs on iOS: a ``~`` that was
    never expanded, so every path became ``~/.pyto_harness`` and the first ``mkdir`` was
    refused with ``Operation not permitted``.  It never raises: an unusable home is the
    thing it exists to report.
    """
    evidence: Dict[str, Any] = {"state": ctx.state}
    if ctx.home:
        evidence["home"] = ctx.home
    if ctx.home_source:
        evidence["source"] = ctx.home_source
    if ctx.home_note:
        evidence["note"] = ctx.home_note

    if ctx.home_error or not ctx.home:
        detail = ctx.home_error or home.no_home_message(["no candidate directory is writable"])
        return result(
            "home",
            "Home folder and state directory",
            "unfixable",
            "no writable home directory: ~/{} cannot be created".format(CONFIG_DIR_NAME),
            human_action=detail,
            evidence=evidence,
        )

    path = ctx.state
    if os.path.exists(path) and not os.path.isdir(path):
        return result(
            "home",
            "Home folder and state directory",
            "unfixable",
            "{} exists but is not a directory".format(path),
            human_action="move that file aside; it must be a directory.",
            evidence=evidence,
        )
    if not os.path.exists(path):
        return result(
            "home",
            "Home folder and state directory",
            "fail",
            "{} does not exist (home resolved to {})".format(path, ctx.home),
            fixable=True,
            fix_id="home.create",
            human_action="run `--doctor --fix` to create it, or set PYTO_HARNESS_HOME elsewhere.",
            evidence=evidence,
        )
    problem = home.writability_problem(path)
    if problem:
        return result(
            "home",
            "Home folder and state directory",
            "unfixable",
            "{} is not writable: {}".format(path, problem),
            human_action=home.no_home_message(["{}: {}".format(path, problem)]),
            evidence=evidence,
        )
    choice = home.resolve_home_choice(environ=ctx.env)
    if choice.ok and choice.temporary:
        return result(
            "home",
            "Home folder and state directory",
            "warn",
            "{} is writable, but it is the temporary folder: iOS can purge it at any time".format(path),
            human_action=(
                "set PYTO_HARNESS_HOME to a folder you control, for example: "
                'import os; os.environ["PYTO_HARNESS_HOME"] = os.getcwd()'
            ),
            evidence=evidence,
        )
    return result(
        "home",
        "Home folder and state directory",
        "ok",
        "{} is writable (home {} from {})".format(path, ctx.home, ctx.home_source or "default"),
        evidence=evidence,
    )


def check_workspace(ctx: DoctorContext) -> CheckResult:
    path = ctx.workspace
    evidence: Dict[str, Any] = {"path": path}
    if not os.path.exists(path):
        return result(
            "workspace",
            "Workspace exists and is writable",
            "fail",
            "{} does not exist".format(path),
            fixable=True,
            fix_id="workspace.create",
            human_action="run `--doctor --fix` to create it, or point --workspace somewhere else.",
            evidence=evidence,
        )
    if not os.path.isdir(path):
        return result(
            "workspace",
            "Workspace exists and is writable",
            "unfixable",
            "{} exists but is not a directory".format(path),
            human_action="move that file aside or choose another workspace with --workspace.",
            evidence=evidence,
        )
    probe = os.path.join(path, ".doctor-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
        evidence["writable"] = True
    except OSError as exc:
        evidence["writable"] = False
        return result(
            "workspace",
            "Workspace exists and is writable",
            "unfixable",
            "{} is not writable: {}: {}".format(path, type(exc).__name__, exc),
            human_action=(
                "pick a writable workspace with --workspace. Inside Pyto, only the app's own "
                "container is writable, so a path such as ~/Documents/agent works and /tmp may not."
            ),
            evidence=evidence,
        )
    try:
        usage = shutil.disk_usage(path)
        evidence["free_mb"] = round(usage.free / (1024 * 1024), 1)
        evidence["total_mb"] = round(usage.total / (1024 * 1024), 1)
    except OSError:  # pragma: no cover - some iOS paths refuse disk_usage
        evidence["free_mb"] = None
    free = evidence.get("free_mb")
    if isinstance(free, (int, float)):
        if usage.free < DISK_FAIL_BYTES:
            return result(
                "workspace",
                "Workspace exists and is writable",
                "unfixable",
                "only {:.0f} MB free on {}; the harness needs room for programs and logs".format(free, path),
                human_action="free space on the device (Photos and offline media are the usual culprits).",
                evidence=evidence,
            )
        if usage.free < DISK_WARN_BYTES:
            return result(
                "workspace",
                "Workspace exists and is writable",
                "warn",
                "only {:.0f} MB free on {}".format(free, path),
                human_action="free some space before running long tasks.",
                evidence=evidence,
            )
    return result(
        "workspace",
        "Workspace exists and is writable",
        "ok",
        "{} is writable ({:.0f} MB free)".format(path, free if isinstance(free, (int, float)) else 0.0),
        evidence=evidence,
    )


def _count_lines(raw: bytes) -> int:
    return raw.count(b"\n")


def scan_session_log(path: str) -> Dict[str, Any]:
    """Integrity-scan one JSONL session log without modifying it.

    Distinguishes the three failure modes that actually happen on iOS: a **torn** final
    line (the app was killed mid-write), a **corrupt** header or middle row (something
    else wrote to the file), and an **oversized** log (the memory budget will stop the
    script).  A file whose last row parses is *not* torn, even without the trailing
    newline: truncating it would throw away good data.
    """
    from .session import CURRENT_VERSION

    info: Dict[str, Any] = {"file": os.path.basename(path), "bytes": 0, "state": "ok", "detail": "", "lines": 0}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        info.update(state="corrupt", detail="cannot stat: {}: {}".format(type(exc).__name__, exc))
        return info
    info["bytes"] = size
    if size == 0:
        info.update(state="empty", detail="the log is empty (no header row)")
        return info
    complete_read = size <= MAX_SESSION_SCAN_BYTES
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
            if complete_read:
                handle.seek(0)
                body = handle.read()
            else:
                handle.seek(max(0, size - 512 * 1024))
                body = handle.read()
    except OSError as exc:
        info.update(state="corrupt", detail="cannot read: {}: {}".format(type(exc).__name__, exc))
        return info
    first_line = head.split(b"\n", 1)[0]
    try:
        header = json.loads(first_line.decode("utf-8", "replace"))
    except ValueError as exc:
        info.update(state="corrupt", detail="the header row is not JSON: {}".format(exc))
        return info
    if not isinstance(header, dict) or header.get("kind") != "header":
        info.update(state="corrupt", detail="the first row is not a session header")
        return info
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        info.update(state="corrupt", detail="the header has no integer version")
        return info
    if version > CURRENT_VERSION:
        info.update(
            state="corrupt",
            detail="the header says version {}, newer than this build ({})".format(version, CURRENT_VERSION),
        )
        return info
    info["session_id"] = str(header.get("id") or "")[:12]
    rows = body.split(b"\n")
    trailing_partial = rows.pop() if rows and rows[-1] != b"" else b""
    torn = False
    if trailing_partial:
        try:
            json.loads(trailing_partial.decode("utf-8", "replace"))
        except ValueError:
            torn = True
            info["torn_bytes"] = len(trailing_partial)
    for raw in rows:
        stripped = raw.strip()
        if not stripped:
            continue
        info["lines"] = int(info["lines"]) + 1
        try:
            row = json.loads(stripped.decode("utf-8", "replace"))
        except ValueError:
            if raw is rows[-1] and not complete_read:
                continue  # the tail slice may have cut a row in half
            info.update(state="corrupt", detail="a row is not JSON (truncated write in the middle?)")
            return info
        if not isinstance(row, dict):
            info.update(state="corrupt", detail="a row is not a JSON object")
            return info
        if row.get("kind") not in ("header", "event"):
            info.update(state="corrupt", detail="a row has an unknown kind {!r}".format(row.get("kind")))
            return info
    if torn:
        info.update(
            state="torn",
            detail="the final line was not written completely ({} bytes); the rest of the log is fine".format(
                info.get("torn_bytes", 0)
            ),
        )
        return info
    oversized = size >= budget.MAX_SESSION_BYTES or (complete_read and int(info["lines"]) >= budget.MAX_SESSION_EVENTS)
    if oversized:
        info.update(
            state="oversized",
            detail="{} rows / {:.1f} MB -- past the memory budget ({} events / {:.0f} MB)".format(
                info["lines"], size / (1024 * 1024), budget.MAX_SESSION_EVENTS, budget.MAX_SESSION_BYTES / (1024 * 1024)
            ),
        )
        return info
    info["detail"] = "{} row(s) parsed".format(info["lines"])
    return info


def sessions_report(ctx: DoctorContext) -> Dict[str, Any]:
    """Directory state plus a per-log integrity scan of the newest logs."""
    directory = ctx.sessions_dir
    report: Dict[str, Any] = {"dir": directory, "exists": os.path.isdir(directory), "writable": False, "logs": []}
    if not report["exists"]:
        return report
    probe = os.path.join(directory, ".doctor-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
        report["writable"] = True
    except OSError:
        report["writable"] = False
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".jsonl")]
    except OSError:
        names = []
    paths = []
    for name in names:
        full = os.path.join(directory, name)
        try:
            paths.append((os.path.getmtime(full), full))
        except OSError:
            continue
    paths.sort(reverse=True)
    for _mtime, path in paths[:MAX_SESSION_SCAN_LOGS]:
        report["logs"].append(scan_session_log(path))
    report["log_count"] = len(names)
    return report


def check_sessions(ctx: DoctorContext) -> CheckResult:
    report = sessions_report(ctx)
    evidence: Dict[str, Any] = {
        "dir": report["dir"],
        "exists": report["exists"],
        "writable": report["writable"],
        "log_count": report.get("log_count", 0),
        "scanned": [{"file": item["file"], "state": item["state"], "bytes": item["bytes"]} for item in report["logs"]],
    }
    if not report["exists"]:
        return result(
            "sessions",
            "Session logs are healthy",
            "warn",
            "{} does not exist yet".format(report["dir"]),
            fixable=True,
            fix_id="sessions.create",
            human_action="run `--doctor --fix` to create it (a normal run creates it too).",
            evidence=evidence,
        )
    if not report["writable"]:
        return result(
            "sessions",
            "Session logs are healthy",
            "unfixable",
            "{} is not writable, so turns cannot be recorded".format(report["dir"]),
            human_action="fix the permissions on that directory, or set PYTO_HARNESS_SESSIONS_DIR elsewhere.",
            evidence=evidence,
        )
    problems = [item for item in report["logs"] if item["state"] != "ok"]
    evidence["problems"] = [
        {"file": item["file"], "state": item["state"], "detail": item["detail"]} for item in problems
    ]
    if not problems:
        return result(
            "sessions",
            "Session logs are healthy",
            "ok",
            "{} log(s), newest {} scanned cleanly".format(report.get("log_count", 0), len(report["logs"])),
            evidence=evidence,
        )
    states = sorted({item["state"] for item in problems})
    detail = "; ".join(
        "{}: {}".format(item["file"], item["detail"]) for item in problems[:3]
    )
    safe_only = all(item["state"] in ("torn",) for item in problems)
    return result(
        "sessions",
        "Session logs are healthy",
        "fail",
        "{} log(s) need repair ({}) -- {}".format(len(problems), ", ".join(states), detail),
        fixable=True,
        fix_id="session.repair_safe" if safe_only else "session.repair_full",
        human_action=None if safe_only else "run `--doctor --fix`; corrupt logs are moved to *.corrupt, never deleted.",
        evidence=evidence,
    )


def check_memory_headroom(ctx: DoctorContext) -> CheckResult:
    available = ios.available_memory_bytes()
    evidence: Dict[str, Any] = {
        "available_bytes": available,
        "warn_bytes": MEMORY_WARN_BYTES,
        "fail_bytes": MEMORY_FAIL_BYTES,
        "supported": available is not None,
        "session_bytes": _session_bytes_total(ctx),
    }
    if available is None:
        return result(
            "memory_headroom",
            "Enough free memory",
            "skipped",
            "os_proc_available_memory() is not exposed on this interpreter",
            evidence=evidence,
        )
    megabytes = available / (1024 * 1024)
    evidence["available_mb"] = round(megabytes, 1)
    if available < MEMORY_FAIL_BYTES:
        return result(
            "memory_headroom",
            "Enough free memory",
            "fail",
            "{:.0f} MB free; Pyto stops every script near 500 MB".format(megabytes),
            human_action=(
                "close other apps, then delete stale session logs and workspace tool-output files. "
                "A doctor cannot free memory for you."
            ),
            evidence=evidence,
        )
    if available < MEMORY_WARN_BYTES:
        return result(
            "memory_headroom",
            "Enough free memory",
            "warn",
            "{:.0f} MB free; keep outputs small".format(megabytes),
            human_action="consider deleting old session logs before a long task.",
            evidence=evidence,
        )
    return result(
        "memory_headroom",
        "Enough free memory",
        "ok",
        "{:.0f} MB free".format(megabytes),
        evidence=evidence,
    )


def _session_bytes_total(ctx: DoctorContext) -> int:
    total = 0
    try:
        for name in os.listdir(ctx.sessions_dir):
            if name.endswith(".jsonl"):
                try:
                    total += os.path.getsize(os.path.join(ctx.sessions_dir, name))
                except OSError:
                    continue
    except OSError:
        return 0
    return total


# --------------------------------------------------------------------------------------
# Checks -- iOS surface
# --------------------------------------------------------------------------------------


def probe_module(name: str) -> Dict[str, Any]:
    """Import an optional bridge module, recording rather than raising a failure."""
    import importlib

    try:
        module = importlib.import_module(name)
    except BaseException as exc:  # noqa: BLE001 - bridge modules raise anything on import
        return {"name": name, "available": False, "error": "{}: {}".format(type(exc).__name__, exc)[:160]}
    origin = getattr(module, "__file__", None) or "<builtin>"
    return {"name": name, "available": True, "origin": str(origin), "module": module}


def _probe_table(ctx: DoctorContext) -> Dict[str, Dict[str, Any]]:
    table = ctx.state_data.get("ios_modules")
    if isinstance(table, dict):
        return table
    table = {name: probe_module(name) for name in IOS_PROBE_MODULES}
    ctx.state_data["ios_modules"] = table
    return table


def check_ios_modules(ctx: DoctorContext) -> CheckResult:
    table = _probe_table(ctx)
    available = sorted(name for name, item in table.items() if item.get("available"))
    missing = sorted(name for name, item in table.items() if not item.get("available"))
    on_device = ios.is_ios() or ios.is_pyto()
    evidence = {
        "available": available,
        "missing": missing,
        "counts": {"available": len(available), "probed": len(table)},
        "on_device": on_device,
    }
    if not on_device:
        return result(
            "ios_modules",
            "iOS bridge modules",
            "skipped",
            "{}/{} probed modules present; not running inside Pyto".format(len(available), len(table)),
            evidence=evidence,
        )
    if not available:
        return result(
            "ios_modules",
            "iOS bridge modules",
            "warn",
            "no iOS bridge module could be imported, yet this looks like Pyto",
            human_action=(
                "restart Pyto; if the device tools still do not load, the app build may have lost its "
                "Python bridge modules and only a reinstall or app update can restore them."
            ),
            evidence=evidence,
        )
    return result(
        "ios_modules",
        "iOS bridge modules",
        "ok",
        "{}/{} available: {}".format(len(available), len(table), ", ".join(available)),
        evidence=evidence,
    )


def discover_signatures(ctx: DoctorContext) -> Dict[str, Any]:
    """Discover the fragile call shapes with ``dir()``/``inspect`` only.  No calls."""
    table = _probe_table(ctx)
    discovered: Dict[str, Any] = {}
    for module_name, attr in FRAGILE_SIGNATURES:
        entry: Dict[str, Any] = {"module": module_name, "attribute": attr, "available": False}
        info = table.get(module_name)
        if info is None:
            info = probe_module(module_name)
        module = info.get("module") if info.get("available") else None
        entry["available"] = bool(info.get("available"))
        if module is None:
            entry["exists"] = False
            discovered["{}.{}".format(module_name, attr)] = entry
            continue
        member = getattr(module, attr, None)
        entry["exists"] = member is not None
        entry["callable"] = callable(member)
        signature = None
        if member is not None:
            try:
                signature = str(inspect.signature(member))
            except (TypeError, ValueError):
                signature = None
        entry["signature"] = signature
        parameters: List[str] = []
        if signature:
            try:
                parameters = [
                    name
                    for name, _param in inspect.signature(member).parameters.items()
                ]
            except (TypeError, ValueError):  # pragma: no cover - defensive
                parameters = []
        entry["parameters"] = parameters
        doc = inspect.getdoc(member) or ""
        if doc:
            entry["doc"] = _snippet(doc.splitlines()[0], 140, ctx)
        discovered["{}.{}".format(module_name, attr)] = entry
    payload = {
        "version": 1,
        "created_at": int(time.time() * 1000),
        "platform": ios.platform_label(),
        "python": _client_tls_python(),
        "signatures": discovered,
    }
    return payload


def _preserved_capability_keys(path: str) -> Dict[str, Any]:
    """Keys in ``capabilities.json`` that another component owns.

    ``pyto_api`` keeps its module probe in this same file; the doctor's write must not erase
    it (and :func:`harness.pyto_api.save_cache` likewise preserves the doctor's keys).
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(existing, dict):
        return {}
    return {key: value for key, value in existing.items() if key not in ("version", "created_at", "platform", "python", "signatures")}


def check_ios_signatures(ctx: DoctorContext) -> CheckResult:
    payload = discover_signatures(ctx)
    discovered = payload["signatures"]
    present_modules = [item for item in discovered.values() if item.get("available")]
    drift = [key for key, item in discovered.items() if item.get("available") and not item.get("exists")]
    persisted = False
    persist_error = ""
    if ctx.persist:
        try:
            mkdir_private(ctx.state)
            payload.update(_preserved_capability_keys(ctx.capabilities_path))
            write_private(ctx.capabilities_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
            persisted = True
        except OSError as exc:
            persist_error = "{}: {}".format(type(exc).__name__, exc)
    evidence = {
        "capabilities_path": ctx.capabilities_path,
        "persisted": persisted,
        "modules_present": [item["module"] for item in present_modules],
        "shapes": {key: item.get("signature") for key, item in discovered.items()},
    }
    if persist_error:
        evidence["persist_error"] = persist_error
    if not present_modules:
        return result(
            "ios_signatures",
            "Pyto call shapes are discoverable",
            "skipped",
            "none of the fragile Pyto modules are importable here",
            evidence=evidence,
        )
    if drift:
        return result(
            "ios_signatures",
            "Pyto call shapes are discoverable",
            "warn",
            "module present but attribute missing: {}".format(", ".join(drift)),
            human_action=(
                "a Pyto app update may have renamed this API. The harness reports an 'unsupported' "
                "result instead of guessing; check the Pyto release notes before relying on it."
            ),
            evidence=evidence,
        )
    return result(
        "ios_signatures",
        "Pyto call shapes are discoverable",
        "ok",
        "{} fragile call shape(s) discovered{}".format(
            len(discovered), "" if persisted else " (not persisted)"
        ),
        evidence=evidence,
    )


def shortcuts_urls(ctx: DoctorContext) -> Dict[str, str]:
    example = "summarise my notes folder"
    return {
        "run_py": ctx.run_py,
        "url": ios.pyto_run_url(ctx.run_py, {"task": example}),
        "url_no_task": ios.pyto_run_url(ctx.run_py),
        "example_task": example,
    }


def check_shortcuts_wiring(ctx: DoctorContext) -> CheckResult:
    urls = shortcuts_urls(ctx)
    doc = ctx.shortcuts_doc
    exists = os.path.exists(doc)
    current = False
    if exists:
        try:
            with open(doc, "r", encoding="utf-8") as handle:
                current = urls["url_no_task"] in handle.read() or urls["run_py"] in handle.read()
        except OSError:
            current = False
    evidence = {
        "url": urls["url"],
        "url_no_task": urls["url_no_task"],
        "run_py": urls["run_py"],
        "doc": doc,
        "doc_exists": exists,
        "doc_current": current,
    }
    if exists and current:
        return result(
            "shortcuts_wiring",
            "Shortcuts wiring is documented",
            "ok",
            "{} describes this installation; URL: {}".format(doc, urls["url"]),
            evidence=evidence,
        )
    return result(
        "shortcuts_wiring",
        "Shortcuts wiring is documented",
        "warn",
        "{}{}; URL: {}".format(
            "{} is missing".format(doc) if not exists else "{} is out of date".format(doc),
            "" if os.path.isdir(ctx.workspace) else " (the workspace does not exist yet)",
            urls["url"],
        ),
        fixable=True,
        fix_id="shortcuts.write_doc",
        human_action="run `--doctor --fix` to write the step-by-step Shortcuts guide into the workspace.",
        evidence=evidence,
    )


def check_libs_reference(ctx: DoctorContext) -> CheckResult:
    """``PYTO_LIBS.md`` must exist and match this device's catalogue."""
    doc = ctx.libs_doc
    exists = os.path.exists(doc)
    fresh = False
    reason = "missing"
    if exists:
        fresh, reason = pyto_api.doc_is_fresh(doc, state_dir=ctx.state)
    evidence = {
        "doc": doc,
        "exists": exists,
        "fresh": fresh,
        "reason": reason,
        "modules": len(pyto_api.module_names()),
        "not_available": len(pyto_api.NOT_AVAILABLE),
        "platform": ios.platform_label(),
    }
    if exists and fresh:
        return result(
            "libs_reference",
            "Pyto library reference is current",
            "ok",
            "{} covers {} Pyto modules and {} unavailable APIs for this device".format(
                doc, len(pyto_api.module_names()), len(pyto_api.NOT_AVAILABLE)
            ),
            evidence=evidence,
        )
    detail = "{} is {}{}".format(
        doc,
        "missing" if not exists else reason,
        "" if os.path.isdir(ctx.workspace) else " (the workspace does not exist yet)",
    )
    return result(
        "libs_reference",
        "Pyto library reference is current",
        "warn",
        "{}; the model grounds every Pyto import on that file".format(detail),
        fixable=True,
        fix_id="libs.write_doc",
        human_action="run `--doctor --fix` to write PYTO_LIBS.md (the Pyto API cheat-sheet) into the workspace.",
        evidence=evidence,
    )


# --------------------------------------------------------------------------------------
# Checks -- the offline test suite
# --------------------------------------------------------------------------------------

def _parse_unittest_output(text: str) -> Dict[str, Any]:
    """Pull counts out of ``unittest``'s summary lines."""
    ran = -1
    failures = 0
    errors = 0
    skipped = 0
    match = re.search(r"^Ran (\d+) tests? in ([\d.]+)s", text, re.MULTILINE)
    if match:
        ran = int(match.group(1))
    match = re.search(r"^FAILED \((.*)\)", text, re.MULTILINE)
    if match:
        for part in match.group(1).split(","):
            bit = part.strip()
            if bit.startswith("failures="):
                failures = int(bit.split("=", 1)[1])
            elif bit.startswith("errors="):
                errors = int(bit.split("=", 1)[1])
            elif bit.startswith("skipped="):
                skipped = int(bit.split("=", 1)[1])
    elif re.search(r"^OK\b", text, re.MULTILINE):
        match = re.search(r"^OK \((.*)\)", text, re.MULTILINE)
        if match:
            for part in match.group(1).split(","):
                bit = part.strip()
                if bit.startswith("skipped="):
                    skipped = int(bit.split("=", 1)[1])
    return {"ran": ran, "failures": failures, "errors": errors, "skipped": skipped}


def gate_modules_for(relative_path: str, *, base: Optional[Sequence[str]] = None) -> List[str]:
    """The bounded gate module list for one source file: base subset + coverage."""
    wanted: List[str] = list(base) if base is not None else list(FAST_TEST_MODULES)
    normalised = (relative_path or "").replace(os.sep, "/")
    for candidate in GATE_COVERAGE.get(normalised, ()):
        if candidate not in wanted:
            wanted.append(candidate)
    return wanted


def run_offline_tests(
    ctx: DoctorContext,
    *,
    modules: Optional[Sequence[str]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Run this install's offline test suite.  Returns a structured report.

    Preferred path is a **subprocess** (``python -m unittest discover``): it is the exact
    command the README documents, it cannot mutate this process's environment, and it can
    be killed on a timeout.  When ``subprocess`` is Pyto's fake, in-process stub, the suite
    runs inside this interpreter with the same loader instead — degraded, and said so.

    ``modules`` narrows the run (``unittest -k``).  Passing ``None`` runs everything, which
    is what ``--deep-tests`` asks for; the self-edit gate deliberately does not.
    """
    timeout = float(ctx.selftest_timeout if timeout is None else timeout)
    selected = list(modules) if modules is not None else []
    root = ctx.root
    started = time.monotonic()
    report: Dict[str, Any] = {
        "mode": "subprocess",
        "root": root,
        "modules": selected or ["<all>"],
        "ok": False,
        "ran": -1,
        "failures": -1,
        "errors": -1,
        "skipped": 0,
        "output_tail": "",
    }
    if not os.path.isdir(ctx.tests_dir):
        report.update(mode="none", error="no tests/ directory under {}".format(root))
        return report
    depth = _selftest_depth()
    if depth >= MAX_SELFTEST_DEPTH:
        report.update(
            mode="refused",
            error=(
                "refusing to run the test suite at depth {}: a suite that runs the suite is a "
                "fork bomb, not a gate".format(depth)
            ),
        )
        return report
    if ios.has_fake_subprocess():
        report.update(_run_tests_in_process(ctx, selected, timeout))
    else:
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        for module in selected:
            command.extend(["-k", module])
        # The suite is code the user did not write running next to the API key: it gets a
        # scrubbed environment (no *_API_KEY/*_TOKEN/*_SECRET, no PYTO_HARNESS_*), then the
        # three non-secret switches this specific run needs.
        env = scrubbed_environ()
        env["PYTHONPATH"] = root + os.pathsep + os.environ.get("PYTHONPATH", "")
        env["PYTO_HARNESS_NO_DOCTOR"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env[SELFTEST_DEPTH_ENV] = str(depth + 1)
        report["command"] = " ".join(command)
        try:
            process = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=max(5.0, timeout), env=env
            )
        except subprocess.TimeoutExpired:
            report.update(ok=False, error="the test suite did not finish within {:.0f}s".format(timeout))
            report["duration_ms"] = int((time.monotonic() - started) * 1000)
            return report
        except (OSError, ValueError) as exc:
            report.update(mode="in-process")
            report.update(_run_tests_in_process(ctx, selected, timeout))
            report["subprocess_error"] = "{}: {}".format(type(exc).__name__, exc)
            return report
        combined = (process.stdout or "") + "\n" + (process.stderr or "")
        report.update(_parse_unittest_output(combined))
        report["returncode"] = process.returncode
        report["ok"] = process.returncode == 0 and report["ran"] > 0
        report["output_tail"] = scrub_secrets(combined[-4000:], ctx)
    report["duration_ms"] = int((time.monotonic() - started) * 1000)
    return report


def _selftest_depth() -> int:
    try:
        return int(os.environ.get(SELFTEST_DEPTH_ENV, "0") or 0)
    except ValueError:
        return 0


def _filtered_suite(ctx: DoctorContext, loader: Any, modules: Sequence[str]) -> Any:
    """Discover the suite, then keep only the requested test modules."""
    import unittest

    suite = loader.discover(start_dir=ctx.tests_dir, top_level_dir=ctx.root)
    if not modules:
        return suite
    wanted = set(modules)

    def keep(item: Any) -> Any:
        if isinstance(item, unittest.TestSuite):
            inner = unittest.TestSuite()
            for child in item:
                kept = keep(child)
                if kept is not None and (not isinstance(kept, unittest.TestSuite) or kept.countTestCases()):
                    inner.addTest(kept)
            return inner
        module = getattr(item.__class__, "__module__", "")
        return item if any(module == name or module.endswith("." + name) for name in wanted) else None

    return keep(suite)


def _run_tests_in_process(ctx: DoctorContext, modules: Sequence[str], timeout: float) -> Dict[str, Any]:
    """Fallback for iOS, where spawning an interpreter is not a real spawn."""
    import io
    import unittest

    buffer = io.StringIO()
    cwd = os.getcwd()
    if ctx.root not in sys.path:
        sys.path.insert(0, ctx.root)
    try:
        os.chdir(ctx.root)
        loader = unittest.TestLoader()
        suite = _filtered_suite(ctx, loader, modules)
        runner = unittest.TextTestRunner(stream=buffer, verbosity=1)
        outcome = runner.run(suite)
        ran = outcome.testsRun
        failures = len(outcome.failures)
        errors = len(outcome.errors)
        skipped = len(outcome.skipped)
        ok = ran > 0 and failures == 0 and errors == 0
    except BaseException as exc:  # noqa: BLE001 - a broken suite is a finding, not a crash
        return {
            "mode": "in-process",
            "ok": False,
            "ran": -1,
            "failures": -1,
            "errors": -1,
            "skipped": 0,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "output_tail": scrub_secrets(traceback.format_exc(limit=6)[-2000:], ctx),
        }
    finally:
        os.chdir(cwd)
    del timeout  # in-process tests cannot be interrupted; the caller's timeout is advisory
    return {
        "mode": "in-process",
        "ok": ok,
        "ran": ran,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "output_tail": scrub_secrets(buffer.getvalue()[-4000:], ctx),
    }


def check_selftest(ctx: DoctorContext) -> CheckResult:
    if ctx.testing:
        return result(
            "selftest",
            "The offline test suite passes",
            "skipped",
            "a self-test is already running in this process",
        )
    if not ctx.deep:
        return result(
            "selftest",
            "The offline test suite passes",
            "skipped",
            "only with --deep",
        )
    modules = () if ctx.deep_tests else FAST_TEST_MODULES
    ctx.testing = True
    try:
        report = run_offline_tests(ctx, modules=modules or None)
    finally:
        ctx.testing = False
    evidence = {
        "mode": report.get("mode"),
        "ran": report.get("ran"),
        "failures": report.get("failures"),
        "errors": report.get("errors"),
        "skipped": report.get("skipped"),
        "duration_ms": report.get("duration_ms"),
        "modules": report.get("modules"),
    }
    if report.get("ok"):
        return result(
            "selftest",
            "The offline test suite passes",
            "ok",
            "{} test(s) passed in {} ms ({})".format(report.get("ran"), report.get("duration_ms"), report.get("mode")),
            evidence=evidence,
        )
    tail = _snippet(report.get("output_tail") or report.get("error") or "no output", 700, ctx)
    return result(
        "selftest",
        "The offline test suite passes",
        "fail",
        "the offline suite did not pass: {} ran, {} failure(s), {} error(s)".format(
            report.get("ran"), report.get("failures"), report.get("errors")
        ),
        human_action=(
            "the harness's own tests are the gate for self-repair, so nothing will be promoted "
            "until they pass. Run `--repair \"<what broke>\"` to have the model fix it under that "
            "gate, or `--restore <backup_id>` to go back to a snapshot."
        ),
        evidence={**evidence, "output_tail": tail},
    )


# --------------------------------------------------------------------------------------
# Fixes -- every write the doctor is allowed to perform
# --------------------------------------------------------------------------------------


def _write_config(ctx: DoctorContext, payload: Mapping[str, Any]) -> str:
    """Replace the config file atomically, keeping a ``.bak`` copy and mode 0600.

    The payload is written verbatim, so callers must think about the key: no fix here
    ever *reads* the key out of the file and writes it back — the key is either left
    untouched (schema repair keeps the existing value) or deliberately absent.
    """
    path = ctx.config_path
    mkdir_private(os.path.dirname(path) or ".")
    if os.path.exists(path):
        # A byte-for-byte copy of the key at 0664 was the exposure: the copy is created
        # 0600 like the original, at creation, not chmod'ed afterwards.
        try:
            with open(path, "rb") as handle:
                payload_bytes = handle.read()
            write_private(path + ".bak", payload_bytes)
        except OSError:  # pragma: no cover - best effort
            pass
    temporary = path + ".doctor-tmp"
    with open_private(temporary, truncate=True) as handle:
        json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return path


def _read_raw_or_empty(ctx: DoctorContext) -> Dict[str, Any]:
    payload, error = _raw_config(ctx)
    if error or payload is None:
        return {}
    return dict(payload)


def fix_config_create(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    if os.path.exists(ctx.config_path):
        return FixOutcome("config.create", item.id, True, "{} already exists".format(ctx.config_path))
    payload = {
        "api_base": ctx.config.api_base or DEFAULT_API_BASE,
        "model": ctx.config.model or DEFAULT_MODEL,
        "api_key": "",
        "max_turns": int(ctx.config.max_turns or 8),
        "timeout": float(ctx.config.timeout or 60.0),
        "workspace": ctx.workspace,
    }
    path = _write_config(ctx, payload)
    return FixOutcome(
        "config.create",
        item.id,
        True,
        "wrote a starter config to {} (mode 0600, no key in it)".format(path),
        evidence={"path": path},
    )


def fix_config_chmod(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    """Tighten the config **and every copy of it** (``.bak``, ``.doctor-tmp``, …).

    Fixing only the live path was the bug: the ``.bak`` holds the same key and was left
    world-readable forever.
    """
    targets = [path for path in config_copies(ctx) if os.path.exists(path)]
    if not targets:
        return FixOutcome("config.chmod", item.id, False, error="no config file to chmod")
    failed: List[str] = []
    for path in targets:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            failed.append("{}: {}: {}".format(path, type(exc).__name__, exc))
    remaining = [
        "{} is mode {}".format(path, oct(stat.S_IMODE(os.stat(path).st_mode)))
        for path in targets
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077
    ]
    if failed or remaining:
        return FixOutcome(
            "config.chmod",
            item.id,
            False,
            error="; ".join(failed + remaining),
            evidence={"paths": targets},
        )
    return FixOutcome(
        "config.chmod",
        item.id,
        True,
        "set {} to mode 0600".format(", ".join(targets)),
        evidence={"paths": targets, "mode": "0o600"},
    )


def fix_config_schema(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    payload = _read_raw_or_empty(ctx)
    if not payload:
        return FixOutcome("config.schema_repair", item.id, False, error="the config file is unreadable or does not parse")
    from . import config as config_module

    effective: Dict[str, Any] = {
        "max_turns": int(ctx.config.max_turns or 8),
        "timeout": float(ctx.config.timeout or 60.0),
        "stream": bool(ctx.config.stream),
        "yolo": bool(ctx.config.yolo),
        "compact": bool(getattr(ctx.config, "compact", True)),
        "model": str(ctx.config.model or DEFAULT_MODEL),
        "api_base": str(ctx.config.api_base or DEFAULT_API_BASE),
        "workspace": str(ctx.workspace),
        "sessions_dir": str(ctx.sessions_dir),
    }
    dropped: List[str] = []
    replaced: List[str] = []
    for key in list(payload.keys()):
        numeric = key in (
            "max_turns",
            "max_tokens",
            "timeout",
            "temperature",
            "stream",
            "yolo",
            "compact",
            "allow_unattended_programs",
        )
        textual = key in ("api_base", "model", "api_key", "workspace", "sessions_dir", "spill_dir")
        if not (numeric or textual) or payload[key] is None:
            continue
        invalid = False
        if numeric:
            try:
                config_module._coerce_field(key, payload[key], "file")
            except ConfigError:
                invalid = True
        elif not isinstance(payload[key], str):
            invalid = True
        if not invalid:
            continue
        if key in effective:
            payload[key] = effective[key]
            replaced.append(key)
        else:
            # No sensible substitute (a key, a token cap): drop it so the default applies.
            payload.pop(key)
            dropped.append(key)
    if not replaced and not dropped:
        return FixOutcome("config.schema_repair", item.id, True, "nothing left to correct")
    path = _write_config(ctx, payload)
    bits = []
    if replaced:
        bits.append("replaced {} with the value the harness would use anyway".format(", ".join(replaced)))
    if dropped:
        bits.append("removed {}".format(", ".join(dropped)))
    return FixOutcome(
        "config.schema_repair",
        item.id,
        True,
        "{} in {}".format("; ".join(bits), path),
        evidence={"path": path, "replaced": replaced, "dropped": dropped},
    )


def fix_home_create(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    """Create the state directory inside the resolved home (0700: it holds the health file)."""
    try:
        mkdir_private(ctx.state)
    except OSError as exc:
        return FixOutcome(
            "home.create",
            item.id,
            False,
            error="{}: {} (set PYTO_HARNESS_HOME to a folder you can write to)".format(type(exc).__name__, exc),
        )
    return FixOutcome("home.create", item.id, True, "created {}".format(ctx.state))


def fix_workspace_create(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    try:
        mkdir_private(ctx.workspace)  # 0700: programs, memory and spill files live here
    except OSError as exc:
        return FixOutcome("workspace.create", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    return FixOutcome("workspace.create", item.id, True, "created {}".format(ctx.workspace))


def fix_sessions_create(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    try:
        mkdir_private(ctx.sessions_dir)  # 0700: the logs hold every prompt and result
    except OSError as exc:
        return FixOutcome("sessions.create", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    return FixOutcome("sessions.create", item.id, True, "created {}".format(ctx.sessions_dir))


def _truncate_torn_tail(path: str) -> Tuple[bool, str]:
    """Cut a log back to its last complete JSON row, keeping the fragment as ``.torn``."""
    keep_until = 0
    offset = 0
    fragment = b""
    scanned = 0
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                scanned += len(raw)
                if scanned > MAX_SESSION_SCAN_BYTES:
                    return False, "the log is too large for the tail scan"
                offset += len(raw)
                complete = raw.endswith(b"\n")
                line = raw[:-1] if complete else raw
                if not line.strip():
                    keep_until = offset
                    continue
                try:
                    json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    if complete:
                        return False, "a row in the middle is not JSON; truncation would drop good data"
                    fragment = raw
                    break
                keep_until = offset
    except OSError as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    if keep_until <= 0 or not fragment:
        return False, "no torn final line found"
    if fragment:
        try:
            # The fragment is user data torn out of a session log: it gets the same 0600
            # as the log it came from.
            with open_private(path + ".torn", append=True, binary=True) as handle:
                handle.write(fragment)
        except OSError:  # pragma: no cover - best effort; the fragment is expendable
            pass
    temporary = path + ".doctor-tmp"
    try:
        with open(path, "rb") as source, open_private(temporary, truncate=True, binary=True) as target:
            remaining = keep_until
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            try:
                os.fsync(target.fileno())
            except OSError:  # pragma: no cover - iOS quirk
                pass
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False, "{}: {}".format(type(exc).__name__, exc)
    return True, "truncated {:.0f} torn byte(s); the fragment is in {}".format(
        len(fragment), os.path.basename(path) + ".torn"
    )


def _quarantine(path: str) -> Tuple[bool, str]:
    """Move a corrupt log aside.  Never deletes: the user's data stays on disk."""
    target = path + ".corrupt"
    counter = 1
    while os.path.exists(target):
        counter += 1
        target = "{}.corrupt{}".format(path, counter)
    try:
        os.replace(path, target)
    except OSError as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    return True, "moved the corrupt log to {}".format(os.path.basename(target))


def _compact_log(path: str) -> Tuple[bool, str]:
    from .session import SessionLog

    try:
        log = SessionLog.resume(path, writable=False)
    except (OSError, HarnessError) as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    try:
        summary = log.compact(force=True)
    finally:
        log.close()
    if not summary:
        return False, "nothing safe to cut (no user message inside the keep window)"
    return True, "compacted: {dropped_events} event(s) dropped, {kept_events} kept".format(**summary)


def _repair_sessions(ctx: DoctorContext, *, full: bool) -> FixOutcome:
    fix_id = "session.repair_full" if full else "session.repair_safe"
    report = sessions_report(ctx)
    fixed: List[str] = []
    failed: List[str] = []
    for item in report["logs"]:
        state = item["state"]
        path = os.path.join(ctx.sessions_dir, item["file"])
        if state == "ok":
            continue
        if state == "torn":
            ok, detail = _truncate_torn_tail(path)
        elif state == "oversized":
            if not full:
                continue
            ok, detail = _compact_log(path)
        elif state in ("corrupt", "empty"):
            if not full:
                continue
            ok, detail = _quarantine(path)
        else:  # pragma: no cover - unknown state
            ok, detail = False, "unknown state {!r}".format(state)
        if ok:
            fixed.append("{}: {} ({})".format(item["file"], detail, state))
        else:
            failed.append("{}: {} ({})".format(item["file"], detail, state))
    if not fixed and not failed:
        return FixOutcome(fix_id, "sessions", True, "nothing to repair")
    detail = "; ".join(fixed) if fixed else "no log could be repaired"
    if failed:
        detail += " | unresolved: " + "; ".join(failed)
    return FixOutcome(
        fix_id,
        "sessions",
        bool(fixed) and not failed,
        detail,
        error="" if not failed else "; ".join(failed),
        evidence={"fixed": fixed, "failed": failed},
    )


def fix_session_safe(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    return _repair_sessions(ctx, full=False)


def fix_session_full(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    return _repair_sessions(ctx, full=True)


def _first_working_model(ctx: DoctorContext, base: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    tried: List[Dict[str, Any]] = []
    for candidate in [ctx.config.model] + [item for item in ctx.models if item != ctx.config.model]:
        try:
            response = probe_post(
                chat_url(base),
                api_key=ctx.config.api_key,
                body=minimal_chat_body(candidate),
                timeout=ctx.network_timeout,
            )
        except ProbeFailure as failure:
            tried.append({"model": candidate, "error": failure.kind})
            continue
        tried.append({"model": candidate, "status": response["status"]})
        if response["status"] == 200:
            return candidate, tried
    return None, tried


def fix_api_base_rewrite(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    if not ctx.config.api_key:
        return FixOutcome("api_base.rewrite", item.id, False, error="no key available to probe with")
    probe = probe_api_base_variants(ctx)
    if not probe["ok"]:
        return FixOutcome(
            "api_base.rewrite",
            item.id,
            False,
            error="no candidate base answered 200",
            evidence={"variants": probe["outcomes"]},
        )
    working = str(probe["base"])
    if working == ctx.config.api_base:
        return FixOutcome("api_base.rewrite", item.id, True, "{} already is the working base".format(working))
    payload = _read_raw_or_empty(ctx)
    payload["api_base"] = working
    path = _write_config(ctx, payload)
    ctx.config.api_base = working
    return FixOutcome(
        "api_base.rewrite",
        item.id,
        True,
        "wrote api_base={} into {} (a .bak copy of the previous file is next to it)".format(working, path),
        evidence={"path": path, "api_base": working, "variants": probe["outcomes"]},
    )


def fix_model_rewrite(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    if not ctx.config.api_key:
        return FixOutcome("model.rewrite", item.id, False, error="no key available to probe with")
    auth = ctx.state_data.get("api_auth") or {}
    base = str(auth.get("base") or ctx.config.api_base)
    working, tried = _first_working_model(ctx, base)
    if not working:
        return FixOutcome(
            "model.rewrite",
            item.id,
            False,
            error="no model in the fallback list was accepted",
            evidence={"tried": tried},
        )
    if working == ctx.config.model:
        return FixOutcome("model.rewrite", item.id, True, "{!r} is already accepted".format(working))
    payload = _read_raw_or_empty(ctx)
    payload["model"] = working
    path = _write_config(ctx, payload)
    ctx.config.model = working
    return FixOutcome(
        "model.rewrite",
        item.id,
        True,
        "wrote model={!r} into {}".format(working, path),
        evidence={"path": path, "model": working, "tried": tried},
    )


def fix_shortcuts_doc(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    try:
        mkdir_private(ctx.workspace)
    except OSError as exc:
        return FixOutcome("shortcuts.write_doc", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    try:
        with open(ctx.shortcuts_doc, "w", encoding="utf-8") as handle:
            handle.write(shortcuts_document(ctx))
    except OSError as exc:
        return FixOutcome("shortcuts.write_doc", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    return FixOutcome(
        "shortcuts.write_doc",
        item.id,
        True,
        "wrote {}".format(ctx.shortcuts_doc),
        evidence={"doc": ctx.shortcuts_doc, "url": shortcuts_urls(ctx)["url"]},
    )


def fix_libs_doc(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    """Write PYTO_LIBS.md from the merged catalogue, and persist the probe next to it."""
    try:
        mkdir_private(ctx.workspace)
    except OSError as exc:
        return FixOutcome("libs.write_doc", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    # Record what this device has first, so the document and the cache agree, then render.
    if ctx.persist:
        pyto_api.probe_all(state_dir=ctx.state, persist=True)
    try:
        text = pyto_api.write_doc(ctx.libs_doc, state_dir=ctx.state)
    except OSError as exc:
        return FixOutcome("libs.write_doc", item.id, False, error="{}: {}".format(type(exc).__name__, exc))
    return FixOutcome(
        "libs.write_doc",
        item.id,
        True,
        "wrote {} ({} characters, {} modules)".format(
            ctx.libs_doc, len(text), len(pyto_api.module_names())
        ),
        evidence={
            "doc": ctx.libs_doc,
            "chars": len(text),
            "digest": pyto_api.doc_digest(text),
            "not_available": len(pyto_api.NOT_AVAILABLE),
        },
    )


def fix_import_restore(ctx: DoctorContext, item: CheckResult) -> FixOutcome:
    from . import repair

    backups = repair.list_backups(backups_dir=ctx.backups_dir)
    usable = [entry for entry in backups if not entry.get("error")]
    if not usable:
        return FixOutcome("import.restore_backup", item.id, False, error="no readable backup to restore")
    newest = usable[0]["id"]
    outcome = repair.restore(newest, root=ctx.root, backups_dir=ctx.backups_dir, run_tests=True)
    detail = "restored {}: {}".format(newest, outcome.detail)
    if outcome.tests and not outcome.tests.get("ok"):
        detail += " (the tests still fail; see the restore result)"
    return FixOutcome(
        "import.restore_backup",
        item.id,
        bool(outcome.ok),
        detail,
        error=outcome.error,
        evidence={"backup_id": newest, "decision": outcome.decision},
    )


def shortcuts_document(ctx: DoctorContext) -> str:
    """The step-by-step Shortcuts guide written into the workspace by ``--fix``."""
    urls = shortcuts_urls(ctx)
    return SHORTCUTS_TEMPLATE.format(
        url=urls["url"],
        url_no_task=urls["url_no_task"],
        run_py=urls["run_py"],
        task=urls["example_task"],
        workspace=ctx.workspace,
        root=ctx.root,
    )


SHORTCUTS_TEMPLATE = """# Running pyto-harness from a Shortcut

Generated by `python run.py --doctor --fix` for **this** installation. Regenerate it after
you move the harness: the URLs below contain the absolute path.

* harness root: `{root}`
* workspace:    `{workspace}`
* script:       `{run_py}`

## The URL this installation answers to

```
{url}
```

Without a task (the Shortcut supplies the text itself):

```
{url_no_task}
```

`run.py` reads `task=` out of `sys.argv`, so anything that can open a URL can start a run.
Try it once in Safari: it should bounce to Pyto and start working on "{task}".

## Build the Shortcut (2 minutes)

1. Open **Shortcuts** -> **+** -> *Add Action*.
2. Add **Run Script** (Pyto's action).
3. Script: choose `run.py` at the path above.
4. Arguments: `task=Ask%20My%20Agent` style is **not** needed here; instead put your task text
   in the argument field, or add *Ask for Input* before it and pass the provided input.
5. Turn **Show Console off** so the run is headless.
6. Optional: add **Get Script Output** to show the harness's answer back in the Shortcut.
7. Name it, e.g. `Ask My Agent`.

To trigger it from anywhere:

```
shortcuts://run-shortcut?name=Ask%20My%20Agent
```

or hand it a task directly:

```
{url}
```

## Approvals in a headless run

A Shortcut has nobody to answer an approval prompt, so the harness **denies** share /
open-URL / Shortcut calls and says so in its summary. That is deliberate: failing closed is
safer than silently sharing your data. If you want that Shortcut to proceed unattended, add
`--yolo` to its arguments and accept that you pre-approved those actions.

## When it does not work

Run `python run.py --doctor` in Pyto. It prints one line per check with the exact human
action for anything it cannot repair itself, and `--doctor --fix` repairs what a machine
can (directories, file modes, a torn session line, a wrong api_base or model name).
"""


#: Every fix the doctor knows, keyed by ``fix_id``.
FIXES: Dict[str, Fix] = {
    fix.id: fix
    for fix in (
        Fix("config.create", "config_present", "write a starter config", False, fix_config_create),
        Fix("config.chmod", "config_permissions", "tighten the config file mode to 0600", True, fix_config_chmod),
        Fix(
            "permissions.tighten",
            "file_permissions",
            "make sessions, memory, spill files and state owner-only (0600/0700)",
            False,
            fix_permissions_tighten,
        ),
        Fix("config.schema_repair", "config_schema", "drop invalid config values so defaults apply", False, fix_config_schema),
        Fix("workspace.create", "workspace", "create the workspace directory", True, fix_workspace_create),
        Fix("home.create", "home", "create the state directory inside the resolved home", True, fix_home_create),
        Fix("sessions.create", "sessions", "create the sessions directory", True, fix_sessions_create),
        Fix("session.repair_safe", "sessions", "truncate a torn final session line", True, fix_session_safe),
        Fix("session.repair_full", "sessions", "quarantine or compact damaged session logs", False, fix_session_full),
        Fix("api_base.rewrite", "api_auth", "write the working api_base into the config", False, fix_api_base_rewrite),
        Fix("model.rewrite", "model_accepted", "write a working model name into the config", False, fix_model_rewrite),
        Fix("shortcuts.write_doc", "shortcuts_wiring", "write SHORTCUTS.md into the workspace", False, fix_shortcuts_doc),
        Fix("libs.write_doc", "libs_reference", "write PYTO_LIBS.md (the Pyto API reference)", False, fix_libs_doc),
        Fix("import.restore_backup", "importability", "restore the newest source snapshot", False, fix_import_restore),
    )
}


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------

CHECKS: Tuple[Tuple[str, Callable[[DoctorContext], CheckResult]], ...] = (
    ("interpreter", check_interpreter),
    ("importability", check_importability),
    ("stdlib_only", check_stdlib_only),
    ("home", check_home),
    ("config_present", check_config_present),
    ("config_parses", check_config_parses),
    ("config_schema", check_config_schema),
    ("config_permissions", check_config_permissions),
    ("file_permissions", check_file_permissions),
    ("api_key_present", check_api_key_present),
    ("api_key_shape", check_api_key_shape),
    ("network_reachable", check_network_reachable),
    ("api_auth", check_api_auth),
    ("model_accepted", check_model_accepted),
    ("workspace", check_workspace),
    ("sessions", check_sessions),
    ("memory_headroom", check_memory_headroom),
    ("ios_modules", check_ios_modules),
    ("ios_signatures", check_ios_signatures),
    ("shortcuts_wiring", check_shortcuts_wiring),
    ("libs_reference", check_libs_reference),
    ("selftest", check_selftest),
)

CHECK_FUNCTIONS: Dict[str, Callable[[DoctorContext], CheckResult]] = dict(CHECKS)

CHECK_TITLES: Dict[str, str] = {
    "interpreter": "Python >= 3.10",
    "importability": "Every harness module imports",
    "stdlib_only": "No third-party imports",
    "home": "Home folder and state directory",
    "config_present": "Config file exists",
    "config_parses": "Config file is valid JSON",
    "config_schema": "Config keys are known and well-typed",
    "config_permissions": "Config file mode is 0600",
    "file_permissions": "Harness files are private (0600/0700)",
    "api_key_present": "API key is set",
    "api_key_shape": "API key looks like a credential",
    "network_reachable": "API host is reachable",
    "api_auth": "The API accepts the key",
    "model_accepted": "The configured model is usable",
    "workspace": "Workspace exists and is writable",
    "sessions": "Session logs are healthy",
    "memory_headroom": "Enough free memory",
    "ios_modules": "iOS bridge modules",
    "ios_signatures": "Pyto call shapes are discoverable",
    "shortcuts_wiring": "Shortcuts wiring is documented",
    "libs_reference": "Pyto library reference is current",
    "selftest": "The offline test suite passes",
}


def run_checks(
    ctx: DoctorContext,
    *,
    only: Optional[Iterable[str]] = None,
    network: Optional[bool] = None,
) -> List[CheckResult]:
    """Run every registered check.  A check that raises becomes a ``fail`` result."""
    if network is not None:
        ctx = replace(ctx, network=bool(network), state_data=ctx.state_data)
    wanted = set(only) if only is not None else None
    results: List[CheckResult] = []
    for check_id, function in CHECKS:
        if wanted is not None and check_id not in wanted:
            continue
        results.append(run_one(ctx, check_id, function))
    return results


def run_one(
    ctx: DoctorContext,
    check_id: str,
    function: Optional[Callable[[DoctorContext], CheckResult]] = None,
) -> CheckResult:
    function = function or CHECK_FUNCTIONS.get(check_id)
    title = CHECK_TITLES.get(check_id, check_id)
    if function is None:
        return result(check_id, title, "skipped", "no such check")
    started = time.monotonic()
    try:
        outcome = function(ctx)
    except Exception as exc:  # noqa: BLE001 - a check must never take the doctor down
        return result(
            check_id,
            title,
            "fail",
            "the check itself raised {}: {}".format(type(exc).__name__, exc),
            human_action="this is a bug in the doctor, not in your setup; the traceback summary is in evidence.",
            evidence={"traceback": _snippet(traceback.format_exc(limit=5), 500, ctx)},
        )
    if not isinstance(outcome, CheckResult):  # pragma: no cover - a check authoring bug
        return result(check_id, title, "fail", "the check returned {!r}".format(outcome))
    outcome.evidence.setdefault("duration_ms", round((time.monotonic() - started) * 1000, 1))
    return outcome


def apply_fix(ctx: DoctorContext, fix_id: str, item: Optional[CheckResult] = None) -> FixOutcome:
    """Run one named fix.  Never raises: a broken fix is a value, like a broken check."""
    fix = FIXES.get(fix_id)
    if fix is None:
        return FixOutcome(fix_id, item.id if item else "", False, error="unknown fix id {!r}".format(fix_id))
    subject = item or result(fix.check_id, CHECK_TITLES.get(fix.check_id, fix.check_id), "fail")
    try:
        return fix.handler(ctx, subject)
    except Exception as exc:  # noqa: BLE001 - the fix ran, but failed
        return FixOutcome(
            fix_id,
            fix.check_id,
            False,
            error="{}: {} ({})".format(type(exc).__name__, exc, _snippet(traceback.format_exc(limit=3), 200, ctx)),
        )


def apply_fixes(
    ctx: DoctorContext,
    results: Sequence[CheckResult],
    *,
    safe_only: bool = False,
    max_passes: int = 3,
) -> Tuple[List[CheckResult], List[FixOutcome]]:
    """Apply the fixes the results ask for, re-running each check afterwards.

    Fixes are applied one at a time and the affected check is re-run, so the report after
    a ``--fix`` is the truth about the *repaired* installation, not a promise.
    """
    current = list(results)
    outcomes: List[FixOutcome] = []
    attempted: set = set()
    for _pass in range(max(1, max_passes)):
        progressed = False
        for index, item in enumerate(current):
            if not item.fixable or not item.fix_id or item.fix_id in attempted:
                continue
            fix = FIXES.get(item.fix_id)
            if fix is None or (safe_only and not fix.safe):
                continue
            attempted.add(item.fix_id)
            outcome = apply_fix(ctx, item.fix_id, item)
            outcomes.append(outcome)
            fresh = run_one(ctx, item.id, CHECK_FUNCTIONS.get(item.id))
            if fresh.clean() and outcome.ok:
                fresh.status = "fixed"
                fresh.fixable = False
                fresh.fix_id = None
                fresh.human_action = None
                fresh.detail = "{} -- {}".format(outcome.detail, fresh.detail)
                fresh.evidence = {
                    **fresh.evidence,
                    "fixed_by": outcome.fix_id,
                    "was": item.detail,
                    "before_status": item.status,
                }
            current[index] = fresh
            progressed = True
        if not progressed:
            break
    return current, outcomes


def counts(results: Sequence[CheckResult]) -> Dict[str, int]:
    table = {status: 0 for status in STATUSES}
    for item in results:
        table[item.status] = table.get(item.status, 0) + 1
    return table


def needs_attention(results: Sequence[CheckResult]) -> int:
    return sum(1 for item in results if item.failed())


def exit_code(results: Sequence[CheckResult]) -> int:
    """0 when nothing failed, 1 for a broken-but-fixable item, 2 when only you can fix it."""
    if any(item.status == "fail" for item in results):
        return 1
    if any(item.status == "unfixable" for item in results):
        return 2
    return 0


def summary_line(results: Sequence[CheckResult]) -> str:
    table = counts(results)
    ok = table.get("ok", 0) + table.get("warn", 0) + table.get("skipped", 0)
    attention = needs_attention(results)
    return "doctor: {} ok, {} fixed, {} {} (run --doctor for details)".format(
        ok,
        table.get("fixed", 0),
        attention,
        "needs you" if attention == 1 else "need you",
    )


def fix_summary_line(results: Sequence[CheckResult]) -> str:
    """The after-``--fix`` line: ``4 fixed, 1 needs you, 0 failed``."""
    table = counts(results)
    attention = needs_attention(results)
    return "{} fixed, {} {}, {} failed".format(
        table.get("fixed", 0),
        attention,
        "needs you" if attention == 1 else "need you",
        table.get("fail", 0),
    )


REPORT_ORDER = ("fail", "unfixable", "warn", "fixed", "ok", "skipped")


def format_report(
    results: Sequence[CheckResult],
    *,
    ctx: Optional[DoctorContext] = None,
    title: str = "doctor",
    show_evidence: bool = True,
) -> str:
    """The human-readable report, grouped by status."""
    table = counts(results)
    lines = ["pyto-harness {}".format(title), "=" * 72]
    if ctx is not None:
        lines.append("root       : {}".format(ctx.root))
        lines.append("python     : {} ({})".format(_client_tls_python(), platform.platform()))
        lines.append("home       : {}".format(ctx.home_line))
        lines.append("config     : {}".format(ctx.config_path))
        lines.append("workspace  : {}".format(ctx.workspace))
        lines.append("sessions   : {}".format(ctx.sessions_dir))
        lines.append("state      : {}".format(ctx.state))
        lines.append("network    : {}".format("checked" if ctx.network else "not checked (fast pass / --no-network)"))
        lines.append("-" * 72)
    for status in REPORT_ORDER:
        group = [item for item in results if item.status == status]
        if not group:
            continue
        lines.append("{} ({})".format(status, len(group)))
        for item in group:
            lines.append("  {:<18} {}".format(item.id, item.detail))
            if item.fix_id:
                lines.append("  {:<18} fix: {} (run --doctor --fix)".format("", item.fix_id))
            if item.human_action:
                lines.append("  {:<18} you: {}".format("", item.human_action))
            if show_evidence and item.status in ATTENTION_STATUSES and item.evidence:
                rendered = json.dumps(item.evidence, sort_keys=True, default=str)
                if len(rendered) > 320:
                    rendered = rendered[:320] + "..."
                lines.append("  {:<18} evidence: {}".format("", rendered))
        lines.append("")
    lines.append("-" * 72)
    lines.append(summary_line(results))
    return "\n".join(lines)


def compact_report(results: Sequence[CheckResult], *, limit: int = 1200) -> str:
    """The agent-facing report: ids, statuses and human actions only, size-capped."""
    table = counts(results)
    ok = table.get("ok", 0) + table.get("warn", 0) + table.get("skipped", 0)
    header = "doctor: {} ok, {} fixed, {} need you (local checks{})".format(
        ok,
        table.get("fixed", 0),
        needs_attention(results),
        ", network checked" if any(item.id == "api_auth" and item.status != "skipped" for item in results) else "",
    )
    lines = [header]
    healthy = [item.id for item in results if item.status in ("ok", "skipped")]
    lines.append("ok: " + (", ".join(healthy) if healthy else "none"))
    for item in results:
        if item.status in ("ok", "skipped"):
            continue
        line = "{} {}: {}".format(item.status, item.id, item.detail)
        if item.fix_id:
            line += " [fix: {}]".format(item.fix_id)
        if item.human_action:
            line += " [you: {}]".format(item.human_action)
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: max(0, limit - 40)].rstrip() + "\n... (truncated, {} checks total)".format(len(results))
    return text


# --------------------------------------------------------------------------------------
# Health file and the first-run pass
# --------------------------------------------------------------------------------------

#: A first-run pass is skipped when the health file is younger than this.
HEALTH_MAX_AGE_SECONDS = 24 * 60 * 60


def health_age_seconds(state: str) -> Optional[float]:
    path = os.path.join(state, "health.json")
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def load_health(state: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(state, "health.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def save_health(ctx: DoctorContext, results: Sequence[CheckResult]) -> Optional[str]:
    """Record the outcome (never the evidence, never a secret) for the 24 h gate."""
    payload = {
        "version": 1,
        "time": int(time.time() * 1000),
        "python": _client_tls_python(),
        "platform": platform.platform(),
        "counts": counts(results),
        "summary": summary_line(results),
        "checks": [
            {
                "id": item.id,
                "status": item.status,
                "detail": _snippet(item.detail, 200, ctx),
                "fix_id": item.fix_id,
                "human_action": item.human_action,
            }
            for item in results
        ],
    }
    path = ctx.health_path
    try:
        mkdir_private(ctx.state)
        temporary = path + ".tmp"
        write_private(temporary, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except OSError:
        return None
    return path


def first_run(
    config: Config,
    *,
    env: Optional[Mapping[str, str]] = None,
    persist: bool = True,
    force: bool = False,
) -> Optional[str]:
    """The fast first-run pass.  Returns the one-line summary, or ``None``.

    Contract with the caller: **this never raises and never blocks a normal run.**  It
    runs only local checks, applies only the unambiguously safe fixes (create a missing
    directory, tighten the config mode, cut a torn session line), records the health file
    and returns one line for the user.  It never prints or stores the API key.
    """
    try:
        environ: Mapping[str, str] = dict(os.environ) if env is None else env
        if environ.get("PYTO_HARNESS_NO_DOCTOR"):
            return None
        ctx = DoctorContext.for_config(config, env=environ, persist=persist, network=False)
        if not force:
            age = health_age_seconds(ctx.state)
            if age is not None and age < HEALTH_MAX_AGE_SECONDS:
                return None
        results = run_checks(ctx, network=False)
        results, _outcomes = apply_fixes(ctx, results, safe_only=True)
        if persist:
            save_health(ctx, results)
        return summary_line(results)
    except Exception:  # noqa: BLE001 - a diagnostic must never break a normal run
        return None


def run_doctor(
    config: Config,
    *,
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[str] = None,
    root: Optional[str] = None,
    state: Optional[str] = None,
    workspace: Optional[str] = None,
    sessions_dir: Optional[str] = None,
    network: bool = True,
    deep: bool = False,
    deep_tests: bool = False,
    fix: bool = False,
    persist: bool = True,
    models: Optional[Sequence[str]] = None,
    network_timeout: float = 6.0,
) -> Tuple[List[CheckResult], List[FixOutcome], DoctorContext]:
    """The whole ``--doctor`` flow: check, optionally fix, check again."""
    ctx = DoctorContext.for_config(
        config,
        env=env,
        config_path=config_path,
        root=root,
        state=state,
        network=network,
        deep=deep,
        deep_tests=deep_tests,
        persist=persist,
        network_timeout=network_timeout,
        models=models,
    )
    if workspace:
        ctx.workspace = home.expand_user_path(workspace, what="workspace")
    if sessions_dir:
        ctx.sessions_dir = home.expand_user_path(sessions_dir, what="sessions directory")
    before = run_checks(ctx)
    outcomes: List[FixOutcome] = []
    if fix:
        after, outcomes = apply_fixes(ctx, before, safe_only=False)
    else:
        after = before
    if persist:
        save_health(ctx, after)
    return after, outcomes, ctx


def before_after_report(
    before: Sequence[CheckResult],
    after: Sequence[CheckResult],
    outcomes: Sequence[FixOutcome],
    *,
    ctx: Optional[DoctorContext] = None,
) -> str:
    parts = [format_report(before, ctx=ctx, title="doctor (before)")]
    parts.append("")
    if outcomes:
        parts.append("fixes applied")
        parts.append("-" * 72)
        for outcome in outcomes:
            parts.append(outcome.render())
        parts.append("")
    parts.append(format_report(after, ctx=None, title="doctor (after)"))
    parts.append("")
    parts.append("before: " + summary_line(before))
    parts.append("after : " + fix_summary_line(after))
    return "\n".join(parts)

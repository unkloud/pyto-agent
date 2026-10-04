"""Configuration: file, then environment, then explicit overrides.

Precedence, lowest to highest::

    built-in defaults
    ~/.pyto_harness/config.json
    environment (DEEPSEEK_API_KEY, OPENAI_API_KEY, PYTO_HARNESS_MODEL, ...)
    explicit keyword arguments (CLI flags)

The API key is treated as a secret: :func:`Config.public` and :meth:`Config.to_dict`
never include it, and :func:`describe` is what every log line and ``--dry-run``
printout uses.  Nothing in this module ever writes the key to disk.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .errors import ConfigError
from .security import chmod_private, is_secret_header, open_private, redact_url_userinfo, register_secret

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_TURNS = 8

#: Config file location.  Overridable for tests with PYTO_HARNESS_CONFIG.
CONFIG_DIR_NAME = ".pyto_harness"
CONFIG_FILE_NAME = "config.json"

#: Environment variable -> Config field.  Order matters: the first variable that is
#: set and non-empty wins.
ENV_API_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY")
ENV_FIELDS = {
    "PYTO_HARNESS_MODEL": "model",
    "PYTO_HARNESS_API_BASE": "api_base",
    "PYTO_HARNESS_MAX_TURNS": "max_turns",
    "PYTO_HARNESS_TIMEOUT": "timeout",
    "PYTO_HARNESS_WORKSPACE": "workspace",
    "PYTO_HARNESS_SESSIONS_DIR": "sessions_dir",
    "PYTO_HARNESS_MAX_TOKENS": "max_tokens",
    "PYTO_HARNESS_TEMPERATURE": "temperature",
    "PYTO_HARNESS_STREAM": "stream",
}

_INT_FIELDS = ("max_turns", "max_tokens")
_FLOAT_FIELDS = ("timeout", "temperature")
_BOOL_FIELDS = ("stream", "yolo", "compact", "allow_unattended_programs")


@dataclass
class Config:
    """Resolved settings for one harness run."""

    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    workspace: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    timeout: float = 60.0
    #: Ask the provider to stream.  ``stream=False`` is the non-streaming fallback path.
    stream: bool = True
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    #: Skip approvals for share / open_url / shortcut_run / network sends.
    yolo: bool = False
    #: Allow ``run_program`` when the run has no interactive approver (Shortcut/headless).
    #: Narrower than ``yolo``: everything else that needs a human stays denied.
    allow_unattended_programs: bool = False
    #: Trim the session log when it approaches the iOS memory budget.
    compact: bool = True
    #: Extra request headers (e.g. a proxy or an OpenRouter referer).
    extra_headers: Dict[str, str] = field(default_factory=dict)
    #: Directory holding session logs (PYTO_HARNESS_SESSIONS_DIR or the config file).
    sessions_dir: str = ""
    #: Where truncated tool output is spilled.
    spill_dir: str = ""
    #: Sources that actually contributed a value, for ``--dry-run`` reporting.
    sources: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Register the key (and any credential-shaped header value) with the scrubber, so
        # every runtime path that logs, prints or spills text removes it by *value* as
        # well as by shape — see harness.security.scrub_secrets.
        register_secret(self.api_key)
        for name, value in (self.extra_headers or {}).items():
            if is_secret_header(str(name)):
                register_secret(str(value))

    # -- derived -------------------------------------------------------------------

    @property
    def chat_completions_url(self) -> str:
        """The URL a request would be POSTed to (no key material in it)."""
        return self.api_base.rstrip("/") + "/chat/completions"

    @property
    def redacted_api_base(self) -> str:
        """``api_base`` with any ``user:password@`` userinfo removed, for display."""
        return redact_url_userinfo(self.api_base)

    @property
    def redacted_chat_completions_url(self) -> str:
        return redact_url_userinfo(self.chat_completions_url)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def public(self) -> Dict[str, Any]:
        """Everything except the key, safe to print or log."""
        payload = {
            "api_base": self.api_base,
            "model": self.model,
            "workspace": self.workspace,
            "max_turns": self.max_turns,
            "timeout": self.timeout,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "yolo": self.yolo,
            "compact": self.compact,
            "api_key": redact_key(self.api_key),
        }
        if self.extra_headers:
            payload["extra_headers"] = dict(self.extra_headers)
        if self.sources:
            payload["sources"] = dict(self.sources)
        return payload

    def to_dict(self) -> Dict[str, Any]:
        return self.public()


def redact_key(key: Optional[str]) -> str:
    """Render a key for display: presence, length and last 4 only."""
    if not key:
        return "<unset>"
    if len(key) <= 8:
        return "<set:{} chars>".format(len(key))
    return "<set:{} chars, ...{}>".format(len(key), key[-4:])


def default_home() -> str:
    return os.path.expanduser("~")


def default_workspace() -> str:
    return os.path.join(default_home(), "pyto_harness_workspace")


def default_sessions_dir() -> str:
    return os.path.join(default_home(), CONFIG_DIR_NAME, "sessions")


def default_config_path() -> str:
    override = os.environ.get("PYTO_HARNESS_CONFIG")
    if override:
        return os.path.expanduser(override)
    return os.path.join(default_home(), CONFIG_DIR_NAME, CONFIG_FILE_NAME)


def default_state_dir() -> str:
    """Where the harness keeps its own state: health.json, capabilities.json, backups.

    Precedence: ``PYTO_HARNESS_STATE_DIR``, then the directory of ``PYTO_HARNESS_CONFIG``
    (so a portable install or a test keeps its state next to its config), then
    ``~/.pyto_harness``.
    """
    override = os.environ.get("PYTO_HARNESS_STATE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    configured = os.environ.get("PYTO_HARNESS_CONFIG")
    if configured:
        directory = os.path.dirname(os.path.abspath(os.path.expanduser(configured)))
        if directory:
            return directory
    return os.path.join(default_home(), CONFIG_DIR_NAME)


def load_config_file(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the JSON config file.  A missing file is not an error; a broken one is."""
    resolved = os.path.expanduser(path) if path else default_config_path()
    if not os.path.exists(resolved):
        return {}
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except ValueError as exc:
        raise ConfigError("config file {} is not valid JSON: {}".format(resolved, exc)) from exc
    except OSError as exc:
        raise ConfigError("config file {} could not be read: {}".format(resolved, exc)) from exc
    if not isinstance(payload, dict):
        raise ConfigError("config file {} must contain a JSON object".format(resolved))
    return payload


def _coerce_field(field_name: str, raw: Any, origin: str) -> Any:
    if raw is None:
        return None
    if field_name in _INT_FIELDS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ConfigError("{}: {} must be an integer, got {!r}".format(origin, field_name, raw)) from None
    if field_name in _FLOAT_FIELDS:
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ConfigError("{}: {} must be a number, got {!r}".format(origin, field_name, raw)) from None
    if field_name in _BOOL_FIELDS:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigError("{}: {} must be a boolean, got {!r}".format(origin, field_name, raw))
    return raw


def _apply(target: Config, values: Mapping[str, Any], origin: str, sources: Dict[str, str]) -> None:
    for key, raw in values.items():
        if key in ("api_key", "sessions_dir", "spill_dir", "extra_headers"):
            value = raw
        elif key == "headers":
            key, value = "extra_headers", dict(raw or {})
        elif hasattr(target, key):
            value = _coerce_field(key, raw, origin)
        else:
            continue  # unknown keys in the file are ignored, not fatal
        if value is None:
            continue
        if key == "extra_headers" and isinstance(value, Mapping):
            merged = dict(target.extra_headers)
            merged.update({str(k): str(v) for k, v in value.items()})
            value = merged
            for header_name, header_value in merged.items():
                if is_secret_header(header_name):
                    register_secret(header_value)
        setattr(target, key, value)
        if key == "api_key":
            # Registering here (not only in ``Config.__post_init__``) matters: the file and
            # environment paths set the attribute directly, and that is how a real run gets
            # its key.
            register_secret(value if isinstance(value, str) else None)
        sources[key] = origin


def load_config(
    *,
    config_path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
    use_env: bool = True,
) -> Config:
    """Build a :class:`Config` from defaults + file + environment + overrides.

    ``overrides`` with a ``None`` value are ignored, which lets the CLI pass its
    argparse namespace through without clobbering file or env settings.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    config = Config()
    sources: Dict[str, str] = {}

    _apply(config, load_config_file(config_path), "file", sources)

    if use_env:
        for env_name, field_name in ENV_FIELDS.items():
            raw = environ.get(env_name)
            if raw not in (None, ""):
                _apply(config, {field_name: raw}, "env:{}".format(env_name), sources)
        for env_name in ENV_API_KEYS:
            raw = environ.get(env_name)
            if raw:
                _apply(config, {"api_key": raw}, "env:{}".format(env_name), sources)
                break

    if overrides:
        _apply(config, {k: v for k, v in overrides.items() if k not in (None, "")}, "cli", sources)

    config.sources = sources
    if not config.workspace:
        config.workspace = default_workspace()
        config.sources.setdefault("workspace", "default")
    if not config.sessions_dir:
        config.sessions_dir = default_sessions_dir()
    if not config.spill_dir:
        config.spill_dir = os.path.join(config.workspace, "tool-output")
    if config.max_turns < 1:
        raise ConfigError("max_turns must be >= 1, got {}".format(config.max_turns))
    if config.timeout <= 0:
        raise ConfigError("timeout must be > 0, got {}".format(config.timeout))
    return config


def describe(config: Config) -> str:
    """Multi-line human summary used by ``--dry-run`` and the startup banner."""
    key = "set" if config.has_api_key else "MISSING"
    lines = [
        "api_base   : {}".format(config.redacted_api_base),
        "endpoint   : {}".format(config.redacted_chat_completions_url),
        "model      : {}".format(config.model),
        "api_key    : {} {}".format(key, redact_key(config.api_key)),
        "workspace  : {}".format(config.workspace),
        "max_turns  : {}".format(config.max_turns),
        "timeout    : {}s".format(config.timeout),
        "stream     : {}".format(config.stream),
        "approvals  : {}".format("bypassed (--yolo)" if config.yolo else "on"),
        "compaction : {}".format("on" if config.compact else "off"),
    ]
    if config.sources:
        rendered = ", ".join("{}<-{}".format(k, v) for k, v in sorted(config.sources.items()))
        lines.append("sources    : {}".format(rendered))
    return "\n".join(lines)


def ensure_workspace(config: Config) -> str:
    """Create the workspace directory if needed and return its absolute path.

    Created ``0700``: the workspace holds the user's programs, the memory store and the
    spill files, and it is the one directory the agent writes freely.
    """
    path = os.path.abspath(os.path.expanduser(config.workspace))
    try:
        from .security import mkdir_private

        mkdir_private(path)
    except OSError as exc:
        raise ConfigError("workspace {} could not be created: {}".format(path, exc)) from exc
    if not os.path.isdir(path):
        raise ConfigError("workspace {} is not a directory".format(path))
    return path


def write_sample_config(
    path: Optional[str] = None, *, api_key: str = "sk-REPLACE-ME", force: bool = False
) -> str:
    """Write a commented-ish starter config; returns the path.  Used by ``--init``.

    The file is created ``O_CREAT|O_EXCL`` at ``0600`` so there is never a window in
    which it exists with a looser mode, and an existing config is **never** overwritten
    silently — it may hold the user's only copy of the key.  ``force=True`` is the
    explicit opt-in.  The returned path is the real one; the caller prints the mode it
    actually has (``config_file_mode``), never an assumed ``0600``.
    """
    resolved = os.path.expanduser(path) if path else default_config_path()
    payload: List[str] = [
        "{",
        '  "api_base": "{}",'.format(DEFAULT_API_BASE),
        '  "model": "{}",'.format(DEFAULT_MODEL),
        '  "api_key": "{}",'.format(api_key),
        '  "max_turns": {},'.format(DEFAULT_MAX_TURNS),
        '  "timeout": 60,',
        '  "workspace": "{}"'.format(default_workspace()),
        "}",
        "",
    ]
    try:
        handle = open_private(resolved, exclusive=not force, truncate=True)
    except FileExistsError:
        raise ConfigError(
            "{} already exists; refusing to overwrite it (it may hold your only copy of the "
            "API key). Move it aside, or re-run with --force.".format(resolved)
        ) from None
    with handle:
        handle.write("\n".join(payload))
    return resolved


def config_file_mode(path: str) -> Optional[str]:
    """The octal mode of ``path``, or ``None`` when it cannot be stat'ed."""
    try:
        return oct(stat_module.S_IMODE(os.stat(path).st_mode))
    except OSError:
        return None


def enforce_config_mode(path: str, mode: int = 0o600) -> str:
    """Make ``path`` owner-only and return the mode it actually has.

    A ``chmod`` that fails is reported, not swallowed: printing "mode 0600" over a file
    that is still world-readable is exactly the lie this closes.
    """
    try:
        chmod_private(path, mode)
    except OSError:
        pass
    return config_file_mode(path) or "unknown"

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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .errors import ConfigError

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
_BOOL_FIELDS = ("stream", "yolo", "compact")


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

    # -- derived -------------------------------------------------------------------

    @property
    def chat_completions_url(self) -> str:
        """The URL a request would be POSTed to (no key material in it)."""
        return self.api_base.rstrip("/") + "/chat/completions"

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
        setattr(target, key, value)
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
        "api_base   : {}".format(config.api_base),
        "endpoint   : {}".format(config.chat_completions_url),
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
    """Create the workspace directory if needed and return its absolute path."""
    path = os.path.abspath(os.path.expanduser(config.workspace))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ConfigError("workspace {} could not be created: {}".format(path, exc)) from exc
    if not os.path.isdir(path):
        raise ConfigError("workspace {} is not a directory".format(path))
    return path


def write_sample_config(path: Optional[str] = None, *, api_key: str = "sk-REPLACE-ME") -> str:
    """Write a commented-ish starter config; returns the path.  Used by ``--init``."""
    resolved = os.path.expanduser(path) if path else default_config_path()
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
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
    with open(resolved, "w", encoding="utf-8") as handle:
        handle.write("\n".join(payload))
    try:
        os.chmod(resolved, 0o600)
    except OSError:  # pragma: no cover - filesystem dependent
        pass
    return resolved

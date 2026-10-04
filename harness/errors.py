"""Error taxonomy.

Every failure the harness can report is a subclass of :class:`HarnessError` and
carries a short machine code plus a ``retryable`` flag.  The flag is what the
retry policy in :mod:`harness.llm` keys off, so "should this request be retried"
is decided by the code that raised the error rather than by a string match at the
call site.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class HarnessError(Exception):
    """Base class for every harness failure."""

    code = "HARNESS_ERROR"
    retryable = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(HarnessError):
    """Invalid or missing configuration."""

    code = "CONFIG_ERROR"


class TransportError(HarnessError):
    """The network conversation failed (connection, TLS, timeout, reset)."""

    code = "TRANSPORT_ERROR"
    retryable = True


class RateLimitedError(TransportError):
    """HTTP 429."""

    code = "RATE_LIMITED"
    retryable = True

    def __init__(self, message: str, retry_after: Optional[float] = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class ServerError(TransportError):
    """HTTP 5xx from the provider."""

    code = "SERVER_ERROR"
    retryable = True


class AuthError(HarnessError):
    """HTTP 401/403 - a bad or missing API key is never worth retrying."""

    code = "AUTH_ERROR"


class BadRequestError(HarnessError):
    """HTTP 4xx that is not 401/403/429 - the request itself is wrong."""

    code = "BAD_REQUEST"


class MalformedResponseError(HarnessError):
    """The provider replied with something that is not a valid chat completion."""

    code = "MALFORMED_RESPONSE"


class EmptyResponseError(MalformedResponseError):
    """The stream ended with neither content nor a finish reason."""

    code = "EMPTY_RESPONSE"


class CancelledError(HarnessError):
    """The caller set the stop event."""

    code = "CANCELLED"


class ValidationError(HarnessError):
    """Tool arguments did not satisfy the tool's schema."""

    code = "VALIDATION_ERROR"


class ToolError(HarnessError):
    """A tool handler failed in a way it wants the model to see verbatim."""

    code = "TOOL_ERROR"


class ToolTimeoutError(ToolError):
    """A tool handler exceeded its per-tool timeout."""

    code = "TOOL_TIMEOUT"


class ApprovalDenied(ToolError):
    """Policy refused the call."""

    code = "APPROVAL_DENIED"


class SessionFormatError(HarnessError):
    """The session log is not readable by this build."""

    code = "SESSION_FORMAT_ERROR"


class UnsupportedCapability(HarnessError):
    """An iOS capability is not available on this interpreter/platform."""

    code = "UNSUPPORTED_CAPABILITY"


def error_for_status(status: int, body: str, headers: Any = None) -> HarnessError:
    """Map an HTTP status onto the taxonomy.

    ``body`` is included verbatim (truncated) because provider error bodies carry the
    actionable part ("model not found", "insufficient balance"); the API key is never
    in a response body, so this is safe to surface to the model and the log.
    """
    snippet = (body or "").strip()[:600]
    detail = f" (body: {snippet})" if snippet else ""
    if status == 429:
        retry_after: Optional[float] = None
        raw = None
        if headers is not None:
            try:
                raw = headers.get("retry-after")
            except Exception:  # pragma: no cover - defensive
                raw = None
        if raw:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None
        return RateLimitedError(f"provider rate limited the request (HTTP 429){detail}", retry_after=retry_after)
    if status in (401, 403):
        return AuthError(f"provider rejected the credentials (HTTP {status}){detail}")
    if status >= 500:
        return ServerError(f"provider failed with HTTP {status}{detail}")
    return BadRequestError(f"provider rejected the request with HTTP {status}{detail}")

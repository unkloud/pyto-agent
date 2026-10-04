"""OpenAI-compatible chat-completions client: stdlib only.

Everything here is ``http.client``, ``ssl``, ``socket``, ``json`` and threads.  Design
decisions that matter on a phone:

* **``http.client``, not ``urllib.request``.**  The SSE reader needs *incremental*
  reads so tokens surface as they arrive; ``urllib`` hands back a buffered file object
  whose chunking we do not control.  ``http.client`` exposes the raw response.
* **Blocking core, thin wrappers.**  :meth:`LLMClient.stream_sync` is the real
  implementation; :meth:`LLMClient.stream` is a coroutine over it and
  :meth:`LLMClient.stream_events` is a synchronous generator, so the same client works
  from the agent loop, from a UI thread and from a plain REPL.
* **Cancellation is cooperative.**  ``stop: threading.Event`` is checked between socket
  reads, and :meth:`cancel` also closes the connection, because a thread parked in
  ``recv`` cannot be interrupted any other way.  This is iOS-relevant: the user
  backgrounds the app and the turn has to die promptly.
* **Retry only before the first token.**  Retrying after content has been surfaced would
  duplicate output the user already saw, so :class:`AssistantStream` tracks ``emitted``
  and the retry loop refuses to restart once it is set.
* **Tool-call arguments arrive as fragments.**  Providers emit ``function.arguments``
  as a series of partial JSON strings keyed by ``index``; they are concatenated and
  parsed once at the end, and a malformed tail becomes a model-visible tool error
  instead of an exception that kills the turn.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import queue
import random
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .errors import (
    CancelledError,
    HarnessError,
    MalformedResponseError,
    EmptyResponseError,
    RateLimitedError,
    TransportError,
    error_for_status,
)
from .security import redact_headers as _redact_headers_pattern
from .security import redact_url_userinfo

# --------------------------------------------------------------------------------------
# SSE framing
# --------------------------------------------------------------------------------------


@dataclass
class SseEvent:
    """One decoded server-sent event."""

    data: str
    event: Optional[str] = None
    id: Optional[str] = None


class SseParser:
    """Incremental SSE frame decoder.

    Feed arbitrary byte chunks to :meth:`feed`; complete frames come back as a list.
    Implements the parts of the WHATWG event-stream rules that providers actually use:
    LF and CRLF line endings, multiple ``data:`` lines joined with ``\\n``, ``:``
    comment lines ignored, a blank line terminating the frame, and an unterminated tail
    that is *not* emitted as an event.

    A trailing lone ``\\r`` is held back until the next chunk because it may be the
    first half of a CRLF pair split across a TCP segment.
    """

    __slots__ = ("_buffer", "_start", "_data", "_event", "_id", "_saw_data", "comments")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._start = 0
        self._data: List[str] = []
        self._event: Optional[str] = None
        self._id: Optional[str] = None
        self._saw_data = False
        self.comments = 0

    def feed(self, chunk: bytes) -> List[SseEvent]:
        self._buffer.extend(chunk)
        out: List[SseEvent] = []
        buf = self._buffer
        i = self._start
        length = len(buf)
        while i < length:
            byte = buf[i]
            if byte == 0x0A:  # LF
                event = self._line(bytes(buf[self._start : i]).decode("utf-8", "replace"))
                if event is not None:
                    out.append(event)
                i += 1
                self._start = i
                continue
            if byte == 0x0D:  # CR, possibly half of CRLF
                if i + 1 >= length:
                    break  # cannot decide yet; keep the CR buffered
                event = self._line(bytes(buf[self._start : i]).decode("utf-8", "replace"))
                if event is not None:
                    out.append(event)
                i += 2 if buf[i + 1] == 0x0A else 1
                self._start = i
                continue
            i += 1
        if self._start:
            del buf[: self._start]
            self._start = 0
        return out

    def _line(self, line: str) -> Optional[SseEvent]:
        if line == "":
            if not self._saw_data and self._event is None:
                return None
            event = SseEvent(data="\n".join(self._data), event=self._event, id=self._id)
            self._data = []
            self._event = None
            self._id = None
            self._saw_data = False
            return event
        if line.startswith(":"):
            self.comments += 1
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
            self._saw_data = True
        elif name == "event":
            self._event = value
        elif name == "id":
            self._id = value
        # `retry:` is ignored: backoff is owned by RetryPolicy, not the server.
        return None

    def finish(self) -> None:
        """Drop an unterminated tail; a truncated stream is not an event."""
        self._buffer.clear()
        self._data = []
        self._event = None
        self._id = None
        self._saw_data = False


# --------------------------------------------------------------------------------------
# Streamed message assembly
# --------------------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One assembled tool call from an assistant message."""

    id: str = ""
    name: str = ""
    arguments_raw: str = ""

    def arguments(self) -> Dict[str, Any]:
        """Parse the accumulated fragments.

        Returns ``{}`` for an empty string.  Raises
        :class:`~harness.errors.MalformedResponseError` for invalid JSON — the caller
        turns that into a tool result, so one bad call cannot abort the turn.
        """
        raw = self.arguments_raw.strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise MalformedResponseError(
                "tool call {!r} arguments are not valid JSON: {}: {}".format(self.name, exc, raw[:400])
            ) from exc
        if not isinstance(value, dict):
            raise MalformedResponseError("tool call {!r} arguments must be a JSON object".format(self.name))
        return value

    def to_wire(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_raw},
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    @classmethod
    def from_wire(cls, raw: Optional[Mapping[str, Any]]) -> "Usage":
        if not raw:
            return cls()
        details = raw.get("prompt_tokens_details") or {}
        if not isinstance(details, Mapping):
            details = {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
        )

    def to_wire(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass
class AssistantStream:
    """Accumulator for one streamed (or non-streamed) assistant message."""

    content: List[str] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    model: Optional[str] = None
    response_id: Optional[str] = None
    chunk_count: int = 0
    #: True once any content / reasoning / tool-call material has been observed.
    emitted: bool = False
    #: ``index`` -> position in :attr:`tool_calls`; providers may skip indexes.
    _by_index: Dict[int, int] = field(default_factory=dict, repr=False)
    #: ``(kind, text)`` deltas produced by the most recent :meth:`apply_chunk`.
    pending_deltas: List[Tuple[str, str]] = field(default_factory=list, repr=False)

    # -- chunk application ---------------------------------------------------------

    def apply_chunk(self, chunk: Mapping[str, Any]) -> None:
        """Fold one ``chat.completion.chunk`` object into the accumulator."""
        self.chunk_count += 1
        self.pending_deltas = []
        if self.response_id is None:
            self.response_id = chunk.get("id")
        if chunk.get("model"):
            self.model = chunk["model"]
        if chunk.get("usage"):
            self.usage = Usage.from_wire(chunk.get("usage"))
        for choice in chunk.get("choices") or ():
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                # Non-streaming responses carry `message` instead of `delta`.
                delta = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            text = delta.get("content")
            if text:
                self.content.append(text)
                self.pending_deltas.append(("content", text))
                self.emitted = True
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                self.reasoning.append(reasoning)
                self.pending_deltas.append(("reasoning", reasoning))
                self.emitted = True
            for call in delta.get("tool_calls") or ():
                if isinstance(call, Mapping):
                    self._apply_tool_call_delta(call)

    def _apply_tool_call_delta(self, delta: Mapping[str, Any]) -> None:
        index = delta.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = len(self.tool_calls)
        slot = self._by_index.get(index)
        if slot is None:
            slot = len(self.tool_calls)
            self._by_index[index] = slot
            self.tool_calls.append(ToolCall())
        call = self.tool_calls[slot]
        self.emitted = True
        if delta.get("id"):
            call.id = str(delta["id"])
        function = delta.get("function") or {}
        if not isinstance(function, Mapping):
            function = {}
        if function.get("name"):
            call.name = str(function["name"])
        arguments = function.get("arguments")
        if arguments:
            call.arguments_raw += str(arguments)

    # -- results -------------------------------------------------------------------

    @property
    def text(self) -> str:
        return "".join(self.content)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning)

    def is_empty(self) -> bool:
        return not self.text and not self.reasoning_text and not self.tool_calls

    def to_message(self) -> Dict[str, Any]:
        """Build the assistant message to append to the conversation."""
        for position, call in enumerate(self.tool_calls):
            if not call.id:
                call.id = "call_{}_{}".format(position, int(time.time() * 1000))
            if not call.name:
                raise MalformedResponseError("tool call {} carried no function name".format(call.id))
        message: Dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning_text
        if self.tool_calls:
            message["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        return message


# --------------------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------------------


class StopToken:
    """A ``threading.Event`` that is also set when another event is set.

    ``stream_sync`` accepts a caller-supplied stop event, but a *cancel* issued on the
    client has to reach a call that was started with its own event.  Linking the two at
    the point of use is simpler — and less error-prone — than requiring every call site
    to remember to pass the client's event down.
    """

    __slots__ = ("_local", "_owner")

    def __init__(self, local: Optional[threading.Event] = None, owner: Optional[threading.Event] = None) -> None:
        self._local = local if local is not None else threading.Event()
        self._owner = owner

    def is_set(self) -> bool:
        if self._local.is_set():
            return True
        return self._owner is not None and self._owner.is_set()

    def set(self) -> None:
        self._local.set()

    def clear(self) -> None:
        self._local.clear()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.is_set():
            return True
        if self._owner is None:
            return self._local.wait(timeout)
        # Poll in small slices so `cancel()` is noticed promptly even while waiting.
        remaining = timeout
        while True:
            if self._local.wait(0.05 if remaining is None else min(0.05, remaining)):
                return True
            if self.is_set():
                return True
            if remaining is not None:
                remaining -= 0.05
                if remaining <= 0:
                    return self.is_set()


def resolve_stop(stop: Optional[threading.Event], owner: Optional[threading.Event]) -> StopToken:
    """Combine a caller event with the client's own cancel event."""
    if owner is None and stop is None:
        return StopToken()
    if stop is None:
        return StopToken(owner=owner)
    if owner is None or stop is owner:
        return StopToken(local=stop)
    return StopToken(local=stop, owner=owner)


# --------------------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Bounded exponential backoff with symmetric jitter."""

    max_retries: int = 3
    initial_delay: float = 0.5
    max_delay: float = 8.0
    jitter_ratio: float = 0.1

    def delay_for(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """``attempt`` is 0 for the first retry.  Jitter avoids lockstep retries."""
        base = min(self.initial_delay * (2**attempt), self.max_delay)
        jitter = base * self.jitter_ratio
        return max(0.0, base + (rand() * 2 - 1) * jitter)

    def should_retry(self, error: BaseException, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        if isinstance(error, HarnessError):
            return error.retryable
        return isinstance(error, (OSError, http.client.HTTPException, json.JSONDecodeError))

    def delay_for_error(self, error: BaseException, attempt: int) -> float:
        """Honour a server ``Retry-After`` when it is sane, else fall back to backoff."""
        if isinstance(error, RateLimitedError) and error.retry_after is not None:
            return max(0.0, min(float(error.retry_after), self.max_delay))
        return self.delay_for(attempt)


def _sleep_with_stop(delay: float, stop: Optional[threading.Event]) -> bool:
    """Sleep, waking early if ``stop`` is set.  Returns False when cancelled."""
    if stop is None:
        time.sleep(delay)
        return True
    return not stop.wait(delay)


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------

DeltaCallback = Callable[[str, str], None]
EventCallback = Callable[[Dict[str, Any]], None]
UsageCallback = Callable[[Usage], None]


@dataclass
class LLMConfig:
    api_base: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    #: ``""`` is legitimate for a local mock or llama.cpp server; ``None`` omits the header.
    api_key: Optional[str] = None
    timeout: float = 60.0
    connect_timeout: float = 10.0
    #: Ask the provider to stream.  ``stream=False`` selects the non-streaming path.
    stream: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    include_usage: bool = True
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)


class LLMClient:
    """Chat-completions client with streaming, a non-streaming fallback and retries."""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig()
        self._stop = threading.Event()
        self._conn: Optional[http.client.HTTPConnection] = None
        self._response: Optional[http.client.HTTPResponse] = None
        self._socket: Any = None
        self._lock = threading.Lock()
        #: Populated by every request, for ``--dry-run`` and tests.
        self.last_request: Optional[Dict[str, Any]] = None

    # -- lifecycle -----------------------------------------------------------------

    def cancel(self) -> None:
        """Ask an in-flight request to stop promptly.

        Two mechanisms, because neither is sufficient alone:

        1. the ``stop`` event, checked between socket reads (cooperative);
        2. a shutdown of the socket, so a thread *parked inside* ``recv`` wakes up now.

        The second is the interesting one.  ``socket.close()`` does **not** interrupt a
        blocked ``recv`` on another thread, and after ``getresponse()`` the connection's
        ``sock`` attribute is ``None`` while ``HTTPResponse.sock`` is ``None`` too — the
        live descriptor is buried in ``response.fp.raw``.  Worse, that wrapper is marked
        closed, so ``socket.shutdown()`` on it is unreliable; the ``os.shutdown()``
        syscall on the file descriptor itself is what actually works.
        """
        self._stop.set()
        with self._lock:
            conn, response, sock = self._conn, self._response, self._socket
        if sock is None and response is not None:
            sock = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(sock, "_sock", sock)
        if sock is not None:
            fileno = None
            try:
                fileno = sock.fileno()
            except Exception:  # noqa: BLE001 - already closed
                fileno = None
            if fileno is not None and fileno >= 0:
                try:
                    os.shutdown(fileno, socket.SHUT_RDWR)
                except (OSError, AttributeError, ValueError):
                    pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass
        if conn is not None:
            _close_quietly(conn)

    def reset_cancel(self) -> None:
        self._stop = threading.Event()

    def close(self) -> None:
        self.cancel()

    # -- request construction ------------------------------------------------------

    def candidate_urls(self) -> List[str]:
        """Absolute URLs to try, in order.

        The path is appended to the configured base.  When the result is a bare origin
        with no path, ``/v1`` is also tried: that is the difference between
        ``https://api.deepseek.com`` (which serves ``/chat/completions``) and
        ``https://api.openai.com`` (which requires ``/v1/chat/completions``).  Both are
        legitimate values for ``--api-base``, so both are attempted rather than
        documented as a gotcha.
        """
        base = (self.config.api_base or "").strip()
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("api_base must look like http(s)://host[:port][/path], got {!r}".format(base))
        origin = "{}://{}".format(parsed.scheme, parsed.netloc)
        path = parsed.path.rstrip("/")
        urls = [base.rstrip("/") + "/chat/completions"]
        if not path:
            urls.append(origin + "/v1/chat/completions")
        seen: List[str] = []
        for url in urls:
            if url not in seen:
                seen.append(url)
        return seen

    def build_body(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        stream: bool = True,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": model or self.config.model,
            "messages": [dict(m) for m in messages],
            "stream": bool(stream),
        }
        if stream and self.config.include_usage:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        if self.config.max_tokens is not None:
            body["max_tokens"] = self.config.max_tokens
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        return body

    def build_headers(self, payload: bytes, *, stream: Optional[bool] = None) -> Dict[str, str]:
        streaming = self.config.stream if stream is None else bool(stream)
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if streaming else "application/json",
            "content-length": str(len(payload)),
            "user-agent": "pyto-harness/1.0 (stdlib; iOS)",
        }
        if self.config.api_key:
            headers["authorization"] = "Bearer {}".format(self.config.api_key)
        for key, value in (self.config.extra_headers or {}).items():
            headers[str(key)] = str(value)
        return headers

    def request_preview(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        stream: bool = True,
    ) -> Dict[str, Any]:
        """What *would* be sent.  Contains no key material — see ``headers_redacted``."""
        body = self.build_body(messages, tools=tools, stream=stream)
        payload = json.dumps(body).encode("utf-8")
        headers = self.build_headers(payload, stream=stream)
        return {
            # Credentials embedded in api_base userinfo (`https://user:pw@host/`) are not
            # key material but they are a password, and this dictionary is printed.
            "url": redact_url_userinfo(self.candidate_urls()[0]),
            "fallback_urls": [redact_url_userinfo(url) for url in self.candidate_urls()[1:]],
            "method": "POST",
            "headers": redact_headers(headers),
            "body": body,
            "bytes": len(payload),
        }

    # -- synchronous streaming -----------------------------------------------------

    def stream_sync(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
        stream: Optional[bool] = None,
        on_delta: Optional[DeltaCallback] = None,
        on_event: Optional[EventCallback] = None,
        on_usage: Optional[UsageCallback] = None,
        stop: Optional[threading.Event] = None,
    ) -> AssistantStream:
        """Blocking call with retries.  Returns the fully assembled assistant message."""
        use_stream = self.config.stream if stream is None else bool(stream)
        stop = resolve_stop(stop, self._stop)
        body = self.build_body(messages, tools=tools, stream=use_stream, model=model)
        policy = self.config.retry
        attempt = 0
        last_error: Optional[BaseException] = None
        while True:
            result = AssistantStream()
            try:
                self._attempt(body, use_stream, result, on_delta, on_event, on_usage, stop, attempt)
                return result
            except CancelledError:
                raise
            except HarnessError as exc:
                if result.emitted or not policy.should_retry(exc, attempt):
                    raise
                last_error = exc
            except (OSError, http.client.HTTPException, json.JSONDecodeError, socket.timeout) as exc:
                if result.emitted or not policy.should_retry(exc, attempt):
                    raise TransportError("{}: {}".format(type(exc).__name__, exc)) from exc
                last_error = exc
            delay = policy.delay_for_error(last_error, attempt)
            if not _sleep_with_stop(delay, stop):
                raise CancelledError("cancelled during retry backoff")
            attempt += 1

    def _attempt(
        self,
        body: Mapping[str, Any],
        use_stream: bool,
        result: AssistantStream,
        on_delta: Optional[DeltaCallback],
        on_event: Optional[EventCallback],
        on_usage: Optional[UsageCallback],
        stop: Optional[StopToken],
        attempt: int,
    ) -> None:
        """One HTTP round trip, streaming or not, across the candidate URL list."""
        payload = json.dumps(body).encode("utf-8")
        headers = self.build_headers(payload)
        urls = self.candidate_urls()
        errors: List[str] = []
        for position, url in enumerate(urls):
            response, conn = self._send(url, payload, headers, stop)
            try:
                if response.status != 200:
                    text = _read_error_body(response)
                    retryable = response.status == 429 or response.status >= 500
                    if retryable:
                        raise error_for_status(response.status, text, response.headers)
                    # Only a 404 on the first candidate is worth trying the next one.
                    if response.status == 404 and position + 1 < len(urls):
                        errors.append("{} -> HTTP 404".format(url))
                        continue
                    raise error_for_status(response.status, text, response.headers)
                self.last_request = {
                    "url": url,
                    "headers": redact_headers(headers),
                    "body": dict(body),
                    "attempt": attempt + 1,
                }
                if use_stream:
                    self._consume_sse(response, result, on_delta, on_event, on_usage, stop)
                else:
                    self._consume_json(response, result, on_delta, on_event, on_usage)
                return
            finally:
                self._release(response, conn)
        raise TransportError(
            "no usable endpoint; tried: {}".format("; ".join(errors) or ", ".join(redact_url_userinfo(u) for u in urls))
        )

    def _send(
        self,
        url: str,
        payload: bytes,
        headers: Mapping[str, str],
        stop: Optional[StopToken],
    ) -> Tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        if stop is not None and stop.is_set():
            raise CancelledError("cancelled before request")
        parsed = urllib.parse.urlsplit(url)
        secure = parsed.scheme == "https"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        port = parsed.port or (443 if secure else 80)
        if secure:
            context = ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname, port, timeout=self.config.timeout, context=context
            )
        else:
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=self.config.timeout)
        with self._lock:
            self._conn = conn
        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(self.config.timeout)
            conn.request("POST", path, body=payload, headers=dict(headers))
            response = conn.getresponse()
            # Capture the live descriptor now: it is the only handle that can interrupt
            # a blocked read later, and it becomes hard to reach once the response has
            # been consumed.
            live = getattr(getattr(response, "fp", None), "raw", None)
            with self._lock:
                self._response = response
                self._socket = getattr(live, "_sock", live)
            return response, conn
        except CancelledError:
            _close_quietly(conn)
            raise
        except (OSError, http.client.HTTPException) as exc:
            _close_quietly(conn)
            with self._lock:
                self._conn = None
                self._response = None
                self._socket = None
            raise TransportError("{}: {}".format(type(exc).__name__, exc)) from exc

    def _release(
        self, response: http.client.HTTPResponse, conn: http.client.HTTPConnection
    ) -> None:
        with self._lock:
            self._conn = None
            self._response = None
            self._socket = None
        try:
            response.close()
        except Exception:  # pragma: no cover
            pass
        _close_quietly(conn)

    def _consume_sse(
        self,
        response: http.client.HTTPResponse,
        result: AssistantStream,
        on_delta: Optional[DeltaCallback],
        on_event: Optional[EventCallback],
        on_usage: Optional[UsageCallback],
        stop: Optional[StopToken],
    ) -> None:
        parser = SseParser()
        frames = 0
        finished = False
        while not finished:
            if stop is not None and stop.is_set():
                raise CancelledError("cancelled while streaming")
            try:
                chunk = response.read(8192)
            except socket.timeout as exc:
                raise TransportError("read timed out after {}s: {}".format(self.config.timeout, exc)) from exc
            except (OSError, http.client.HTTPException) as exc:
                if stop is not None and stop.is_set():
                    raise CancelledError("cancelled while streaming") from exc
                raise TransportError("stream read failed: {}".format(exc)) from exc
            if not chunk:
                break
            for frame in parser.feed(chunk):
                if frame.data == "[DONE]":
                    finished = True
                    break
                if not frame.data:
                    continue
                frames += 1
                try:
                    parsed = json.loads(frame.data)
                except ValueError as exc:
                    raise MalformedResponseError(
                        "SSE data is not JSON: {}: {}".format(exc, frame.data[:200])
                    ) from exc
                if not isinstance(parsed, dict):
                    raise MalformedResponseError("SSE data must be a JSON object")
                if parsed.get("error"):
                    detail = parsed["error"]
                    message = detail.get("message") if isinstance(detail, Mapping) else str(detail)
                    raise MalformedResponseError("provider error frame: {}".format(message))
                if on_event is not None:
                    on_event(parsed)
                result.apply_chunk(parsed)
                if on_delta is not None:
                    for kind, text in result.pending_deltas:
                        on_delta(kind, text)
        parser.finish()
        if stop is not None and stop.is_set():
            # Cancellation shut the socket down, so the read above returned EOF instead of
            # raising.  Surfacing the truncated stream as a success would let the caller
            # treat half an answer as a whole one.
            raise CancelledError("cancelled while streaming")
        if on_usage is not None and result.usage.total_tokens:
            on_usage(result.usage)
        if frames == 0:
            raise MalformedResponseError("response body contained no SSE data frames")
        if result.is_empty() and result.finish_reason is None:
            raise EmptyResponseError("provider stream ended with no content and no finish_reason")

    def _consume_json(
        self,
        response: http.client.HTTPResponse,
        result: AssistantStream,
        on_delta: Optional[DeltaCallback],
        on_event: Optional[EventCallback],
        on_usage: Optional[UsageCallback],
    ) -> None:
        raw = b""
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 8 * 1024 * 1024:
                raise MalformedResponseError("non-streaming response exceeded 8 MiB")
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise MalformedResponseError("response body is not JSON: {}".format(exc)) from exc
        if not isinstance(payload, Mapping):
            raise MalformedResponseError("response body must be a JSON object")
        if payload.get("error"):
            detail = payload["error"]
            message = detail.get("message") if isinstance(detail, Mapping) else str(detail)
            raise MalformedResponseError("provider error: {}".format(message))
        if on_event is not None:
            on_event(dict(payload))
        result.apply_chunk(payload)
        if on_delta is not None:
            for kind, text in result.pending_deltas:
                on_delta(kind, text)
        if on_usage is not None and result.usage.total_tokens:
            on_usage(result.usage)
        if result.is_empty() and result.finish_reason is None:
            raise EmptyResponseError("provider returned no content and no finish_reason")

    # -- async / generator wrappers ------------------------------------------------

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
        stream: Optional[bool] = None,
        on_delta: Optional[DeltaCallback] = None,
        on_usage: Optional[UsageCallback] = None,
        stop: Optional[threading.Event] = None,
    ) -> AssistantStream:
        """Awaitable variant.  The blocking work runs in a worker thread.

        ``on_delta`` is invoked from that worker thread, which is why the loop marshals
        it through an ``asyncio.Queue`` rather than touching loop state directly.
        """
        resolved = resolve_stop(stop, self._stop)
        return await asyncio.to_thread(
            self.stream_sync,
            messages,
            tools=tools,
            model=model,
            stream=stream,
            on_delta=on_delta,
            on_usage=on_usage,
            stop=resolved,
        )

    def stream_events(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Synchronous generator of ``{"kind": ...}`` events, for a REPL or UI thread."""
        pending: "queue.Queue[Any]" = queue.Queue()
        # A local token linked to the client's, so closing the generator early cancels
        # this request only and does not poison every later call on the same client.
        stop = StopToken(owner=self._stop)
        done = object()

        def worker() -> None:
            try:
                self.stream_sync(
                    messages,
                    tools=tools,
                    model=model,
                    on_event=lambda event: pending.put({"kind": "event", "event": event}),
                    on_delta=lambda kind, text: pending.put({"kind": "delta", "delta_kind": kind, "text": text}),
                    stop=stop,
                )
            except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
                pending.put({"kind": "error", "error": exc})
            finally:
                pending.put(done)

        thread = threading.Thread(target=worker, name="pyto-llm", daemon=True)
        thread.start()
        try:
            while True:
                item = pending.get()
                if item is done:
                    return
                yield item
        finally:
            stop.set()


def _read_error_body(response: http.client.HTTPResponse) -> str:
    try:
        return response.read(8192).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - best effort
        return ""


def redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Header copy safe to print or log.

    Redaction is by *pattern*, not by a fixed list of four names: a provider-specific
    header such as ``x-goog-api-key`` was printed verbatim by ``--dry-run`` while
    ``api-key`` was redacted, and the dry-run dump is exactly what a user pastes into a
    bug report.
    """
    return _redact_headers_pattern(headers)


def _close_quietly(conn: http.client.HTTPConnection) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - closing a dead socket
        pass

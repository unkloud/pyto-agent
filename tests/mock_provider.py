"""A scriptable OpenAI-compatible mock server, stdlib only.

Each instance binds an ephemeral port on 127.0.0.1 and serves
``POST /chat/completions`` from a *script*: a list of response specs consumed one per
request, in order.  That makes multi-turn tests (call a tool, then answer) a two-element
list rather than a stateful fake.

Specs are either a dict or a callable ``(request_body) -> dict``::

    {"sse": [{"content": "hi"}]}                       # streamed text
    {"sse": [{"tool": ("write_program", {...})}]}     # streamed tool call, split across chunks
    {"status": 429, "headers": {"retry-after": "0"}}  # an error
    {"json": {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}}
    {"delay": 0.4}                                    # slow first byte, for cancel tests

The last spec is reused once the script runs out, so "answer the same way forever" needs
no bookkeeping.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

TOOL_CALL_FRAGMENTS = 4  # split each tool call's arguments into this many SSE chunks


def text_response(content: str, *, model: str = "mock-model") -> Dict[str, Any]:
    return {"sse": [{"content": content}], "model": model}


def tool_response(*calls: Tuple[str, Dict[str, Any]], model: str = "mock-model") -> Dict[str, Any]:
    """``tool_response(("write_program", {...}), ("run_program", {...}))``"""
    return {"sse": [{"tool": call} for call in calls], "model": model}


def error_response(status: int, message: str = "boom", **headers: str) -> Dict[str, Any]:
    return {
        "status": status,
        "headers": dict(headers),
        "json": {"error": {"message": message, "type": "mock_error"}},
    }


def _default_body(model: str, chunk: Dict[str, Any], finish: Optional[str]) -> Dict[str, Any]:
    choice: Dict[str, Any] = {"index": 0, "delta": {}}
    if "content" in chunk:
        choice["delta"]["content"] = chunk["content"]
    if "reasoning" in chunk:
        choice["delta"]["reasoning_content"] = chunk["reasoning"]
    if "tool" in chunk:
        name, arguments = chunk["tool"]
        choice["delta"]["tool_calls"] = [
            {
                "index": chunk.get("index", 0),
                "id": chunk.get("id") or "call_{}".format(name),
                "type": "function",
                "function": {"name": name, "arguments": ""},
            }
        ]
    if finish:
        choice["finish_reason"] = finish
    return {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model, "choices": [choice]}


def _fragment_tool_arguments(payload: Dict[str, Any], fragments: int = TOOL_CALL_FRAGMENTS) -> List[str]:
    rendered = json.dumps(payload)
    if fragments <= 1 or len(rendered) <= fragments:
        return [rendered]
    size = max(1, len(rendered) // fragments)
    return [rendered[i : i + size] for i in range(0, len(rendered), size)]


class _QuietServer(ThreadingHTTPServer):
    """A server that does not print the two errors a cancelled client always causes."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _invalid_tool_sequence(messages: Any) -> Optional[str]:
    """Reject histories whose assistant tool calls lack exactly one result each."""
    pending = set()
    for index, message in enumerate(messages if isinstance(messages, list) else ()):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if pending and role != "tool":
            return "assistant tool calls are missing results before message {}".format(index)
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                call_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id:
                    return "assistant tool call is missing an id"
                if call_id in pending:
                    return "assistant tool call id is duplicated: {}".format(call_id)
                pending.add(call_id)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return "tool result has no pending assistant declaration: {}".format(call_id)
            pending.remove(call_id)
    if pending:
        return "assistant tool calls are missing results: {}".format(", ".join(sorted(pending)))
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockOpenAI/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from base
        if self.server.verbose:  # type: ignore[attr-defined]
            print("mock: " + format % args)

    def do_POST(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        server: "MockProvider" = self.server  # type: ignore[assignment]
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {"__unparsed__": raw.decode("utf-8", "replace")}
        record = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "time": time.time(),
        }
        with server.lock:
            server.requests.append(record)
            index = len(server.requests) - 1
        if server.strict_tool_protocol:  # type: ignore[attr-defined]
            invalid = _invalid_tool_sequence(body.get("messages")) if isinstance(body, dict) else None
            if invalid:
                self._send_json(400, {"error": {"message": "invalid tool-call sequence: {}".format(invalid)}})
                return
        spec = server.spec_for(index, body)
        if spec is None:
            self._send_json(500, {"error": {"message": "mock script exhausted"}})
            return
        if spec.get("delay"):
            time.sleep(float(spec["delay"]))
        if "status" in spec and int(spec["status"]) != 200:
            self._send_json(int(spec["status"]), spec.get("json") or {"error": {"message": "error"}}, spec.get("headers"))
            return
        if "json" in spec:
            self._send_json(200, spec["json"], spec.get("headers"))
            return
        self._send_sse(spec)

    def _send_json(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, spec: Dict[str, Any]) -> None:
        model = spec.get("model", "mock-model")
        chunks: List[Dict[str, Any]] = list(spec.get("sse") or [])
        # Close after [DONE]: an SSE response has no content-length, so an HTTP/1.1
        # keep-alive connection would leave the client's read() parked until its socket
        # timeout.  Real providers close or keep the socket alive with heartbeats; the
        # mock closes, which is the behaviour the client must handle anyway.
        self.close_connection = True
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        saw_tool = False
        tool_index = 0
        try:
            for chunk in chunks:
                if "tool" in chunk:
                    saw_tool = True
                    chunk = dict(chunk)
                    # Distinct providers index tool calls from 0 upwards; the assembler
                    # must key on this index, so the mock must not reuse index 0.
                    chunk.setdefault("index", tool_index)
                    chunk.setdefault("id", "call_{}_{}".format(tool_index, chunk["tool"][0]))
                    tool_index += 1
                    self._write_event(_default_body(model, chunk, None))
                    fragments = _fragment_tool_arguments(chunk["tool"][1])
                    for fragment in fragments:
                        self._write_event(
                            {
                                "id": "chatcmpl-mock",
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": chunk["index"],
                                                    "function": {"arguments": fragment},
                                                }
                                            ]
                                        },
                                    }
                                ],
                            }
                        )
                else:
                    self._write_event(_default_body(model, chunk, None))
                if spec.get("chunk_delay"):
                    time.sleep(float(spec["chunk_delay"]))
            if spec.get("raw_tail"):
                # A deliberately broken tail: garbage where a data frame should be.
                self.wfile.write(spec["raw_tail"].encode("utf-8"))
                self.wfile.flush()
                return
            if spec.get("hold_open"):
                # Stall without [DONE]: the client is left parked in read(), which is the
                # only state in which cancellation can be tested meaningfully.
                time.sleep(float(spec["hold_open"]))
                return
            finish = spec.get("finish") or ("tool_calls" if saw_tool else "stop")
            self._write_event({"id": "chatcmpl-mock", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            if spec.get("usage", True):
                self._write_event(
                    {
                        "id": "chatcmpl-mock",
                        "model": model,
                        "choices": [],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    }
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # a cancelled client is normal here
            pass

    def _write_event(self, payload: Dict[str, Any]) -> None:
        self.wfile.write("data: {}\n\n".format(json.dumps(payload)).encode("utf-8"))
        self.wfile.flush()


class MockProvider:
    """The server plus its script and request log."""

    def __init__(
        self,
        script: Optional[List[Any]] = None,
        *,
        repeat_last: bool = True,
        verbose: bool = False,
        strict_tool_protocol: bool = False,
    ) -> None:
        self.script: List[Any] = list(script or [text_response("ok")])
        self.repeat_last = repeat_last
        self.verbose = verbose
        self.strict_tool_protocol = strict_tool_protocol
        self.requests: List[Dict[str, Any]] = []
        # Reentrant: `messages_sent` and `tool_names_sent` are called in sequence by
        # tests, and a non-reentrant lock deadlocks the test runner itself.
        self.lock = threading.RLock()
        self._httpd = _QuietServer(("127.0.0.1", 0), _Handler)
        self._httpd.verbose = verbose  # type: ignore[attr-defined]
        self._httpd.strict_tool_protocol = bool(strict_tool_protocol)  # type: ignore[attr-defined]
        self._httpd.lock = self.lock  # type: ignore[attr-defined]
        self._httpd.requests = self.requests  # type: ignore[attr-defined]
        self._httpd.spec_for = self._spec_for  # type: ignore[attr-defined]
        # A short poll interval makes shutdown() return promptly: the default 0.5 s per
        # server teardown adds up across a suite that starts dozens of these.
        self._thread = threading.Thread(
            target=lambda: self._httpd.serve_forever(poll_interval=0.02), name="mock-openai", daemon=True
        )
        self._thread.start()

    # -- lifecycle -----------------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{}".format(self.port)

    @property
    def api_base(self) -> str:
        """What to pass as ``--api-base`` so the client targets ``/chat/completions``."""
        return self.base_url

    @property
    def api_base_v1(self) -> str:
        return "{}/v1".format(self.base_url)

    def url(self) -> str:
        return "{}/chat/completions".format(self.base_url)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "MockProvider":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- script --------------------------------------------------------------------

    def _spec_for(self, index: int, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.script:
            return None
        if index < len(self.script):
            spec = self.script[index]
        else:
            if not self.repeat_last:
                return None
            spec = self.script[-1]
        if callable(spec):
            spec = spec(body)
        return spec

    def push(self, spec: Any) -> None:
        with self.lock:
            self.script.append(spec)

    def request_bodies(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(record["body"]) for record in self.requests]

    def messages_sent(self, index: int = -1) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.requests[index]["body"].get("messages") or [])

    def tool_names_sent(self, index: int = -1) -> List[str]:
        with self.lock:
            tools = self.requests[index]["body"].get("tools") or []
        return [t.get("function", {}).get("name") for t in tools]

    def wait_for_requests(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.requests) >= count:
                    return True
            time.sleep(0.01)
        return False


def make_agent_script(purpose: str = "renames screenshots", program: str = "print('hi')") -> List[Any]:
    """A canonical two-turn script: write_program, then run_program + finish."""
    return [
        tool_response(("write_program", {"path": "demo.py", "source": program, "purpose": purpose})),
        tool_response(
            ("run_program", {"path_or_source": "demo.py"}),
            ("finish", {"message": "Wrote demo.py and ran it."}),
        ),
    ]

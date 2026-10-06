"""Loopback-only browser front end for the Pyto harness.

The browser is a presentation layer only. Model turns still go through ``run_turn`` and
the sealed registry policy; approvals are answered through the same ``UIApprover`` used by
the PytoUI front end. The API key and provider client never leave this Python process.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from typing import Any, Dict, Mapping, Optional, Tuple

from . import program_inputs, programs
from .markdown import parse_markdown
from .security import scrub_secrets, scrub_value
from .ui import Printer, UIApprover, format_history_page, run_turn_sync

WEB_EVENT_LIMIT = 500
WEB_EVENT_TEXT_LIMIT = 12000
WEB_MARKDOWN_EVENT_LIMIT = 24 * 1024
WEB_BODY_LIMIT = 64 * 1024
WEB_PROMPT_LIMIT = 8192
WEB_HISTORY_PAGE_SIZE = 12
_ASSET_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}


class WebInterfaceError(ValueError):
    """A user-facing local web interface error."""


class WebState:
    """Bounded, redacted event buffer shared by the worker and HTTP handler threads."""

    def __init__(self, *, initial_prompt: str = "") -> None:
        self._lock = threading.RLock()
        self._events = deque(maxlen=WEB_EVENT_LIMIT)
        self._next_id = 1
        self._busy = False
        self._stopping = False
        self._pending_approval: Optional[Dict[str, Any]] = None
        self.stop_requested = threading.Event()
        self.initial_prompt = scrub_secrets(initial_prompt[:WEB_PROMPT_LIMIT])

    def publish(self, kind: str, data: Optional[Mapping[str, Any]] = None) -> int:
        safe = scrub_value(dict(data or {}))
        if kind == "output" and isinstance(safe, dict) and safe.get("format") == "markdown":
            text = safe.get("text")
            if isinstance(text, str):
                safe["markdown"] = parse_markdown(text)
        try:
            rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            safe = {"text": scrub_secrets(str(data or ""))[:WEB_EVENT_TEXT_LIMIT]}
            rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        payload_limit = WEB_MARKDOWN_EVENT_LIMIT if kind == "output" else WEB_EVENT_TEXT_LIMIT
        if len(rendered) > payload_limit and isinstance(safe, dict) and "markdown" in safe:
            # Keep the bounded raw response available as plain text if pathological
            # formatting would make the syntax tree too large for the event stream.
            safe.pop("markdown", None)
            safe.pop("format", None)
            rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > payload_limit:
            safe = {"text": scrub_secrets(rendered[: WEB_EVENT_TEXT_LIMIT - 32]) + "… [shortened]"}
        with self._lock:
            event_id = self._next_id
            self._next_id += 1
            self._events.append({"id": event_id, "type": str(kind)[:64], "data": safe})
            return event_id

    def set_busy(self, value: bool) -> None:
        with self._lock:
            self._busy = bool(value)

    def set_pending_approval(self, value: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._pending_approval = value

    def clear_pending_approval(self, approval_id: int) -> None:
        with self._lock:
            if self._pending_approval and self._pending_approval.get("id") == approval_id:
                self._pending_approval = None

    def request_stop(self) -> None:
        with self._lock:
            if not self._stopping:
                self._stopping = True
                self._events.append(
                    {"id": self._next_id, "type": "server_stopping", "data": {"text": "Web session stopping…"}}
                )
                self._next_id += 1
        self.stop_requested.set()

    def snapshot(self, since: int) -> Dict[str, Any]:
        with self._lock:
            oldest = self._events[0]["id"] if self._events else self._next_id
            reset = since < oldest - 1
            events = [] if reset else [event for event in self._events if event["id"] > since]
            return {
                "events": list(events),
                "next_id": self._next_id - 1,
                "reset": reset,
                "busy": self._busy,
                "stopping": self._stopping,
                "pending_approval": dict(self._pending_approval) if self._pending_approval else None,
                "initial_prompt": self.initial_prompt,
            }


class _WebStream:
    """File-like adapter that sends Printer output to the bounded browser event stream."""

    def __init__(self, state: WebState) -> None:
        self.state = state

    def write(self, text: str) -> int:
        if text:
            self.state.publish("output", {"text": scrub_secrets(str(text))[:WEB_EVENT_TEXT_LIMIT]})
        return len(text)

    def write_assistant(self, text: str) -> int:
        if text:
            self.state.publish(
                "output",
                {"text": scrub_secrets(str(text))[:WEB_EVENT_TEXT_LIMIT], "format": "markdown"},
            )
        return len(text)

    def flush(self) -> None:
        return None


class WebController:
    """Run chat and explicitly selected saved programs for one browser session."""

    def __init__(
        self,
        *,
        options_factory: Any,
        session: Any,
        approver: Optional[UIApprover] = None,
        verbose: bool = False,
        initial_prompt: str = "",
    ) -> None:
        self.options_factory = options_factory
        self.session = session
        self.approver = approver
        self.verbose = bool(verbose)
        self.state = WebState(initial_prompt=initial_prompt)
        self._lock = threading.RLock()
        self._busy = False
        self._closing = False
        self._worker: Optional[threading.Thread] = None
        self._active_options: Any = None
        self._cancel_current = threading.Event()
        if self.approver is not None:
            self.approver.set_presenter(self._present_approval)

    def _present_approval(self, request: Any, token: int) -> None:
        description = scrub_secrets(request.describe())[:WEB_EVENT_TEXT_LIMIT]
        pending = {
            "id": int(token),
            "tool": scrub_secrets(str(request.tool))[:120],
            "reason": scrub_secrets(str(request.reason))[:1000],
            "description": description,
        }
        self.state.set_pending_approval(pending)
        self.state.publish("approval", pending)

    def start_chat(self, prompt: Any) -> bool:
        if not isinstance(prompt, str):
            raise WebInterfaceError("message must be text")
        prompt = prompt.strip()
        if not prompt:
            raise WebInterfaceError("enter a message first")
        if len(prompt) > WEB_PROMPT_LIMIT:
            raise WebInterfaceError("message is too long (maximum {} characters)".format(WEB_PROMPT_LIMIT))
        return self._start("chat", prompt)

    def start_saved_program(self, program_id: Any, values: Any) -> bool:
        if not isinstance(program_id, str) or not program_id.strip():
            raise WebInterfaceError("choose a saved program")
        if not isinstance(values, dict):
            raise WebInterfaceError("program inputs must be an object")
        if len(json.dumps(values, ensure_ascii=False)) > 32000:
            raise WebInterfaceError("program inputs are too large")
        return self._start("program", {"program_id": program_id.strip(), "values": values})

    def _start(self, kind: str, payload: Any) -> bool:
        with self._lock:
            if self._closing or self.state.stop_requested.is_set():
                raise WebInterfaceError("the web session is stopping")
            if self._busy:
                return False
            self._cancel_current.clear()
            self._busy = True
            self.state.set_busy(True)
            self.state.publish("operation_started", {"kind": kind})
            worker = threading.Thread(
                target=self._run,
                args=(kind, payload),
                name="pyto-harness-web-{}".format(kind),
                daemon=True,
            )
            self._worker = worker
            try:
                worker.start()
            except BaseException:
                self._busy = False
                self._worker = None
                self.state.set_busy(False)
                raise
        return True

    def _run(self, kind: str, payload: Any) -> None:
        options = None
        try:
            if self.approver is not None:
                self.approver.reset()
            options = self.options_factory(self.session)
            if kind == "chat" and options.client is not None:
                options.client.reset_cancel()
            if self._cancel_current.is_set():
                stop = getattr(options, "stop", None)
                if stop is not None:
                    stop.set()
                client = getattr(options, "client", None)
                if client is not None:
                    client.cancel()
            with self._lock:
                if self._closing:
                    if options.client is not None:
                        options.client.cancel()
                    return
                self._active_options = options

            if kind == "chat":
                prompt = payload
                self.state.publish("user", {"text": scrub_secrets(prompt)})
                printer = Printer(verbose=self.verbose, stream=_WebStream(self.state))
                printer.write("Working…")
                result = run_turn_sync(options, prompt, printer)
                self.state.publish(
                    "turn_finished",
                    {
                        "stop": result.get("stop", "unknown"),
                        "finished": bool(result.get("finished")),
                        "errors": scrub_secrets(str(result.get("errors") or ""))[:2000],
                    },
                )
            else:
                self._run_saved(options, payload["program_id"], payload["values"])
        except Exception as exc:  # noqa: BLE001 - send a bounded actionable error to the browser
            self.state.publish(
                "error",
                {"text": scrub_secrets("{}: {}".format(type(exc).__name__, exc))[:2000]},
            )
        finally:
            with self._lock:
                self._active_options = None
                self._busy = False
                self._worker = None
                self.state.set_busy(False)
            self.state.publish("operation_finished", {"kind": kind})

    def _run_saved(self, options: Any, program_id: str, values: Mapping[str, Any]) -> None:
        registry = getattr(options, "registry", None)
        context = getattr(self.options_factory, "context", None)
        workspace = getattr(context, "workspace", None)
        if registry is None or workspace is None:
            raise WebInterfaceError("saved programs are unavailable in this front end")
        record = programs.find_program(workspace, program_id)
        schema = program_inputs.normalize_schema(record.get("input_schema"))
        validated = program_inputs.validate_values(schema, values) if schema else {}
        self.state.publish(
            "program_started",
            {"id": record["id"], "title": record["title"], "entry_file": record["entry_file"]},
        )
        result = programs.execute_saved(registry, record, input_values=validated if schema else None)
        self.state.publish(
            "program_result",
            {
                "id": record["id"],
                "title": record["title"],
                "is_error": bool(getattr(result, "is_error", True)),
                "content": scrub_secrets(str(getattr(result, "content", "")))[:WEB_EVENT_TEXT_LIMIT],
            },
        )

    def programs_payload(self) -> Dict[str, Any]:
        context = getattr(self.options_factory, "context", None)
        workspace = getattr(context, "workspace", None)
        if workspace is None:
            raise WebInterfaceError("saved programs are unavailable in this front end")
        records = []
        for record in programs.list_programs(workspace):
            records.append(
                scrub_value(
                    {
                        "id": record["id"],
                        "title": record["title"],
                        "purpose": record.get("purpose", ""),
                        "entry_file": record["entry_file"],
                        "mode": record["mode"],
                        "required_capabilities": record.get("required_capabilities", []),
                        "last_verification_result": record.get("last_verification_result", {}),
                        "input_schema": program_inputs.normalize_schema(record.get("input_schema")),
                        "entry_exists": bool(record.get("entry_exists")),
                    }
                )
            )
        return {"programs": records}

    def history_payload(self, offset: int = 0) -> Dict[str, Any]:
        # Use a snapshot so a simultaneous append cannot mutate the list being rendered.
        messages = self.session.project()
        body, older = format_history_page(
            messages,
            offset=offset,
            page_size=WEB_HISTORY_PAGE_SIZE,
            session_path=str(getattr(self.session, "path", "") or ""),
        )
        return {"text": body, "has_older": older, "offset": max(0, int(offset)), "total": len(messages)}

    def answer_approval(self, approval_id: Any, allow: Any) -> bool:
        if self.approver is None:
            raise WebInterfaceError("approvals are not active for this session")
        if isinstance(approval_id, bool) or not isinstance(approval_id, int):
            raise WebInterfaceError("approval id is invalid")
        if not isinstance(allow, bool):
            raise WebInterfaceError("approval answer must be Allow or Deny")
        answered = self.approver.answer(approval_id, allow)
        if answered:
            self.state.clear_pending_approval(approval_id)
            self.state.publish("approval_answered", {"id": approval_id, "allow": allow})
        return answered

    def stop_turn(self) -> bool:
        with self._lock:
            if not self._busy:
                return False
            self._cancel_current.set()
            options = self._active_options
        approval_id = self.approver.current_token() if self.approver is not None else None
        if options is not None:
            stop = getattr(options, "stop", None)
            if stop is not None:
                stop.set()
            client = getattr(options, "client", None)
            if client is not None:
                client.cancel()
        if self.approver is not None:
            self.approver.cancel()
        if approval_id is not None:
            self.state.clear_pending_approval(approval_id)
            self.state.publish("approval_answered", {"id": approval_id, "allow": False})
        self.state.publish("status", {"text": "Stopping the current operation…"})
        return True

    def request_stop_session(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        self.stop_turn()
        if self.approver is not None:
            self.approver.close()
        self.state.request_stop()

    def close(self) -> None:
        """Cancel chat work and drain the worker before the caller closes session resources."""
        self.request_stop_session()
        with self._lock:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()


class _LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def get_request(self) -> Tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(10)
        return request, address


class LocalWebServer:
    """Ephemeral, token-protected server bound only to the device loopback interface."""

    def __init__(self, controller: WebController) -> None:
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.server = _LoopbackHTTPServer(("127.0.0.1", 0), self._handler_type())
        self.server.controller = controller  # type: ignore[attr-defined]
        self.server.token = self.token  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    def _handler_type(self) -> Any:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "pyto-harness"
            sys_version = ""

            def log_message(self, _format: str, *args: Any) -> None:  # noqa: A002
                # Request paths contain the per-run bearer token; never log them.
                return None

            def do_GET(self) -> None:  # noqa: N802
                if not self._valid_host():
                    self._send_json(403, {"error": "Host not allowed"})
                    return
                route = self._route()
                if route is None:
                    self._send_json(404, {"error": "Not found"})
                    return
                if route == "":
                    self._send_asset("index.html")
                    return
                if route.startswith("asset/"):
                    name = route[6:]
                    if name not in _ASSET_TYPES:
                        self._send_json(404, {"error": "Not found"})
                        return
                    self._send_asset(name)
                    return
                if not route.startswith("api/"):
                    self._send_json(404, {"error": "Not found"})
                    return
                if not self._authorized():
                    self._send_json(401, {"error": "Unauthorized"})
                    return
                origin_error = self._origin_error()
                if origin_error:
                    self._send_json(403, {"error": origin_error})
                    return
                try:
                    if route == "api/state":
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                        since = self._query_int(query, "since", default=0, maximum=2**53 - 1)
                        self._send_json(200, parent.controller.state.snapshot(since))
                    elif route == "api/history":
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                        offset = self._query_int(query, "offset", default=0, maximum=100000)
                        self._send_json(200, parent.controller.history_payload(offset))
                    elif route == "api/programs":
                        self._send_json(200, parent.controller.programs_payload())
                    else:
                        self._send_json(404, {"error": "Not found"})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": scrub_secrets("{}: {}".format(type(exc).__name__, exc))[:1000]})

            def do_POST(self) -> None:  # noqa: N802
                if not self._valid_host():
                    self._send_json(403, {"error": "Host not allowed"})
                    return
                route = self._route()
                if route is None or not route.startswith("api/"):
                    self._send_json(404, {"error": "Not found"})
                    return
                if not self._authorized():
                    self._send_json(401, {"error": "Unauthorized"})
                    return
                origin_error = self._origin_error()
                if origin_error:
                    self._send_json(403, {"error": origin_error})
                    return
                try:
                    payload = self._read_json()
                    if route == "api/chat":
                        started = parent.controller.start_chat(payload.get("prompt"))
                        if not started:
                            self._send_json(409, {"error": "An operation is already running"})
                        else:
                            self._send_json(202, {"accepted": True})
                    elif route == "api/program/run":
                        started = parent.controller.start_saved_program(
                            payload.get("id"), payload.get("values", {})
                        )
                        if not started:
                            self._send_json(409, {"error": "An operation is already running"})
                        else:
                            self._send_json(202, {"accepted": True})
                    elif route == "api/approval":
                        answered = parent.controller.answer_approval(payload.get("id"), payload.get("allow"))
                        if not answered:
                            self._send_json(409, {"error": "Approval is stale or already answered"})
                        else:
                            self._send_json(200, {"answered": True})
                    elif route == "api/stop-turn":
                        stopped = parent.controller.stop_turn()
                        self._send_json(200, {"stopped": stopped})
                    elif route == "api/stop":
                        self._send_json(200, {"stopping": True})
                        parent.controller.request_stop_session()
                    else:
                        self._send_json(404, {"error": "Not found"})
                except WebInterfaceError as exc:
                    self._send_json(400, {"error": scrub_secrets(str(exc))[:1000]})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(400, {"error": scrub_secrets(str(exc) or "Invalid request")[:1000]})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": scrub_secrets("{}: {}".format(type(exc).__name__, exc))[:1000]})

            def _route(self) -> Optional[str]:
                parsed = urllib.parse.urlsplit(self.path)
                prefix = "/ui/{}/".format(parent.token)
                if not parsed.path.startswith(prefix):
                    return None
                return parsed.path[len(prefix):]

            def _valid_host(self) -> bool:
                expected = "127.0.0.1:{}".format(parent.server.server_address[1])
                return self.headers.get("Host", "").lower() == expected.lower()

            def _authorized(self) -> bool:
                supplied = self.headers.get("X-Pyto-Harness-Token", "")
                return secrets.compare_digest(supplied, parent.token)

            def _origin_error(self) -> str:
                origin = self.headers.get("Origin")
                if origin is None:
                    return ""
                expected = "http://127.0.0.1:{}".format(parent.server.server_address[1])
                return "Origin not allowed" if origin.rstrip("/") != expected else ""

            def _read_json(self) -> Dict[str, Any]:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise WebInterfaceError("send application/json")
                raw_length = self.headers.get("Content-Length")
                if raw_length is None or not raw_length.isdigit():
                    raise WebInterfaceError("request length is missing or invalid")
                length = int(raw_length)
                if length < 1 or length > WEB_BODY_LIMIT:
                    raise WebInterfaceError("request body must be between 1 and {} bytes".format(WEB_BODY_LIMIT))
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise WebInterfaceError("request body must be valid UTF-8 JSON") from exc
                if not isinstance(value, dict):
                    raise WebInterfaceError("request body must be a JSON object")
                return value

            def _query_int(self, query: Mapping[str, Any], name: str, *, default: int, maximum: int) -> int:
                values = query.get(name, [str(default)])
                if not values or not str(values[0]).isdigit():
                    raise WebInterfaceError("{} must be a non-negative integer".format(name))
                value = int(values[0])
                if value > maximum:
                    raise WebInterfaceError("{} is too large".format(name))
                return value

            def _send_asset(self, name: str) -> None:
                asset_path = os.path.join(os.path.dirname(__file__), "web_assets", name)
                try:
                    with open(asset_path, "rb") as handle:
                        body = handle.read(512 * 1024 + 1)
                except OSError:
                    self._send_json(500, {"error": "Web interface asset is missing"})
                    return
                if len(body) > 512 * 1024:
                    self._send_json(500, {"error": "Web interface asset is too large"})
                    return
                self._send_bytes(200, body, _ASSET_TYPES[name])

            def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._send_bytes(status, raw, "application/json; charset=utf-8")

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                    "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    @property
    def url(self) -> str:
        return "http://127.0.0.1:{}/ui/{}/".format(self.server.server_address[1], self.token)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("web server already started")
        self._thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05),
            name="pyto-harness-web-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self._thread.join(timeout=5)
        else:
            self.server.server_close()


def _background_task() -> Any:
    """Return Pyto's supported keepalive task, or ``None`` off-device."""
    try:
        import background  # type: ignore
    except ImportError:
        return None
    task = background.BackgroundTask(id="pyto-harness-web")
    try:
        task.reminder_notifications = False
    except (AttributeError, TypeError):
        pass
    return task


class _BackgroundKeepalive:
    """Own Pyto's keepalive from a helper thread so stopping cannot raise into the server.

    Pyto's BackgroundTask.stop() raises TaskExit into the thread that called start().
    Starting it on the web-server thread would let a stop request interrupt cleanup itself.
    """

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._task: Any = None
        self._error: Optional[BaseException] = None
        self._stop_started = False
        self._thread = threading.Thread(
            target=self._run,
            name="pyto-harness-background-keepalive",
            daemon=True,
        )

    def _run(self) -> None:
        try:
            self._task = _background_task()
            if self._task is not None:
                self._task.start()
        except BaseException as exc:  # keep startup errors visible to the foreground thread
            self._error = exc
        finally:
            self._ready.set()
        if self._task is None or self._error is not None:
            return
        try:
            self._stop_requested.wait()
        except BaseException as exc:  # Pyto may asynchronously raise TaskExit from stop()
            if type(exc).__name__ != "TaskExit":
                self._error = exc

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("Pyto background keepalive did not start within 10 seconds")
        if self._error is not None:
            error = self._error
            self.stop()
            raise RuntimeError(
                "could not start Pyto background keepalive: {}: {}".format(type(error).__name__, error)
            ) from error

    def stop(self) -> None:
        with self._lock:
            if self._stop_started:
                return
            self._stop_started = True
            task = self._task
        # Wake the helper first. BackgroundTask.stop() may also raise TaskExit in it.
        # Either way, the exception target is this helper thread, never the web server.
        self._stop_requested.set()
        if task is not None:
            task.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Pyto background keepalive did not stop within 5 seconds")


def run_web(
    controller: WebController,
    *,
    open_browser: bool = True,
    keep_alive: bool = True,
    on_ready: Any = None,
) -> int:
    """Serve one browser session until its Stop button is used or Pyto stops the script.

    Pyto backgrounds this script when Safari opens. Its BackgroundTask is therefore
    started only for this explicitly invoked web session and always stopped on exit.
    """
    local_server: Optional[LocalWebServer] = None
    keepalive = None
    try:
        local_server = LocalWebServer(controller)
        local_server.start()
        if keep_alive:
            keepalive = _BackgroundKeepalive()
            keepalive.start()
        print("pyto-harness web interface: {}".format(local_server.url))
        print("Use Stop web session in the page, or stop this script in Pyto to close it.")
        if callable(on_ready):
            on_ready(local_server.url)
        if open_browser and os.environ.get("PYTO_HARNESS_NO_BROWSER") != "1":
            try:
                opened = webbrowser.open(local_server.url, new=2)
                if not opened:
                    print("The browser did not open automatically; open the local URL shown above.")
            except Exception as exc:  # noqa: BLE001
                print("Could not open the browser automatically: {}: {}".format(type(exc).__name__, exc))
        while not controller.state.stop_requested.wait(0.2):
            pass
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        controller.request_stop_session()
        if local_server is not None:
            local_server.close()
        controller.close()
        if keepalive is not None:
            try:
                keepalive.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup should still finish
                print("Could not stop Pyto background task: {}: {}".format(type(exc).__name__, exc))

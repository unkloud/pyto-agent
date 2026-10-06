"""Front ends: the Pyto UI when it exists, otherwise a terminal REPL.

The UI is deliberately small.  The pattern that matters is not the layout — it is that
**the network never runs on the UI thread**.  A Pyto button handler starts a plain
``threading.Thread`` for model work. Pyto's documented ``pyto_ui`` view wrappers can be
modified from another thread; direct UIKit calls have separate main-thread requirements
and are not used here. ``ui.show_view`` keeps the script and its session resources alive
until the view closes.

If ``pyto_ui`` is missing (Linux, a test, a plain Pyto console session) the caller gets
:func:`terminal_repl` instead.  :func:`run_ui` raises :class:`UnsupportedCapability` in
that case so ``run.py --ui`` can explain itself rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import UnsupportedCapability
from .home import expand_user_path
from .loop import ApprovalRequest, Event, LoopOptions, make_policy, run_turn
from . import program_inputs, programs
from .session import SessionLog, new_session_path
from .security import scrub_secrets, scrub_value

TRANSCRIPT_LIMIT_CHARS = 80_000
UI_STREAM_FLUSH_INTERVAL_SECONDS = 0.06
UI_STREAM_FLUSH_CHARS = 2048
UI_HISTORY_PAGE_MESSAGES = 12
UI_HISTORY_MESSAGE_CHARS = 1800


def pyto_ui_module() -> Any:
    """Return the ``pyto_ui`` module, or ``None`` when it is not available."""
    try:
        import pyto_ui  # type: ignore
    except ImportError:
        return None
    return pyto_ui


def is_ui_available() -> bool:
    return pyto_ui_module() is not None


@dataclass
class Transcript:
    """Text buffer behind the chat view.  Pure data, so it is testable without Pyto."""

    lines: List[str] = field(default_factory=list)
    _partial: str = ""

    def add(self, text: str) -> None:
        if not text:
            return
        if len(text) > TRANSCRIPT_LIMIT_CHARS:
            text = text[-TRANSCRIPT_LIMIT_CHARS:]
        self.lines.append(text)
        self._trim()

    def append_delta(self, text: str) -> None:
        self._partial += text
        if len(self._partial) > TRANSCRIPT_LIMIT_CHARS:
            self._partial = self._partial[-TRANSCRIPT_LIMIT_CHARS:]
        self._trim()

    def flush_delta(self, prefix: str = "") -> None:
        if self._partial:
            self.add(("{}{}".format(prefix, self._partial)).rstrip())
        self._partial = ""

    def complete_message(self, text: str) -> None:
        """Commit one assistant message, preferring the final body over partial deltas."""
        content = (text or "").strip()
        partial = self._partial.strip()
        self._partial = ""
        if content:
            self.add(partial if partial == content else content)
        elif partial:
            self.add(partial)

    def _trim(self) -> None:
        total = sum(len(line) for line in self.lines) + len(self._partial)
        while total > TRANSCRIPT_LIMIT_CHARS and len(self.lines) > 1:
            total -= len(self.lines.pop(0))
        if total > TRANSCRIPT_LIMIT_CHARS and self.lines:
            total -= len(self.lines.pop(0))
        if total > TRANSCRIPT_LIMIT_CHARS:
            self._partial = self._partial[-TRANSCRIPT_LIMIT_CHARS:]

    def render(self) -> str:
        body = "\n".join(self.lines)
        if self._partial:
            body += ("\n" if body else "") + self._partial
        return body


def format_history_page(
    messages: Any,
    *,
    offset: int = 0,
    page_size: int = UI_HISTORY_PAGE_MESSAGES,
    session_path: str = "",
) -> tuple[str, bool]:
    """Format one bounded page of durable provider history for the chat's History view."""
    if not isinstance(messages, (list, tuple)):
        messages = []
    total = len(messages)
    safe_offset = max(0, int(offset))
    safe_page_size = max(1, int(page_size))
    end = max(0, total - safe_offset)
    start = max(0, end - safe_page_size)
    rows = ["Session history · messages {}–{} of {}".format(start + 1 if end else 0, end, total)]
    if session_path:
        rows.append("Durable session: {}".format(scrub_secrets(session_path)))
    rows.append("The full available log stays on disk; long entries are shortened in this view.")

    for index, message in enumerate(messages[start:end], start=start + 1):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        call_details = []
        if role == "tool":
            speaker = "Tool · {} · call {}".format(
                message.get("name") or "result", message.get("tool_call_id") or "unknown"
            )
        elif role == "assistant":
            calls = []
            for call in message.get("tool_calls") or ():
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and function.get("name"):
                    calls.append(str(function["name"]))
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
                        except (TypeError, ValueError):
                            pass
                    call_details.append(
                        "{} [{}]\n{}".format(
                            function["name"],
                            call.get("id", "unknown") if isinstance(call, dict) else "unknown",
                            scrub_secrets(str(arguments or "{}")),
                        )
                    )
            speaker = "Assistant" + (" · called {}".format(", ".join(calls)) if calls else "")
        else:
            speaker = role.capitalize()
        body = scrub_secrets(str(message.get("content") or "")).strip()
        if call_details:
            body = (body + "\n\n" if body else "") + "Tool request details:\n" + "\n\n".join(call_details)
        if len(body) > UI_HISTORY_MESSAGE_CHARS:
            first = UI_HISTORY_MESSAGE_CHARS * 2 // 3
            last = UI_HISTORY_MESSAGE_CHARS - first
            omitted = len(body) - UI_HISTORY_MESSAGE_CHARS
            body = body[:first] + "\n… {} characters omitted from this page …\n".format(omitted) + body[-last:]
        rows.append("\n{}. {}\n{}".format(index, speaker, body or "(no text)"))

    return "\n".join(rows), start > 0


# --------------------------------------------------------------------------------------
# Event -> text formatting (shared by both front ends)
# --------------------------------------------------------------------------------------


class Printer:
    """Turns loop events into user-visible text, in one place for both front ends."""

    def __init__(self, *, verbose: bool = False, stream: Any = None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.transcript = Transcript()
        self._last_message: Optional[bytes] = None
        self._denied_ids = set()

    def write(self, text: str, *, newline: bool = True) -> None:
        self.stream.write(text + ("\n" if newline else ""))
        self.stream.flush()

    def handle(self, event: Event) -> None:
        kind = event.kind
        data = event.data
        if kind in ("delta", "reasoning.delta"):
            # Wait for message.completed, whose whole text is scrubbed before display.
            # This prevents a credential split across provider chunks from leaking.
            return
        elif kind == "message.completed":
            content = scrub_secrets(str(data.get("content") or "")).strip()
            reasoning = scrub_secrets(str(data.get("reasoning") or "")).strip()
            self.transcript.complete_message(content)
            if self.verbose and reasoning:
                self.write("[reasoning]")
                for line in reasoning.splitlines():
                    self.write("  {}".format(line))
            if content:
                # Display the scrubbed, complete body once for both provider modes.
                self.write(content)
            latest = content
            self._last_message = _message_key(latest) if latest else None
        elif kind == "tool.started":
            name = str(data.get("name") or "tool")
            if self.verbose:
                self.write("\n  -> {}({})".format(name, _format_args(data.get("arguments"))))
            else:
                self.write("\n{}".format(_tool_progress(name)))
        elif kind == "tool.completed":
            name = str(data.get("name") or "tool")
            call_id = data.get("id")
            if self.verbose:
                marker = "!!" if data.get("is_error") else "ok"
                self.write("  <- [{}] {} ({:.0f} ms)".format(marker, name, data.get("duration_ms") or 0))
                self._write_detail_body(
                    data.get("content"),
                    truncated=bool(data.get("truncated")),
                    metadata=data.get("metadata") or {},
                )
            elif call_id not in self._denied_ids:
                if data.get("is_error"):
                    detail = _concise_error(data.get("content", ""))
                    suffix = ": {}".format(detail) if detail else ""
                    self.write("Could not complete {}{}".format(name.replace("_", " "), suffix))
                else:
                    status = _tool_success(name, data.get("metadata") or {}, data.get("truncated", False))
                    if status:
                        self.write(status)
            if call_id is not None:
                self._denied_ids.discard(call_id)
        elif kind == "tool.denied":
            name = str(data.get("name") or "tool")
            call_id = data.get("id")
            if call_id is not None:
                self._denied_ids.add(call_id)
            reason = _concise_error(data.get("reason") or "no reason provided", limit=200)
            self.write("Not run: {} — {}".format(name.replace("_", " "), reason))
        elif kind == "finished":
            message = scrub_secrets(str(data.get("message", ""))).strip()
            message_key = _message_key(message) if message else None
            if message and message_key != self._last_message:
                self.write(message)
                self.transcript.add(message)
            if message_key is not None:
                self._last_message = message_key
        elif kind == "turn.limit":
            self.write("Turn limit reached ({}).".format(data.get("limit")))
        elif kind == "error":
            message = scrub_secrets(str(data.get("message") or "An unexpected error occurred."))
            if data.get("code") == "CANCELLED" or message.strip().lower() == "cancelled":
                self.write("Interrupted.")
            else:
                if not self.verbose:
                    message = _concise_error(message, limit=240)
                self.write("Error: {}".format(message))
        elif kind == "usage" and self.verbose:
            self.write("[usage] {}".format(data))
        elif kind == "turn.started":
            if data.get("turn") == 1:
                self._last_message = None
            if self.verbose:
                self.write("[turn {}]".format(data.get("turn")))
            elif data.get("turn") == 1:
                self.write("Working…")
        elif kind == "status":
            message = scrub_secrets(str(data.get("message") or ""))
            if message:
                self.write(message)

    def _write_detail_body(self, body: Any, *, truncated: bool = False, metadata: Any = None) -> None:
        text = scrub_secrets(str(body or "")).strip()
        if text:
            for line in text.splitlines():
                self.write("     | {}".format(line))
        if truncated:
            details = metadata if isinstance(metadata, dict) else {}
            path = scrub_secrets(str(details.get("spill_path") or ""))
            suffix = "; full output saved to {}".format(path) if path else ""
            self.write("     | [tool result was shortened{}]".format(suffix))


def _format_args(arguments: Any) -> str:
    """Render complete, already-scrubbed arguments for explicit verbose diagnostics."""
    if not isinstance(arguments, dict):
        return ""
    safe = scrub_value(arguments)
    return ", ".join("{}={!r}".format(key, value) for key, value in safe.items())


def _message_key(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", "replace")).digest()


_TOOL_PROGRESS = {
    "write_program": "Saving program…",
    "run_program": "Running program…",
    "register_program": "Adding program to your library…",
    "list_saved_programs": "Finding saved programs…",
    "pyto_api": "Checking the Pyto API reference…",
    "list_files": "Listing workspace files…",
    "read_file": "Reading a file…",
    "write_file": "Saving a file…",
    "edit_file": "Updating a file…",
    "search_files": "Searching workspace files…",
    "clipboard_get": "Reading the clipboard…",
    "clipboard_set": "Copying to the clipboard…",
    "share_text": "Preparing the share sheet…",
    "open_url": "Opening a link…",
    "notify": "Sending a notification…",
    "speak": "Speaking the requested text…",
    "shortcut_run": "Running a Shortcut…",
    "shortcut_run_wait": "Waiting for the Shortcut…",
    "save_photo": "Saving a photo…",
    "open_in_files": "Opening a file…",
    "calendar_add_event": "Adding a calendar event…",
    "calendar_list_events": "Checking calendar events…",
    "device_capabilities": "Checking device capabilities…",
    "diagnose": "Checking the installation…",
    "selftest": "Running a self-check…",
    "memory_read": "Reading project notes…",
    "memory_write": "Saving project notes…",
    "finish": "Preparing your result…",
}


def _tool_progress(name: str) -> str:
    return _TOOL_PROGRESS.get(name, "Working: {}…".format(name.replace("_", " ")))


def _tool_success(name: str, metadata: Any, truncated: bool = False) -> str:
    """Return a short user-facing completion only when it conveys a useful outcome."""
    details = metadata if isinstance(metadata, dict) else {}
    path = scrub_secrets(str(details.get("path") or ""))
    if name == "write_program":
        result = "Saved program{}".format(": {}".format(path) if path else ".")
    elif name == "register_program":
        title = scrub_secrets(str(details.get("title") or "program"))
        program_id = scrub_secrets(str(details.get("program_id") or ""))
        result = "Added {} to saved programs{}".format(
            title, " (id {})".format(program_id) if program_id else ""
        )
    elif name == "write_file":
        result = "Saved file{}".format(": {}".format(path) if path else ".")
    elif name == "edit_file":
        result = "Updated file{}".format(": {}".format(path) if path else ".")
    elif name == "run_program":
        result = "Program ran successfully."
    elif name == "clipboard_set":
        result = "Copied to the clipboard."
    elif name == "share_text":
        result = "Shared the requested text."
    elif name == "open_url":
        result = "Opened the link."
    elif name == "calendar_add_event":
        result = "Added the calendar event."
    elif name == "finish":
        return ""
    else:
        return ""
    if truncated:
        spill = scrub_secrets(str(details.get("spill_path") or ""))
        result += " Full output{}.".format(" saved to {}".format(spill) if spill else " was shortened")
    return result


def _concise_error(body: Any, limit: int = 240) -> str:
    """Keep a short actionable diagnostic visible without dumping a traceback or output."""
    text = scrub_secrets(str(body or "")).strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    stderr_index = next((i for i, line in enumerate(lines) if line == "--- stderr ---"), None)
    if stderr_index is not None:
        diagnostic = lines[stderr_index + 1 :]
        end = next((i for i, line in enumerate(diagnostic) if line.startswith("---")), len(diagnostic))
        diagnostic = diagnostic[:end]
        if diagnostic:
            lines = diagnostic[:1] if len(diagnostic) == 1 else [diagnostic[0], diagnostic[-1]]
    elif len(lines) > 1 and lines[0].startswith(("FAILED ", "TIMED OUT ")):
        lines = lines[1:3]
    rendered = " — ".join(line for line in lines if not line.startswith("---"))
    return rendered if len(rendered) <= limit else rendered[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------------------
# Interactive approval prompt (terminal)
# --------------------------------------------------------------------------------------


class TerminalApprover:
    """Ask y/n on stdin.  ``--yolo`` replaces this with an always-allow prompter.

    With no interactive stdin (a pipe, a Shortcut invocation, a test) there is nobody to
    answer, so the call is **denied** and the transcript says why.  Blocking on ``input()``
    in a non-interactive run would hang the app with no way for the user to respond.
    """

    def __init__(
        self,
        *,
        assume_yes: bool = False,
        input_fn: Callable[[str], str] = input,
        interactive: Optional[bool] = None,
    ) -> None:
        self.assume_yes = assume_yes
        self.input_fn = input_fn
        if interactive is None:
            try:
                interactive = bool(sys.stdin) and sys.stdin.isatty()
            except (ValueError, AttributeError):  # pragma: no cover - closed stdin
                interactive = False
        self.interactive = interactive
        self.answers: List[bool] = []

    def __call__(self, request: Any) -> bool:
        if self.assume_yes:
            self.answers.append(True)
            return True
        if not self.interactive:
            print(
                "\n[approval needed] {}\n  -> denied: nothing is attached to answer "
                "(re-run with --yolo to allow it)".format(request.describe())
            )
            self.answers.append(False)
            return False
        print("\n[approval needed] {}".format(request.describe()))
        try:
            answer = self.input_fn("Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        granted = answer in ("y", "yes")
        self.answers.append(granted)
        return granted


@dataclass
class _ApprovalTicket:
    token: int
    request: ApprovalRequest
    answered: threading.Event = field(default_factory=threading.Event)
    answer: Optional[bool] = None


class UIApprover:
    """Queue policy prompts for explicit answers in the visible Pyto chat."""

    interactive = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queue: List[_ApprovalTicket] = []
        self._presenter: Optional[Callable[[ApprovalRequest, int], None]] = None
        self._next_token = 1
        self._cancelled = False
        self._closed = False

    def set_presenter(self, presenter: Callable[[ApprovalRequest, int], None]) -> None:
        """Attach the GUI display callback and show a request already waiting, if any."""
        with self._lock:
            self._presenter = presenter
            ticket = self._queue[0] if self._queue else None
        if ticket is not None:
            self._present(ticket)

    def __call__(self, request: ApprovalRequest) -> bool:
        with self._lock:
            if self._closed or self._cancelled or self._presenter is None:
                return False
            ticket = _ApprovalTicket(self._next_token, request)
            self._next_token += 1
            is_current = not self._queue
            self._queue.append(ticket)
        if is_current:
            self._present(ticket)
        ticket.answered.wait()
        return ticket.answer is True

    def answer(self, token: int, granted: bool) -> bool:
        """Resolve only the displayed request; stale and repeated taps are ignored."""
        with self._lock:
            if not self._queue or self._queue[0].token != token:
                return False
            ticket = self._queue.pop(0)
            ticket.answer = bool(granted)
            ticket.answered.set()
            next_ticket = self._queue[0] if self._queue else None
        if next_ticket is not None:
            self._present(next_ticket)
        return True

    def current_token(self) -> Optional[int]:
        """Return the active request token, if one is still waiting for an answer."""
        with self._lock:
            return self._queue[0].token if self._queue else None

    def cancel(self) -> None:
        """Deny all outstanding requests and reject new ones until the next turn."""
        with self._lock:
            self._cancelled = True
            waiting, self._queue = self._queue, []
            for ticket in waiting:
                ticket.answer = False
                ticket.answered.set()

    def reset(self) -> None:
        """Allow a fresh chat turn to ask after the prior turn was stopped."""
        with self._lock:
            if not self._closed:
                self._cancelled = False

    def close(self) -> None:
        """Permanently deny pending and future requests when the chat window closes."""
        with self._lock:
            self._closed = True
            self._cancelled = True
            waiting, self._queue = self._queue, []
            for ticket in waiting:
                ticket.answer = False
                ticket.answered.set()

    def _present(self, ticket: _ApprovalTicket) -> None:
        with self._lock:
            presenter = self._presenter
            is_current = bool(self._queue and self._queue[0] is ticket)
        if presenter is None or not is_current:
            return
        try:
            presenter(ticket.request, ticket.token)
        except Exception:  # noqa: BLE001 - a broken UI fails closed, never opens the gate
            self.answer(ticket.token, False)


# --------------------------------------------------------------------------------------
# Terminal REPL
# --------------------------------------------------------------------------------------


def run_turn_sync(options: LoopOptions, prompt: str, printer: Printer) -> Dict[str, Any]:
    """Drive one turn to completion from synchronous code.  Returns the ``done`` payload."""
    done: Dict[str, Any] = {}

    async def drive() -> None:
        nonlocal done
        async for event in run_turn(options, prompt):
            printer.handle(event)
            if event.kind == "done":
                done = event.data

    asyncio.run(drive())
    return done


def _library_workspace(options_factory: Callable[..., Any]) -> Any:
    """Return the active workspace object when the front end uses the standard factory."""
    context = getattr(options_factory, "context", None)
    return getattr(context, "workspace", None)


def collect_program_inputs_ui(
    ui: Any,
    record: Dict[str, Any],
    *,
    file_system: Any = None,
) -> Dict[str, Any]:
    """Render a scrollable PytoUI form and return typed values after explicit submission."""
    schema = program_inputs.normalize_schema(record.get("input_schema"))
    if not schema:
        return {}
    view = ui.View()
    view.title = "Run {}".format(record.get("title", "saved program"))
    view.background_color = ui.COLOR_SYSTEM_BACKGROUND

    width = max(220, view.width)
    scroll = ui.ScrollView()
    scroll.frame = (0, 52, width, max(100, view.height - 150))
    scroll.content_width = width
    scroll.content_height = max(100, len(schema) * 78 + 12)
    scroll.vertical = True
    content = scroll.content_view
    content.frame = (0, 0, width, scroll.content_height)
    view.add_subview(scroll)

    raw_values: Dict[str, Any] = {}
    controls: Dict[str, Any] = {}
    field_states: Dict[str, Dict[str, Any]] = {}
    for index, field in enumerate(schema):
        name = field["name"]
        top = 8 + index * 78
        label = ui.Label("{}{}".format(field["label"], " (required)" if field["required"] else ""))
        label.frame = (14, top, width - 28, 22)
        content.add_subview(label)
        kind = field["type"]
        if kind in ("text", "number"):
            default = field.get("default", "")
            field_control = ui.TextField(text="" if default is None else str(default), placeholder=field["label"])
            field_control.frame = (14, top + 25, width - 28, 40)
            if kind == "number":
                keyboard_type = getattr(ui, "KeyboardType", None)
                decimal_pad = getattr(keyboard_type, "DECIMAL_PAD", None) if keyboard_type is not None else None
                if decimal_pad is not None:
                    field_control.keyboard_type = decimal_pad
            controls[name] = field_control
            content.add_subview(field_control)
        elif kind == "choice":
            choices = field["choices"]
            default = field.get("default", choices[0])
            state = {"value": default}
            button = ui.Button(title="{}: {}".format(field["label"], state["value"]))
            button.frame = (14, top + 25, width - 28, 40)

            def next_choice(_sender: Any = None, *, current=state, options=choices, title=field["label"], target=button) -> None:
                position = options.index(current["value"]) if current["value"] in options else -1
                current["value"] = options[(position + 1) % len(options)]
                target.title = "{}: {}".format(title, current["value"])

            button.action = next_choice
            controls[name] = button
            field_states[name] = state
            content.add_subview(button)
        else:
            status = ui.Label("Not selected")
            status.number_of_lines = 1
            status.frame = (14, top + 26, max(80, width - 150), 36)
            browse = ui.Button(title="Pick {}".format(kind))
            browse.frame = (width - 130, top + 25, 116, 40)
            state = {"value": None}

            def choose_path(
                _sender: Any = None,
                *,
                current=state,
                target=status,
                descriptor=field,
            ) -> None:
                try:
                    selected = program_inputs.pick_value(descriptor, file_system)
                    current["value"] = selected
                    base = os.path.basename(selected.rstrip(os.sep)) or selected
                    target.text = "Selected: {}".format(base)
                except program_inputs.InputsCancelled:
                    current["value"] = None
                    target.text = "Selection cancelled"
                except program_inputs.ProgramInputError as exc:
                    current["value"] = None
                    target.text = str(exc)

            browse.action = choose_path
            controls[name] = browse
            field_states[name] = state
            content.add_subview(status)
            content.add_subview(browse)

    error_label = ui.Label("")
    error_label.number_of_lines = 2
    error_label.text_color = getattr(ui, "COLOR_SYSTEM_RED", ui.COLOR_LABEL)
    error_label.frame = (14, view.height - 92, width - 28, 36)
    view.add_subview(error_label)

    buttons_top = view.height - 48
    submit = ui.Button(title="Run")
    submit.frame = (width - 132, buttons_top, 118, 38)
    cancel = ui.Button(title="Cancel")
    cancel.frame = (14, buttons_top, 118, 38)
    view.add_subview(submit)
    view.add_subview(cancel)
    result: Dict[str, Any] = {}

    def submit_values(_sender: Any = None) -> None:
        submitted: Dict[str, Any] = {}
        for field in schema:
            name, kind = field["name"], field["type"]
            if kind in ("text", "number"):
                value = controls[name].text or ""
                submitted[name] = None if value == "" and not field["required"] and "default" not in field else value
            elif kind == "choice":
                submitted[name] = field_states[name]["value"]
            else:
                selected = field_states[name]["value"]
                if selected is not None or field["required"]:
                    submitted[name] = selected
                else:
                    submitted[name] = None
        try:
            result["values"] = program_inputs.validate_values(schema, submitted)
        except program_inputs.ProgramInputError as exc:
            error_label.text = str(exc)
            return
        view.close()

    def cancel_form(_sender: Any = None) -> None:
        result["cancelled"] = True
        view.close()

    submit.action = submit_values
    cancel.action = cancel_form
    ui.show_view(view)
    if result.get("cancelled") or "values" not in result:
        raise program_inputs.InputsCancelled("program input form cancelled")
    return result["values"]


def handle_program_command(
    prompt: str,
    *,
    options_factory: Callable[[SessionLog], LoopOptions],
    session: SessionLog,
    printer: Printer,
    options: Optional[LoopOptions] = None,
    input_collector: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> bool:
    """Handle /programs, /run and /edit locally; return False for normal model prompts."""
    command = programs.parse_command(prompt)
    if command is None:
        return False
    workspace = _library_workspace(options_factory)
    if workspace is None:
        printer.write("The saved-program library is unavailable in this front end.")
        return True
    action = command["action"]
    if action == "list":
        try:
            printer.write(scrub_secrets(programs.render_listing(workspace)))
        except programs.ProgramLibraryError as exc:
            printer.write("Saved-program library error: {}".format(exc))
        return True

    identifier = command.get("identifier", "")
    if not identifier:
        syntax = "/run ID" if action == "run" else "/edit ID <requested change>"
        printer.write("Use {}. Run /programs to see saved program ids.".format(syntax))
        return True
    try:
        record = programs.find_program(workspace, identifier)
    except programs.ProgramLibraryError as exc:
        printer.write("{}".format(exc))
        return True

    if action == "edit":
        requested_change = command.get("request", "")
        if not requested_change:
            printer.write("Describe the change: /edit {} <requested change>".format(record["id"]))
            return True
        current_options = options or options_factory(session)
        edit_prompt = programs.build_edit_prompt(record, requested_change, workspace=workspace)
        printer.write("Editing saved program {!r} ({})…".format(record["title"], record["entry_file"]))
        run_turn_sync(current_options, edit_prompt, printer)
        return True

    if not record.get("entry_exists"):
        printer.write(
            "The entry file {} is missing. Restore it, or update this record with register_program using id {} and its new workspace-relative path.".format(
                record["entry_file"], record["id"]
            )
        )
        return True
    current_options = options or options_factory(session)
    registry = getattr(current_options, "registry", None)
    if registry is None:
        printer.write("The saved program runner is unavailable in this front end.")
        return True
    input_values = None
    if record.get("input_schema"):
        if input_collector is None:
            printer.write("This program needs inputs. Run it from the chat form or terminal prompt.")
            return True
        try:
            input_values = input_collector(record)
        except program_inputs.InputsCancelled:
            printer.write("Input cancelled. The program was not run.")
            return True
        except program_inputs.ProgramInputError as exc:
            printer.write("Could not collect program inputs: {}".format(exc))
            return True
    printer.write("Running saved program {!r} ({})…".format(record["title"], record["entry_file"]))
    try:
        result = programs.execute_saved(registry, record, input_values=input_values)
    except programs.ProgramLibraryError as exc:
        printer.write("Could not run saved program: {}".format(exc))
        return True
    printer.write(scrub_secrets(result.content))
    return True


def terminal_repl(
    *,
    options_factory: Callable[[SessionLog], LoopOptions],
    session: SessionLog,
    printer: Optional[Printer] = None,
    input_fn: Callable[[str], str] = input,
    banner: str = "",
) -> int:
    """Plain terminal chat.  EOF or ``/quit`` ends it; ``/exit`` too."""
    printer = printer or Printer(verbose=False)
    if banner:
        printer.write(banner)
    printer.write("Type your request. /programs, /run ID, /edit ID <change>, /session, or /quit.")
    while True:
        try:
            prompt = input_fn("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            printer.write("\nbye")
            return 0
        if not prompt:
            continue
        if prompt in ("/quit", "/exit", "/q"):
            printer.write("bye")
            return 0
        if prompt == "/session":
            printer.write("session: {}".format(session.path))
            continue
        options = options_factory(session)
        if handle_program_command(
            prompt,
            options_factory=options_factory,
            session=session,
            printer=printer,
            options=options,
            input_collector=lambda record: program_inputs.collect_terminal_values(
                record.get("input_schema", []), input_fn=input_fn
            ),
        ):
            continue
        printer.write("")
        try:
            run_turn_sync(options, prompt, printer)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            printer.handle(Event("error", {"code": "CANCELLED", "message": "cancelled"}))
            if options.client is not None:
                options.client.cancel()


# --------------------------------------------------------------------------------------
# Pyto UI
# --------------------------------------------------------------------------------------

UI_HEADER = "pyto-harness"


def run_ui(
    *,
    options_factory: Callable[[SessionLog], LoopOptions],
    session: SessionLog,
    title: str = UI_HEADER,
    verbose: bool = False,
    approver: Optional[UIApprover] = None,
) -> None:
    """Launch the chat view.  Raises :class:`UnsupportedCapability` when pyto_ui is absent."""
    ui = pyto_ui_module()
    if ui is None:
        raise UnsupportedCapability(
            "pyto_ui is not importable here, so the Pyto window cannot be created. "
            "Run without --ui for the terminal REPL, or run this file from the Pyto app."
        )
    view = ui.View()
    view.title = title
    view.background_color = ui.COLOR_SYSTEM_BACKGROUND

    transcript = ui.TextView()
    transcript.text = ""
    transcript.editable = False
    transcript.font = ui.Font("Menlo", 13)
    transcript.background_color = ui.COLOR_SECONDARY_SYSTEM_BACKGROUND
    transcript.text_color = ui.COLOR_LABEL

    history_button = ui.Button()
    history_button.title = "History"
    history_button.background_color = ui.COLOR_SECONDARY_SYSTEM_BACKGROUND
    history_button.text_color = ui.COLOR_LABEL

    entry = ui.TextField()
    entry.placeholder = "Ask for an automation..."
    entry.background_color = ui.COLOR_TERTIARY_SYSTEM_BACKGROUND
    entry.text_color = ui.COLOR_LABEL

    send = ui.Button()
    send.title = "Send"
    send.background_color = ui.COLOR_SYSTEM_BLUE
    send.text_color = ui.COLOR_WHITE

    stop = ui.Button()
    stop.title = "Stop"
    stop.text_color = ui.COLOR_WHITE
    stop.enabled = False

    approve = ui.Button()
    approve.title = "Allow"
    approve.background_color = ui.COLOR_SYSTEM_BLUE
    approve.text_color = ui.COLOR_WHITE
    approve.enabled = False

    deny = ui.Button()
    deny.title = "Deny"
    deny.background_color = ui.COLOR_SECONDARY_SYSTEM_BACKGROUND
    deny.text_color = ui.COLOR_LABEL
    deny.enabled = False

    close = ui.Button()
    close.title = "Close"

    programs_button = ui.Button()
    programs_button.title = "Programs"
    programs_button.background_color = ui.COLOR_SECONDARY_SYSTEM_BACKGROUND
    programs_button.text_color = ui.COLOR_LABEL

    view.add_subview(transcript)
    view.add_subview(programs_button)
    view.add_subview(history_button)
    if approver is not None:
        view.add_subview(approve)
        view.add_subview(deny)
    view.add_subview(entry)
    view.add_subview(send)
    view.add_subview(stop)
    view.add_subview(close)

    transcript.frame = (10, 46, max(180, view.width - 20), max(80, view.height - (175 if approver is not None else 130) - 46))
    programs_button.frame = (10, 5, 90, 32)
    history_button.frame = (max(110, view.width - 100), 5, 90, 32)
    approve.frame = (10, view.height - 160, 100, 36)
    deny.frame = (120, view.height - 160, 100, 36)
    entry.frame = (10, view.height - 110, max(100, view.width - 240), 40)
    send.frame = (view.width - 220, view.height - 110, 65, 40)
    stop.frame = (view.width - 145, view.height - 110, 65, 40)
    close.frame = (view.width - 70, view.height - 110, 60, 40)

    _apply_chat_flex(
        ui,
        transcript=transcript,
        programs_button=programs_button,
        history_button=history_button,
        approve=approve,
        deny=deny,
        entry=entry,
        send=send,
        stop=stop,
        close=close,
    )

    output = _UIStream(transcript)
    prior_messages = session.project()
    if prior_messages:
        recent, _has_older = format_history_page(
            prior_messages,
            session_path=str(getattr(session, "path", "") or ""),
        )
        output.seed(recent)
    printer = Printer(verbose=verbose, stream=output)
    lifecycle = _ChatLifecycle()
    approval_state = {"token": None}
    history_state = {"active": False, "offset": 0, "older_available": False}

    def clear_approval_controls() -> None:
        approval_state["token"] = None
        approve.enabled = False
        deny.enabled = False
        approve.action = lambda _sender=None: None
        deny.action = lambda _sender=None: None

    def on_approval(token: int, granted: bool) -> None:
        if approval_state["token"] != token:
            return
        clear_approval_controls()
        printer.write("\nApproval {}.\n".format("allowed" if granted else "denied"))
        output.flush(force=True)
        if approver is not None:
            approver.answer(token, granted)

    def present_approval(request: ApprovalRequest, token: int) -> None:
        printer.write(
            "\n[approval required]\n{}\nChoose Allow or Deny below.\n".format(request.describe())
        )
        output.flush(force=True)

        def update() -> None:
            if approver is not None and approver.current_token() != token:
                return
            approval_state["token"] = token
            approve.action = lambda _sender=None, current=token: on_approval(current, True)
            deny.action = lambda _sender=None, current=token: on_approval(current, False)
            approve.enabled = True
            deny.enabled = True

        if not lifecycle.update_if_open(update) and approver is not None:
            approver.cancel()

    def update_controls(*, busy: bool) -> None:
        def update() -> None:
            _set_chat_controls(
                send,
                stop,
                entry,
                navigation=(programs_button, history_button),
                busy=busy,
            )
            if not busy and history_state["active"] and not history_state["older_available"]:
                programs_button.enabled = False

        lifecycle.update_if_open(update)

    def worker(prompt: str) -> None:
        options: Optional[LoopOptions] = None
        try:
            if approver is not None:
                approver.reset()
            options = options_factory(session)
            # Stop cancels the shared client. Each new turn gets a fresh cancellation
            # event before it can become the active worker.
            options.client.reset_cancel()
            if not lifecycle.attach(options):
                return
            printer.write("\n>>> {}".format(prompt))
            if not handle_program_command(
                prompt,
                options_factory=options_factory,
                session=session,
                printer=printer,
                options=options,
                input_collector=lambda record: collect_program_inputs_ui(ui, record),
            ):
                run_turn_sync(options, prompt, printer)
        except Exception as exc:  # noqa: BLE001 - report failures instead of hiding them
            try:
                printer.write("[error] {}: {}".format(type(exc).__name__, exc))
            except Exception as display_exc:  # surface a broken view through Pyto's console
                print(
                    "chat display failed: {}: {}".format(type(display_exc).__name__, display_exc),
                    file=sys.stderr,
                )
        finally:
            try:
                output.flush(force=True)
                update_controls(busy=False)
            except Exception as exc:  # noqa: BLE001 - keep Pyto UI contract failures visible
                print(
                    "chat controls could not be updated: {}: {}".format(type(exc).__name__, exc),
                    file=sys.stderr,
                )
            finally:
                lifecycle.finish(options)

    def on_send(_sender: Any = None) -> None:
        prompt = (entry.text or "").strip()
        if not prompt:
            return
        if history_state["active"]:
            on_chat()
        entry.text = ""
        clear_approval_controls()
        update_controls(busy=True)
        thread = threading.Thread(target=worker, args=(prompt,), name="pyto-ui-turn", daemon=True)
        try:
            if not lifecycle.start(thread):
                update_controls(busy=False)
        except Exception as exc:  # noqa: BLE001 - thread startup errors are actionable
            lifecycle.finish(None)
            update_controls(busy=False)
            printer.write("Could not start the chat worker: {}: {}".format(type(exc).__name__, exc))

    def on_stop(_sender: Any = None) -> None:
        if lifecycle.request_stop():
            if approver is not None:
                approver.cancel()
            clear_approval_controls()
            lifecycle.update_if_open(lambda: setattr(stop, "title", "Stopping…"))

    def on_close(_sender: Any = None) -> None:
        if approver is not None:
            approver.close()
        view.close()

    def on_programs(_sender: Any = None) -> None:
        workspace = _library_workspace(options_factory)
        if workspace is None:
            printer.write("The saved-program library is unavailable in this front end.")
            return
        try:
            printer.write(scrub_secrets(programs.render_listing(workspace)))
            output.flush(force=True)
        except programs.ProgramLibraryError as exc:
            printer.write("Saved-program library error: {}".format(exc))

    def render_history_page() -> None:
        messages = session.project()
        page, older_available = format_history_page(
            messages,
            offset=history_state["offset"],
            session_path=str(getattr(session, "path", "") or ""),
        )
        history_state["older_available"] = older_available
        output.replace_display(page)
        programs_button.title = "Older" if older_available else "Oldest"
        programs_button.action = on_older
        programs_button.enabled = older_available
        history_button.title = "Chat"
        history_button.action = on_chat

    def on_history(_sender: Any = None) -> None:
        history_state["active"] = True
        history_state["offset"] = 0
        render_history_page()

    def on_older(_sender: Any = None) -> None:
        if not history_state["older_available"]:
            return
        history_state["offset"] += UI_HISTORY_PAGE_MESSAGES
        render_history_page()

    def on_chat(_sender: Any = None) -> None:
        history_state["active"] = False
        history_state["offset"] = 0
        output.restore_display()
        programs_button.title = "Programs"
        programs_button.action = on_programs
        programs_button.enabled = True
        history_button.title = "History"
        history_button.action = on_history

    # Register every callback before presentation so a fast first tap cannot race setup.
    send.action = on_send
    entry.action = on_send
    stop.action = on_stop
    close.action = on_close
    programs_button.action = on_programs
    history_button.action = on_history
    if approver is not None:
        approver.set_presenter(present_approval)
    printer.write("pyto-harness ready. Session: {}. Tap Programs to browse saved tools.".format(getattr(session, "path", None) or "<memory>"))
    output.flush(force=True)
    sys.stdout.flush()

    try:
        # The documented API blocks this Python script until dismissal. Model calls run
        # on the dedicated worker, and this wrapper does not call UIKit directly.
        ui.show_view(view)
    finally:
        worker_thread, active_options = lifecycle.close()
        if approver is not None:
            approver.close()
        # Quiesce output before cancellation wakes a provider worker with its final error
        # event. No assignment can reach the dismissed TextView after this barrier.
        output.close()
        try:
            lifecycle.cancel(active_options)
        finally:
            if worker_thread is not None and worker_thread is not threading.current_thread():
                # run.py closes the session and model client only after this returns.
                # Drain the worker so no final event can write to closed resources.
                worker_thread.join()


class _ChatLifecycle:
    """Serialize chat turns and make stop/close safe against worker callbacks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._busy = False
        self._stop_requested = False
        self._options: Optional[LoopOptions] = None
        self._worker: Optional[threading.Thread] = None

    def start(self, worker: threading.Thread) -> bool:
        """Reserve and start one worker atomically with respect to close."""
        with self._lock:
            if self._closed or self._busy:
                return False
            self._busy = True
            self._stop_requested = False
            self._worker = worker
            try:
                worker.start()
            except BaseException:
                self._busy = False
                self._worker = None
                raise
            return True

    def attach(self, options: LoopOptions) -> bool:
        """Register resources, cancelling immediately if stop/close already won."""
        if options.stop is None:
            options.stop = threading.Event()
        with self._lock:
            closed = self._closed
            stop_requested = self._stop_requested
            if not closed:
                self._options = options
            if closed or stop_requested:
                options.stop.set()
        if closed or stop_requested:
            options.client.cancel()
        return not closed

    def request_stop(self) -> bool:
        with self._lock:
            if self._closed or not self._busy:
                return False
            self._stop_requested = True
            options = self._options
            if options is not None and options.stop is not None:
                options.stop.set()
        if options is not None:
            options.client.cancel()
        return True

    def finish(self, options: Optional[LoopOptions]) -> None:
        with self._lock:
            if options is None or self._options is options or self._options is None:
                self._options = None
                self._busy = False
                self._stop_requested = False
                self._worker = None

    def close(self) -> tuple:
        """Mark the window closed and return its worker/resources for teardown."""
        with self._lock:
            self._closed = True
            self._stop_requested = True
            options = self._options
            worker = self._worker
        return worker, options

    def cancel(self, options: Optional[LoopOptions]) -> None:
        if options is not None:
            if options.stop is not None:
                options.stop.set()
            options.client.cancel()

    def update_if_open(self, callback: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
            callback()
            return True


def _apply_chat_flex(ui: Any, **views: Any) -> None:
    """Apply PytoUI's documented autoresizing flags where this Pyto version exposes them."""
    flags = {
        "transcript": ("FLEXIBLE_WIDTH", "FLEXIBLE_HEIGHT"),
        "programs_button": ("FLEXIBLE_RIGHT_MARGIN", "FLEXIBLE_BOTTOM_MARGIN"),
        "history_button": ("FLEXIBLE_LEFT_MARGIN", "FLEXIBLE_BOTTOM_MARGIN"),
        "approve": ("FLEXIBLE_RIGHT_MARGIN", "FLEXIBLE_TOP_MARGIN"),
        "deny": ("FLEXIBLE_RIGHT_MARGIN", "FLEXIBLE_TOP_MARGIN"),
        "entry": ("FLEXIBLE_WIDTH", "FLEXIBLE_TOP_MARGIN"),
        "send": ("FLEXIBLE_LEFT_MARGIN", "FLEXIBLE_TOP_MARGIN"),
        "stop": ("FLEXIBLE_LEFT_MARGIN", "FLEXIBLE_TOP_MARGIN"),
        "close": ("FLEXIBLE_LEFT_MARGIN", "FLEXIBLE_TOP_MARGIN"),
    }
    for name, view in views.items():
        values = [getattr(ui, flag, None) for flag in flags.get(name, ())]
        values = [value for value in values if value is not None]
        if values:
            view.flex = values


def _set_chat_controls(
    send: Any,
    stop: Any,
    entry: Any,
    *,
    busy: bool,
    navigation: Any = (),
) -> None:
    send.enabled = not busy
    stop.enabled = busy
    stop.title = "Stop"
    entry.enabled = not busy
    for button in navigation:
        button.enabled = not busy


class _UIStream:
    """File-like output for a Pyto ``TextView``; writes stop when the view closes.

    Pyto documents that ``show_view`` permits another thread to modify its PytoUI views.
    Keep the lock while assigning ``TextView.text`` so ``close`` forms a barrier: once it
    returns, no earlier or later worker write can touch the dismissed view.
    """

    def __init__(self, view: Any) -> None:
        self._view = view
        self._pending: List[str] = []
        self._pending_chars = 0
        self._lock = threading.Lock()
        self._text = ""
        self._display_override: Optional[str] = None
        self._closed = False
        self._last_flush = time.monotonic()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            if self._closed:
                return len(text)
            self._pending.append(text)
            self._pending_chars += len(text)
            self._flush_if_due_locked()
        return len(text)

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._closed:
                if force:
                    self._flush_locked()
                else:
                    self._flush_if_due_locked()

    def close(self) -> None:
        """Prevent further view writes and wait for any current assignment to finish."""
        with self._lock:
            self._closed = True
            self._pending = []
            self._pending_chars = 0

    def seed(self, text: str) -> None:
        """Set initial chat content before presentation, bounded like later output."""
        with self._lock:
            if self._closed:
                return
            self._text = str(text or "")[-TRANSCRIPT_LIMIT_CHARS:]
            self._view.text = self._text
            self._last_flush = time.monotonic()

    def replace_display(self, text: str) -> None:
        """Temporarily show a history page without changing the chat transcript buffer."""
        with self._lock:
            if self._closed:
                return
            self._display_override = str(text or "")[-TRANSCRIPT_LIMIT_CHARS:]
            self._view.text = self._display_override

    def restore_display(self) -> None:
        """Return to the bounded chat buffer after the user closes the history page."""
        with self._lock:
            if self._closed:
                return
            self._display_override = None
            self._view.text = self._text

    def _flush_if_due_locked(self) -> None:
        if not self._pending:
            return
        if (
            self._pending_chars >= UI_STREAM_FLUSH_CHARS
            or time.monotonic() - self._last_flush >= UI_STREAM_FLUSH_INTERVAL_SECONDS
        ):
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending = []
        self._pending_chars = 0
        if len(text) >= TRANSCRIPT_LIMIT_CHARS:
            self._text = text[-TRANSCRIPT_LIMIT_CHARS:]
        else:
            self._text = (self._text + text)[-TRANSCRIPT_LIMIT_CHARS:]
        self._last_flush = time.monotonic()
        # Do not suppress errors here. A broken Pyto view update must reach the worker's
        # error path instead of silently leaving the chat frozen or stale.
        if self._display_override is None:
            self._view.text = self._text

    def isatty(self) -> bool:
        return False


# --------------------------------------------------------------------------------------
# Session helpers shared by the front ends
# --------------------------------------------------------------------------------------


def open_session(config: Any, *, resume: Optional[str] = None, label: str = "chat") -> SessionLog:
    """Resume a named session file, or start a new one in the configured sessions dir."""
    if resume:
        path = expand_user_path(resume, what="--resume path")
        if os.path.isdir(path):
            candidates = sorted(
                (os.path.join(path, name) for name in os.listdir(path) if name.endswith(".jsonl")),
                key=lambda p: os.path.getmtime(p),
            )
            if not candidates:
                raise FileNotFoundError("no .jsonl session files in {}".format(path))
            path = candidates[-1]
        return SessionLog.open_or_create(path)
    return SessionLog.create(
        new_session_path(config.sessions_dir, label=label),
        workspace=config.workspace,
        config={"model": config.model, "api_base": config.api_base},
    )

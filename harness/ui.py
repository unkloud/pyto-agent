"""Shared front-end helpers for browser and terminal sessions.

The web controller and terminal REPL use the same event renderer, approval queue,
session-history formatter, and synchronous turn driver.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .home import expand_user_path
from .loop import ApprovalRequest, Event, LoopOptions, run_turn
from . import program_inputs, programs
from .session import SessionLog, new_session_path
from .security import scrub_secrets, scrub_value

HISTORY_PAGE_MESSAGES = 12
HISTORY_MESSAGE_CHARS = 1800


def format_history_page(
    messages: Any,
    *,
    offset: int = 0,
    page_size: int = HISTORY_PAGE_MESSAGES,
    session_path: str = "",
) -> tuple[str, bool]:
    """Format one bounded page of durable session history for the browser."""
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
        if len(body) > HISTORY_MESSAGE_CHARS:
            first = HISTORY_MESSAGE_CHARS * 2 // 3
            last = HISTORY_MESSAGE_CHARS - first
            omitted = len(body) - HISTORY_MESSAGE_CHARS
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
        self._last_message: Optional[bytes] = None
        self._denied_ids = set()

    def write(self, text: str, *, newline: bool = True) -> None:
        self.stream.write(text + ("\n" if newline else ""))
        self.stream.flush()

    def _write_assistant(self, text: str) -> None:
        """Write assistant prose, allowing a front end to format it separately."""
        formatter = getattr(self.stream, "write_assistant", None)
        if callable(formatter):
            formatter(text)
        else:
            self.write(text)

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
            if self.verbose and reasoning:
                self.write("[reasoning]")
                for line in reasoning.splitlines():
                    self.write("  {}".format(line))
            if content:
                # Display the scrubbed, complete body once for both provider modes.
                self._write_assistant(content)
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
                self._write_assistant(message)
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
    """Queue policy prompts for explicit answers in an attached browser session."""

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
            printer.write("This program needs inputs. Run it from the browser form or terminal prompt.")
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

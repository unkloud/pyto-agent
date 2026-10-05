"""Front ends: the Pyto UI when it exists, otherwise a terminal REPL.

The UI is deliberately small.  The pattern that matters is not the layout — it is that
**the network never runs on the UI thread**.  A Pyto button handler that calls a model
endpoint freezes the app until the response lands, which on a phone means the watchdog
kills it.  So the handler starts a plain ``threading.Thread`` and the thread pushes
events through Pyto's documented ``pyto_ui`` wrappers, which can be modified from a
worker thread. Direct UIKit calls have separate main-thread requirements and are not used here.

If ``pyto_ui`` is missing (Linux, a test, a plain Pyto console session) the caller gets
:func:`terminal_repl` instead.  :func:`run_ui` raises :class:`UnsupportedCapability` in
that case so ``run.py --ui`` can explain itself rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import UnsupportedCapability
from .home import expand_user_path
from .loop import Event, LoopOptions, make_policy, run_turn
from .session import SessionLog, new_session_path

TRANSCRIPT_LIMIT_CHARS = 200_000


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
        self.lines.append(text)
        self._trim()

    def append_delta(self, text: str) -> None:
        self._partial += text

    def flush_delta(self, prefix: str = "") -> None:
        if self._partial:
            self.add(("{}{}".format(prefix, self._partial)).rstrip())
        self._partial = ""

    def _trim(self) -> None:
        total = sum(len(line) for line in self.lines)
        while total > TRANSCRIPT_LIMIT_CHARS and len(self.lines) > 1:
            total -= len(self.lines.pop(0))

    def render(self) -> str:
        body = "\n".join(self.lines)
        if self._partial:
            body += ("\n" if body else "") + self._partial
        return body


# --------------------------------------------------------------------------------------
# Event -> text formatting (shared by both front ends)
# --------------------------------------------------------------------------------------


class Printer:
    """Turns loop events into user-visible text, in one place for both front ends."""

    def __init__(self, *, verbose: bool = False, stream: Any = None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.transcript = Transcript()
        self._open_line = False

    def write(self, text: str, *, newline: bool = True) -> None:
        self.stream.write(text + ("\n" if newline else ""))
        self.stream.flush()

    def handle(self, event: Event) -> None:
        kind = event.kind
        data = event.data
        if kind == "delta":
            self.transcript.append_delta(data.get("text", ""))
            self.write(data.get("text", ""), newline=False)
            self._open_line = True
        elif kind == "reasoning.delta":
            if self.verbose:
                self.write(data.get("text", ""), newline=False)
                self._open_line = True
        elif kind == "message.completed":
            if self._open_line:
                self.write("")
                self._open_line = False
            content = (data.get("content") or "").strip()
            if content:
                self.transcript.add(content)
        elif kind == "tool.started":
            self.write("\n  -> {}({})".format(data.get("name"), _brief_args(data.get("arguments"))))
        elif kind == "tool.completed":
            marker = "!!" if data.get("is_error") else "ok"
            self.write("  <- [{}] {} ({:.0f} ms)".format(marker, data.get("name"), data.get("duration_ms") or 0))
            body = (data.get("content") or "").strip()
            if body:
                for line in body.splitlines():
                    self.write("     | {}".format(line))
        elif kind == "tool.denied":
            self.write("  xx {} denied: {}".format(data.get("name"), data.get("reason")))
        elif kind == "finished":
            self.write("\n{}".format(data.get("message", "")))
            self.transcript.add(str(data.get("message", "")))
        elif kind == "turn.limit":
            self.write("\n[turn limit reached: {}]".format(data.get("limit")))
        elif kind == "error":
            self.write("\n[error] {}".format(data.get("message")))
        elif kind == "usage" and self.verbose:
            self.write("[usage] {}".format(data))
        elif kind == "turn.started" and self.verbose:
            self.write("[turn {}]".format(data.get("turn")))


def _brief_args(arguments: Any, limit: int = 120) -> str:
    """The one-line status summary shown *before* the approval prompt.

    Deliberately short — the prompt itself (``ApprovalRequest.describe``) is what the user
    decides on, and that one shows the whole value up to a few thousand characters.  A cut
    here is still marked explicitly rather than trailing off into a bare "...".
    """
    if not isinstance(arguments, dict):
        return ""
    parts = []
    for key, value in arguments.items():
        rendered = repr(value)
        if len(rendered) > 60:
            rendered = "{}...(+{} chars)".format(rendered[:40], len(rendered) - 40)
        parts.append("{}={}".format(key, rendered))
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 3] + "..."


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
    printer.write("Type your request. /quit to exit, /session to see the log path.")
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
        printer.write("")
        try:
            run_turn_sync(options, prompt, printer)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            printer.write("\n[interrupted]")
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

    close = ui.Button()
    close.title = "Close"

    view.add_subview(transcript)
    view.add_subview(entry)
    view.add_subview(send)
    view.add_subview(stop)
    view.add_subview(close)

    transcript.frame = (0, 0, view.width, max(200, view.height - 130))
    entry.frame = (10, view.height - 110, max(100, view.width - 240), 40)
    send.frame = (view.width - 220, view.height - 110, 65, 40)
    stop.frame = (view.width - 145, view.height - 110, 65, 40)
    close.frame = (view.width - 70, view.height - 110, 60, 40)

    output = _UIStream(transcript)
    printer = Printer(verbose=verbose, stream=output)
    lifecycle = _ChatLifecycle()

    def update_controls(*, busy: bool) -> None:
        lifecycle.update_if_open(lambda: _set_chat_controls(send, stop, entry, busy=busy))

    def worker(prompt: str) -> None:
        options: Optional[LoopOptions] = None
        try:
            options = options_factory(session)
            # Stop cancels the shared client. Each new turn gets a fresh cancellation
            # event before it can become the active worker.
            options.client.reset_cancel()
            if not lifecycle.attach(options):
                return
            printer.write("\n>>> {}".format(prompt))
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
        entry.text = ""
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
            lifecycle.update_if_open(lambda: setattr(stop, "title", "Stopping…"))

    def on_close(_sender: Any = None) -> None:
        view.close()

    # Register every callback before presentation so a fast first tap cannot race setup.
    send.action = on_send
    entry.action = on_send
    stop.action = on_stop
    close.action = on_close
    printer.write("pyto-harness ready. Session: {}".format(getattr(session, "path", None) or "<memory>"))
    sys.stdout.flush()

    try:
        # The documented API blocks this Python script until dismissal. Model calls run
        # on the dedicated worker, and this wrapper does not call UIKit directly.
        ui.show_view(view)
    finally:
        worker_thread, active_options = lifecycle.close()
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


def _set_chat_controls(send: Any, stop: Any, entry: Any, *, busy: bool) -> None:
    send.enabled = not busy
    stop.enabled = busy
    stop.title = "Stop"
    entry.enabled = not busy


class _UIStream:
    """File-like output for a Pyto ``TextView``; writes stop when the view closes.

    Pyto documents that ``show_view`` permits another thread to modify its PytoUI views.
    Keep the lock while assigning ``TextView.text`` so ``close`` forms a barrier: once it
    returns, no earlier or later worker write can touch the dismissed view.
    """

    def __init__(self, view: Any) -> None:
        self._view = view
        self._pending: List[str] = []
        self._lock = threading.Lock()
        self._text = ""
        self._closed = False

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            if self._closed:
                return len(text)
            self._pending.append(text)
            self._flush_locked()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                self._flush_locked()

    def close(self) -> None:
        """Prevent further view writes and wait for any current assignment to finish."""
        with self._lock:
            self._closed = True
            self._pending = []

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending = []
        self._text = (self._text + text)[-TRANSCRIPT_LIMIT_CHARS:]
        # Do not suppress errors here. A broken Pyto view update must reach the worker's
        # error path instead of silently leaving the chat frozen or stale.
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

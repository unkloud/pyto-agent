"""Front ends: the Pyto UI when it exists, otherwise a terminal REPL.

The UI is deliberately small.  The pattern that matters is not the layout — it is that
**the network never runs on the UI thread**.  A Pyto button handler that calls a model
endpoint freezes the app until the response lands, which on a phone means the watchdog
kills it.  So the handler starts a plain ``threading.Thread`` and the thread pushes
events back with ``pyto_ui.main_thread``, which is the one documented way to touch views
from off the main thread.

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

    view.add_subview(transcript)
    view.add_subview(entry)
    view.add_subview(send)

    transcript.frame = (0, 0, view.width, max(200, view.height - 120))
    entry.frame = (10, view.height - 110, max(120, view.width - 110), 40)
    send.frame = (view.width - 90, view.height - 110, 80, 40)
    view.present("fullscreen")

    printer = Printer(stream=_UIStream(ui, transcript))
    busy = threading.Event()

    def worker(prompt: str) -> None:
        try:
            printer.write("\n>>> {}".format(prompt))
            options = options_factory(session)
            run_turn_sync(options, prompt, printer)
        except Exception as exc:  # noqa: BLE001 - a UI thread must never die silently
            printer.write("\n[error] {}: {}".format(type(exc).__name__, exc))
        finally:
            busy.clear()
            _on_main(ui, lambda: setattr(send, "enabled", True))

    def on_send(_sender: Any = None) -> None:
        prompt = (entry.text or "").strip()
        if not prompt or busy.is_set():
            return
        entry.text = ""
        busy.set()
        send.enabled = False
        # The network call happens HERE, off the UI thread: a frozen UI thread on iOS
        # gets the app killed by the watchdog long before the model answers.
        threading.Thread(target=worker, args=(prompt,), name="pyto-ui-turn", daemon=True).start()

    send.action = on_send
    entry.action = on_send
    printer.write("pyto-harness ready. Session: {}".format(session.path or "<memory>"))
    sys.stdout.flush()


def _on_main(ui: Any, callback: Callable[[], None]) -> None:
    """Run ``callback`` on the UI thread, tolerating a build without ``main_thread``."""
    main_thread = getattr(ui, "main_thread", None)
    if callable(main_thread):
        try:
            main_thread(callback)
            return
        except Exception:  # noqa: BLE001 - fall through to a direct call
            pass
    try:
        callback()
    except Exception:  # pragma: no cover - nothing sensible left to do
        pass


class _UIStream:
    """File-like object that appends to a Pyto ``TextView`` from any thread."""

    def __init__(self, ui: Any, view: Any) -> None:
        self._ui = ui
        self._view = view
        self._pending: List[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            self._pending.append(text)
        _on_main(self._ui, self._flush)
        return len(text)

    def flush(self) -> None:
        _on_main(self._ui, self._flush)

    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            text = "".join(self._pending)
            self._pending = []
        try:
            self._view.text = (self._view.text or "") + text
        except Exception:  # pragma: no cover - the view may be gone
            pass

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

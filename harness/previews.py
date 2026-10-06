"""Validation and lifecycle support for long-lived PytoUI previews.

An interactive view has a different lifetime from a batch program: Pyto's documented
``pyto_ui.show_view`` call blocks until the user closes the view.  This module keeps that
work in the shared-process execution lane until the view and any program-owned threads
have finished.  It is concurrency control, not a sandbox or a hard thread-kill mechanism.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import os
import runpy
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from . import unix_tools
from .errors import ToolError
from .security import temporary_environ_scrub

MAX_PREVIEW_SOURCE_BYTES = 512 * 1024
PREVIEW_STARTUP_WAIT_S = 15.0
PREVIEW_CLOSE_GRACE_S = 2.0
PREVIEW_OUTPUT_CHARS = 20000


class _TextSink:
    """Thread-safe bounded text capture for preview startup and callbacks."""

    def __init__(self, limit: int = PREVIEW_OUTPUT_CHARS) -> None:
        self._limit = limit
        self._chunks: List[str] = []
        self._kept = 0
        self._dropped = 0
        self._lock = threading.Lock()

    def write(self, value: Any) -> int:
        text = value if isinstance(value, str) else str(value)
        with self._lock:
            room = self._limit - self._kept
            if room > 0:
                kept = text[:room]
                self._chunks.append(kept)
                self._kept += len(kept)
            if len(text) > room:
                self._dropped += len(text) - max(0, room)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def value(self) -> str:
        with self._lock:
            result = "".join(self._chunks)
            if self._dropped:
                result += "\n... [{} more characters]".format(self._dropped)
            return result


class PreviewMonitor:
    """Small API injected into an app so its lifecycle is observable and safe to close.

    Generated preview scripts should wrap event callbacks with :meth:`guard`, report
    failures from background jobs with :meth:`report_error`, and call :meth:`present`
    exactly once for the root PytoUI view.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.presentation_requested = threading.Event()
        self.stop_event = threading.Event()
        self._opened = False
        self._closed = False
        self._stop_requested = False
        self._startup_aborted = False
        self._close_reason = ""
        self._callback_attempts = 0
        self._callback_successes = 0
        self._callback_errors: List[str] = []
        self._view: Any = None

    def guard(
        self,
        callback: Callable[..., Any],
        *,
        on_error: Optional[Callable[[Exception], Any]] = None,
        label: str = "callback",
    ) -> Callable[..., Any]:
        """Return a callback wrapper that records interactions and displays failures."""

        @functools.wraps(callback)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self._callback_attempts += 1
            try:
                value = callback(*args, **kwargs)
            except Exception as exc:  # callback errors must not vanish on Pyto's console
                self.report_error(exc, label=label)
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception as display_exc:  # keep a broken error panel visible too
                        self.report_error(display_exc, label="error display")
                return None
            with self._lock:
                self._callback_successes += 1
            return value

        return guarded

    def report_error(self, error: Any, *, label: str = "background work") -> None:
        """Record a callback or background error for the preview result."""
        if isinstance(error, BaseException):
            message = "{}: {}".format(type(error).__name__, error)
        else:
            message = str(error)
        with self._lock:
            self._callback_errors.append("{} — {}".format(label, message))

    def close(self, view: Any = None, *, reason: str = "close action") -> None:
        """Signal owned background work to stop and close the view."""
        with self._lock:
            self._stop_requested = True
            self._close_reason = str(reason)
            target = view if view is not None else self._view
        self.stop_event.set()
        if target is None:
            raise RuntimeError("no preview view is available to close")
        target.close()

    def abort_startup(self) -> None:
        """Prevent a late top-level program from presenting after startup was reported stuck."""
        with self._lock:
            self._startup_aborted = True
            self._stop_requested = True
            self._close_reason = "startup wait expired"
        self.stop_event.set()

    def present(self, view: Any, ui: Any, mode: Any = None) -> None:
        """Present the root view and wait until Pyto reports that it has closed."""
        with self._lock:
            if self._view is not None:
                raise RuntimeError("a preview can present only one root view")
            if self._startup_aborted:
                raise RuntimeError("preview startup expired before the view could be presented")
            self._view = view
        self.presentation_requested.set()
        try:
            if mode is None:
                ui.show_view(view)
            else:
                ui.show_view(view, mode)
            with self._lock:
                # Pyto's documented show_view call returns after dismissal. A normal
                # return therefore proves that this preview was presented and closed.
                self._opened = True
        finally:
            with self._lock:
                self._closed = True
                if not self._close_reason:
                    self._close_reason = "view dismissed"
            self.stop_event.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            errors = list(self._callback_errors)
            return {
                "presentation_requested": self.presentation_requested.is_set(),
                "preview_opened": self._opened,
                "preview_closed": self._closed,
                "stop_requested": self._stop_requested,
                "close_reason": self._close_reason,
                "callback_attempts": self._callback_attempts,
                "callback_successes": self._callback_successes,
                "interaction_verified": self._callback_successes > 0,
                "callback_errors": errors,
            }


def _local_module_exists(module: str, roots: Sequence[str]) -> bool:
    parts = module.split(".")
    for root in roots:
        base = os.path.join(root, *parts)
        if os.path.isfile(base + ".py") or os.path.isfile(os.path.join(base, "__init__.py")):
            return True
    return False


def _import_top_level_exists(module: str, roots: Sequence[str]) -> bool:
    top = module.split(".", 1)[0]
    if _local_module_exists(top, roots):
        return True
    if top in sys.modules:
        return True
    if top in getattr(sys, "builtin_module_names", ()):
        return True
    try:
        # find_spec asks import finders for availability; unlike import_module it does
        # not execute the target module or the user's program.
        return importlib.util.find_spec(top) is not None
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return False


def validate_source(source: str, *, path: str, workspace_root: str) -> Dict[str, Any]:
    """Compile source and check static top-level imports without running user code."""
    encoded_size = len(source.encode("utf-8"))
    if encoded_size > MAX_PREVIEW_SOURCE_BYTES:
        return {
            "passed": False,
            "checked": True,
            "syntax_passed": False,
            "imports_passed": False,
            "errors": [
                "Program is {} bytes; interactive previews are limited to {} bytes.".format(
                    encoded_size, MAX_PREVIEW_SOURCE_BYTES
                )
            ],
            "imports": [],
            "relative_imports": [],
        }
    try:
        tree = ast.parse(source, filename=path)
        compile(tree, path, "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        if isinstance(exc, SyntaxError):
            detail = "SyntaxError at line {}, column {}: {}".format(
                exc.lineno or "?", exc.offset or "?", exc.msg
            )
        else:
            detail = "{}: {}".format(type(exc).__name__, exc)
        return {
            "passed": False,
            "checked": True,
            "syntax_passed": False,
            "imports_passed": False,
            "errors": [detail],
            "imports": [],
            "relative_imports": [],
        }

    roots = list(dict.fromkeys((os.path.dirname(os.path.realpath(path)), os.path.realpath(workspace_root))))
    imports: Set[str] = set()
    relative_imports: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_imports.add("." * node.level + (node.module or ""))
            elif node.module:
                imports.add(node.module)

    missing = sorted(module for module in imports if not _import_top_level_exists(module, roots))
    errors = []
    if missing:
        errors.append(
            "Could not resolve top-level import module(s): {}. Check this Pyto build with pyto_api or "
            "python_module_capabilities before launching.".format(", ".join(missing))
        )
    return {
        "passed": not errors,
        "checked": True,
        "syntax_passed": True,
        "imports_passed": not missing,
        "errors": errors,
        "imports": sorted(imports),
        "relative_imports": sorted(relative_imports),
        "import_check_scope": "static top-level module availability; import members and dynamic imports are checked at runtime",
    }


def _program_threads(baseline: Set[int], current_ident: Optional[int]) -> List[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.ident is not None
        and thread.ident != current_ident
        and thread.ident not in baseline
        and thread.is_alive()
    ]


def run_preview(
    target: str,
    *,
    workspace_root: str,
    args: Sequence[str] = (),
    input_values: Optional[Mapping[str, Any]] = None,
    startup_wait_s: float = PREVIEW_STARTUP_WAIT_S,
    close_grace_s: float = PREVIEW_CLOSE_GRACE_S,
) -> Dict[str, Any]:
    """Validate and present one PytoUI app without applying the batch-run timeout.

    The call returns after ``show_view`` closes, or after a bounded close-cleanup grace
    proves that a program-owned thread is still active. A surviving worker retains the
    process lease and the saved process state until it exits.
    """
    started = time.monotonic()
    monitor = PreviewMonitor()
    stdout_sink = _TextSink()
    stderr_sink = _TextSink()
    outcome: Dict[str, Any] = {}
    try:
        lease = unix_tools.IN_PROCESS_EXECUTION_LANE.acquire("interactive preview")
    except ToolError as exc:
        return {
            "is_error": True,
            "validation": {"passed": False, "checked": False, "errors": [exc.message]},
            "monitor": monitor.snapshot(),
            "stdout": "",
            "stderr": "",
            "duration_s": time.monotonic() - started,
            "execution_lane_busy": True,
            "cleanup_pending": False,
            "surviving_threads": [],
            "error": exc.message,
        }

    try:
        with open(target, "rb") as handle:
            raw = handle.read(MAX_PREVIEW_SOURCE_BYTES + 1)
        source = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        lease.release()
        return {
            "is_error": True,
            "validation": {
                "passed": False,
                "checked": False,
                "errors": ["Could not read preview source: {}: {}".format(type(exc).__name__, exc)],
            },
            "monitor": monitor.snapshot(),
            "stdout": "",
            "stderr": "",
            "duration_s": time.monotonic() - started,
            "execution_lane_busy": False,
            "cleanup_pending": False,
            "surviving_threads": [],
        }

    validation = validate_source(source, path=target, workspace_root=workspace_root)
    if not validation["passed"]:
        lease.release()
        return {
            "is_error": True,
            "validation": validation,
            "monitor": monitor.snapshot(),
            "stdout": "",
            "stderr": "",
            "duration_s": time.monotonic() - started,
            "execution_lane_busy": False,
            "cleanup_pending": False,
            "surviving_threads": [],
        }

    run_finished = threading.Event()
    baseline_holder: Dict[str, Set[int]] = {}

    def execute() -> None:
        saved_stdout, saved_stderr, saved_argv = sys.stdout, sys.stderr, sys.argv
        saved_trace = sys.gettrace()
        saved_path = list(sys.path)
        saved_environ_object = os.environ
        saved_environ = dict(saved_environ_object)
        original_cwd: Optional[str] = None
        current_ident = threading.current_thread().ident
        try:
            lease.bind_worker(threading.current_thread())
            original_cwd = os.getcwd()
            baseline_holder["ids"] = {
                thread.ident for thread in threading.enumerate() if thread.ident is not None
            }
            sys.stdout, sys.stderr = stdout_sink, stderr_sink
            sys.argv = [target] + [str(arg) for arg in args]
            os.chdir(workspace_root)
            program_dir = os.path.dirname(os.path.realpath(target))
            if program_dir not in sys.path:
                sys.path.insert(0, program_dir)
            with temporary_environ_scrub():
                if input_values is None:
                    runpy.run_path(
                        target,
                        run_name="__main__",
                        init_globals={"harness_preview": monitor},
                    )
                else:
                    namespace = runpy.run_path(
                        target,
                        run_name="pyto_saved_program",
                        init_globals={"harness_preview": monitor},
                    )
                    entry = namespace.get("main")
                    if not callable(entry):
                        raise TypeError("input-enabled app must define main(inputs)")
                    entry(dict(input_values))
            outcome["returncode"] = 0
        except BaseException as exc:  # preview failures become a clear tool result
            import traceback

            traceback.print_exc(file=stderr_sink)
            outcome["returncode"] = 1
            outcome["error"] = "{}: {}".format(type(exc).__name__, exc)
            monitor.report_error(exc, label="startup/preview")
        finally:
            # A view must be dismissed (or startup must fail) before this event is set.
            monitor.stop_event.set()
            run_finished.set()
            baseline = baseline_holder.get("ids", set())
            observed: Set[str] = set()
            while True:
                children = _program_threads(baseline, current_ident)
                if not children:
                    break
                observed.update(thread.name for thread in children)
                for child in children:
                    child.join(timeout=0.05)
            if observed:
                outcome["program_threads"] = sorted(observed)
            try:
                if original_cwd is not None:
                    os.chdir(original_cwd)
            except BaseException as exc:
                outcome["cwd_restore_error"] = "{}: {}".format(type(exc).__name__, exc)
                outcome["returncode"] = 1
            try:
                os.environ = saved_environ_object
                saved_environ_object.clear()
                saved_environ_object.update(saved_environ)
            except BaseException as exc:
                outcome["environment_restore_error"] = "{}: {}".format(type(exc).__name__, exc)
                outcome["returncode"] = 1
            try:
                sys.settrace(saved_trace)
                sys.path[:] = saved_path
            finally:
                try:
                    sys.stdout, sys.stderr, sys.argv = saved_stdout, saved_stderr, saved_argv
                finally:
                    lease.release()

    worker = threading.Thread(target=execute, name="pyto-interactive-preview", daemon=True)
    lease.bind_worker(worker)
    try:
        worker.start()
    except BaseException as exc:
        lease.release()
        return {
            "is_error": True,
            "validation": validation,
            "monitor": monitor.snapshot(),
            "stdout": "",
            "stderr": "",
            "duration_s": time.monotonic() - started,
            "execution_lane_busy": False,
            "cleanup_pending": False,
            "surviving_threads": [],
            "error": "Could not start preview worker: {}: {}".format(type(exc).__name__, exc),
        }

    startup_deadline = time.monotonic() + max(0.01, float(startup_wait_s))
    while not monitor.presentation_requested.is_set() and not run_finished.is_set():
        remaining = startup_deadline - time.monotonic()
        if remaining <= 0:
            break
        monitor.presentation_requested.wait(min(0.05, remaining))
    if monitor.presentation_requested.is_set():
        # No 30-second batch deadline applies after the supported UI call is reached.
        run_finished.wait()
    elif not run_finished.is_set() and worker.is_alive():
        # A short startup bound prevents a bad top-level loop from pinning Pyto forever.
        # Once show_view is requested, the view is intentionally allowed to remain open
        # beyond run_program's batch limit.
        monitor.abort_startup()
        lease.mark_deadline_exceeded("preview startup did not request a view within {:.0f}s".format(startup_wait_s))
        return {
            "is_error": True,
            "validation": validation,
            "monitor": monitor.snapshot(),
            "stdout": stdout_sink.value(),
            "stderr": stderr_sink.value(),
            "duration_s": time.monotonic() - started,
            "execution_lane_busy": True,
            "cleanup_pending": True,
            "surviving_threads": [],
            "error": "Preview startup did not reach harness_preview.present() within {:.0f}s. "
            "The worker could not be stopped safely; wait for it or restart Pyto.".format(startup_wait_s),
        }

    worker.join(timeout=max(0.01, float(close_grace_s)))
    worker_alive = worker.is_alive()
    survivors = _program_threads(baseline_holder.get("ids", set()), worker.ident) if worker_alive else []
    if worker_alive:
        reason = "program-owned thread(s) are still active after the preview runner returned"
        lease.mark_deadline_exceeded(reason)
        outcome["returncode"] = 124
        outcome["cleanup_pending"] = True
        outcome["surviving_threads"] = sorted(set(thread.name for thread in survivors))
    monitor_state = monitor.snapshot()
    no_presentation = not monitor_state["presentation_requested"]
    if no_presentation and not outcome.get("error"):
        outcome["error"] = "Program finished without calling harness_preview.present(view, ui)."
    if monitor_state["callback_errors"] and not outcome.get("error"):
        outcome["error"] = "One or more preview callbacks or background tasks reported an error."
    if outcome.get("cwd_restore_error") or outcome.get("environment_restore_error"):
        outcome.setdefault("error", "Preview process state could not be fully restored.")
    if worker_alive:
        outcome.setdefault("error", "Preview closed, but cleanup is still waiting for program-owned work.")
    return {
        "is_error": bool(outcome.get("returncode", 1) or outcome.get("error")),
        "validation": validation,
        "monitor": monitor_state,
        "stdout": stdout_sink.value(),
        "stderr": stderr_sink.value(),
        "duration_s": time.monotonic() - started,
        "execution_lane_busy": worker_alive,
        "cleanup_pending": worker_alive,
        "surviving_threads": outcome.get("surviving_threads", []),
        "program_threads": outcome.get("program_threads", []),
        "returncode": outcome.get("returncode", 1),
        "error": outcome.get("error", ""),
    }

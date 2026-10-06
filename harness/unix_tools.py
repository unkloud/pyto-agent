"""Small, read-only Unix command support for Pyto and desktop development.

Pyto bundles an ``ios_system`` shell, but its commands run inside the app process.  This
module exposes a deliberately small command set using argv (never model-supplied shell
text), and keeps availability discovery separate from invocation so the agent can check
the installed Pyto build before using a command.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ios
from .errors import ToolError

# Keep to short, read-only commands.  `sed`, `awk`, `find -exec`, `tar -x`, and commands
# with output-file options are intentionally omitted because their command languages can
# write files or invoke another program.
COMMANDS: Tuple[str, ...] = (
    "cat", "cut", "find", "grep", "head", "ls", "sort", "tail", "uniq", "wc",
)
MAX_ARGS = 64
MAX_ARG_CHARS = 4096
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 24000
MAX_LINES = 1000
MAX_FILE_BYTES = 1024 * 1024
MAX_DIRECTORY_ENTRIES = 500
MAX_FIND_ENTRIES = 500
class _ExecutionLease:
    """Ownership token for process-wide work that cannot be safely interrupted."""

    def __init__(self, lane: "InProcessExecutionLane", operation: str) -> None:
        self._lane = lane
        self.operation = operation
        self.started_at = time.monotonic()
        self.worker: Optional[threading.Thread] = None
        self.worker_started = False
        self.deadline_exceeded = False
        self.deadline_reason = ""
        self.released = False

    def bind_worker(self, worker: threading.Thread) -> None:
        self._lane._bind(self, worker)

    def mark_deadline_exceeded(self, reason: str = "the operation exceeded its wait limit") -> None:
        self._lane._mark_deadline_exceeded(self, reason)

    def release(self) -> None:
        self._lane._release(self)


class InProcessExecutionLane:
    """Single owner for Pyto work that shares interpreter or native process state.

    Unlike a lock held by the caller, this lease can outlive a timed-out wait. The
    program worker releases it only after it has restored cwd, streams, argv and env.
    New conflicting operations fail clearly while that worker is still alive.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: Optional[_ExecutionLease] = None

    def _prune_locked(self) -> None:
        # Only the worker that owns the lease releases it. A dead thread reference can
        # briefly be visible while a replacement monitor is being attached.
        return None

    def acquire(self, operation: str) -> _ExecutionLease:
        with self._guard:
            self._prune_locked()
            active = self._active
            if active is not None:
                age = max(0.0, time.monotonic() - active.started_at)
                if active.deadline_exceeded:
                    reason = active.deadline_reason or "it exceeded its wait limit"
                    detail = "{} is still active because {} ({:.1f}s elapsed)".format(
                        active.operation, reason, age
                    )
                else:
                    detail = "{} is still active".format(active.operation)
                raise ToolError(
                    "cannot start {} while {}; wait for it to finish. If it does not finish, "
                    "restart Pyto to restore a clean interpreter".format(operation, detail)
                )
            lease = _ExecutionLease(self, operation)
            self._active = lease
            return lease

    def ensure_idle(self, operation: str) -> None:
        """Refuse workspace reads while a timed-out in-process program may still write."""
        with self._guard:
            self._prune_locked()
            active = self._active
            if active is None:
                return
            if active.deadline_exceeded:
                raise ToolError(
                    "cannot {} while {} is still active because {}; its outcome may still "
                    "change workspace files. Wait for it to finish or restart Pyto".format(
                        operation, active.operation, active.deadline_reason or "it exceeded its wait limit"
                    )
                )

    def _bind(self, lease: _ExecutionLease, worker: threading.Thread) -> None:
        with self._guard:
            if self._active is lease and not lease.released:
                lease.worker = worker
                lease.worker_started = True

    def _mark_deadline_exceeded(self, lease: _ExecutionLease, reason: str) -> None:
        with self._guard:
            if self._active is lease and not lease.released:
                lease.deadline_exceeded = True
                lease.deadline_reason = str(reason)

    def _release(self, lease: _ExecutionLease) -> None:
        with self._guard:
            lease.released = True
            if self._active is lease:
                self._active = None

    def hold(self, operation: str):
        """Return a context manager holding the lane for a synchronous operation."""
        return _ExecutionLaneContext(self, operation)


class _ExecutionLaneContext:
    def __init__(self, lane: InProcessExecutionLane, operation: str) -> None:
        self.lane = lane
        self.operation = operation
        self.lease: Optional[_ExecutionLease] = None

    def __enter__(self) -> _ExecutionLease:
        self.lease = self.lane.acquire(self.operation)
        self.lease.bind_worker(threading.current_thread())
        return self.lease

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.lease is not None:
            self.lease.release()


IN_PROCESS_EXECUTION_LANE = InProcessExecutionLane()

_HELP_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9_-])")
_SHELL_META = re.compile(r"[\x00\r\n]")


def parse_help_output(output: str) -> List[str]:
    """Intersect Pyto's `help` output with the harness's intentionally small allowlist."""
    found = set()
    listing = False
    for line in (output or "").splitlines():
        if re.search(r"\bcommands?\s*:", line, re.I):
            listing = True
            line = re.split(r"\bcommands?\s*:", line, maxsplit=1, flags=re.I)[-1]
        elif listing and not line.strip():
            listing = False
            continue
        words = [word.lower() for word in _HELP_TOKEN.findall(line)]
        matches = set(words).intersection(COMMANDS)
        # Only accept rows after a command heading, a command table row, or a line that
        # consists solely of a comma/space separated command list. Ignore help prose.
        tokens = [token.strip(",;:") for token in line.split()]
        if listing:
            found.update(matches)
        elif len(matches) > 1 and tokens and all(token.lower() in COMMANDS for token in tokens):
            found.update(matches)
        elif len(matches) == 1:
            match = next(iter(matches))
            if line.strip().lower() == match or re.match(
                r"^\s*(?:[-*]\s*)?{}(?:\s{{2,}}|\s+-|\s*:)".format(re.escape(match)), line, re.I
            ):
                found.add(match)
    return [name for name in COMMANDS if name in found]


def discover_commands(workspace: str) -> Dict[str, Any]:
    """Return commands confirmed by Pyto's `help` command or this desktop's PATH."""
    if ios.is_pyto():
        try:
            with IN_PROCESS_EXECUTION_LANE.hold("Pyto command discovery"):
                process = subprocess.Popen(
                    ["help"],
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = process.communicate()
        except Exception as exc:  # noqa: BLE001 - Pyto versions differ in command bridges.
            return {
                "platform": "pyto",
                "commands": [],
                "verified": False,
                "method": "embedded help command",
                "note": "Could not read this Pyto build's command list: {}: {}".format(type(exc).__name__, exc),
            }
        output = _decode(stdout) + "\n" + _decode(stderr)
        found = parse_help_output(output) if getattr(process, "returncode", None) in (0, None) else []
        return {
            "platform": "pyto",
            "commands": found,
            "verified": bool(found),
            "method": "Pyto embedded help command",
            "returncode": getattr(process, "returncode", None),
            "note": (
                "Only read-only commands are listed. Pyto commands run in the app process; "
                "they have no independent process or reliable kill timeout."
                if found
                else "The output did not identify any allowlisted commands; do not guess."
            ),
        }

    found = [name for name in COMMANDS if shutil.which(name)]
    return {
        "platform": "desktop",
        "commands": found,
        "verified": True,
        "method": "PATH lookup",
        "note": "Desktop PATH results do not prove that the same commands exist in Pyto.",
    }


def validate_arguments(command: str, args: Sequence[str]) -> List[str]:
    """Validate a narrow, read-only argument grammar for each supported command."""
    if command not in COMMANDS:
        raise ToolError("command is not in the read-only allowlist; call unix_capabilities")
    if len(args) > MAX_ARGS:
        raise ToolError("too many command arguments (maximum {})".format(MAX_ARGS))
    values = [str(arg) for arg in args]
    for value in values:
        if len(value) > MAX_ARG_CHARS or _SHELL_META.search(value):
            raise ToolError("arguments must be short single-line values without NULs or newlines")

    if command in ("cat", "ls"):
        if command == "cat" and values:
            raise ToolError("cat takes no options here; pass the file in the path field")
        allowed = {"-a", "-l", "-h", "-1"}
        if command == "ls" and any(value not in allowed for value in values):
            raise ToolError("ls options supported here are -a, -l, -h and -1")
    elif command in ("head", "tail"):
        if values:
            if len(values) != 2 or values[0] != "-n":
                raise ToolError("{} accepts only -n COUNT (1 to {})".format(command, MAX_LINES))
            _bounded_count(values[1], command)
    elif command == "find":
        index = 0
        while index < len(values):
            option = values[index]
            if option not in ("-name", "-iname", "-type", "-maxdepth", "-mindepth"):
                raise ToolError("find supports only -name, -iname, -type, -maxdepth and -mindepth")
            if index + 1 >= len(values):
                raise ToolError("find option {} needs a value".format(option))
            value = values[index + 1]
            if option in ("-maxdepth", "-mindepth"):
                _bounded_count(value, "find depth", maximum=8, allow_zero=True)
            elif option == "-type" and value not in ("f", "d"):
                raise ToolError("find -type supports only f (files) or d (folders)")
            index += 2
    elif command == "grep":
        flags = {"-i", "-n", "-v", "-F", "-E", "-w"}
        patterns = [value for value in values if value not in flags]
        if any(value.startswith("-") and value not in flags for value in values):
            raise ToolError("grep supports only -i, -n, -v, -F, -E and -w options")
        if len(patterns) != 1:
            raise ToolError("grep needs one pattern in args; put the file or folder in path")
        if len(patterns[0]) > 256:
            raise ToolError("grep pattern is longer than 256 characters")
    elif command == "wc":
        if any(value not in ("-l", "-w", "-c", "-m") for value in values):
            raise ToolError("wc supports only -l, -w, -c and -m")
    elif command == "sort":
        if any(value not in ("-r", "-n", "-f", "-u") for value in values):
            raise ToolError("sort supports only -r, -n, -f and -u; use stdin for generated text")
    elif command == "uniq":
        if any(value not in ("-c", "-d", "-u", "-i") for value in values):
            raise ToolError("uniq supports only -c, -d, -u and -i")
    elif command == "cut":
        if len(values) not in (2, 4):
            raise ToolError("cut needs -f FIELDS and optionally -d DELIMITER")
        if values[0] not in ("-f", "--fields"):
            raise ToolError("cut starts with -f FIELDS")
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", values[1]):
            raise ToolError("cut field list must contain only field numbers, commas and ranges")
        if len(values) == 4 and (values[2] != "-d" or len(values[3]) != 1):
            raise ToolError("cut delimiter form is -d followed by one character")
    return values


def _bounded_count(value: str, command: str, *, maximum: int = MAX_LINES, allow_zero: bool = False) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ToolError("{} count must be an integer".format(command)) from None
    minimum = 0 if allow_zero else 1
    if count < minimum or count > maximum:
        raise ToolError("{} count must be between {} and {}".format(command, minimum, maximum))
    return count


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _check_directory_size(path: str, *, find: bool, maxdepth: int = 0) -> None:
    """Fail closed before an embedded command can dump an unbounded directory listing."""
    if not find:
        try:
            with os.scandir(path) as entries:
                for index, _entry in enumerate(entries, start=1):
                    if index > MAX_DIRECTORY_ENTRIES:
                        raise ToolError("folder has more than {} entries; use list_files for a bounded listing".format(MAX_DIRECTORY_ENTRIES))
        except OSError as exc:
            raise ToolError("could not inspect folder: {}".format(exc)) from None
        return

    # find's optional depth flags limit both the preflight walk and actual command.
    # validate_arguments has already checked each option/value pair.
    if maxdepth == 0:
        return
    stack = [(path, 0)]
    seen = 0
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_FIND_ENTRIES:
                        raise ToolError("find would inspect more than {} entries; narrow its path or depth".format(MAX_FIND_ENTRIES))
                    if entry.is_dir(follow_symlinks=False) and depth + 1 < maxdepth:
                        stack.append((entry.path, depth + 1))
        except OSError as exc:
            raise ToolError("could not inspect folder for find: {}".format(exc)) from None


class _NullLock:
    """Context-manager stand-in so desktop subprocesses can remain concurrent."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def run_command(
    command: str,
    args: Sequence[str],
    *,
    path: str,
    input_text: Optional[str],
    workspace: Any,
    available: Iterable[str],
) -> Dict[str, Any]:
    """Run one allowlisted command with argv and workspace-confined file operands."""
    if command not in set(available):
        raise ToolError("{} is not confirmed on this device; call unix_capabilities and choose an available command".format(command))
    values = validate_arguments(command, args)
    if input_text is not None and len(input_text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ToolError("stdin is larger than the {} byte limit".format(MAX_INPUT_BYTES))
    resolved = ""
    if path:
        resolved = workspace.resolve(path, must_exist=True)
        if not (os.path.isfile(resolved) or os.path.isdir(resolved)):
            raise ToolError("path must be a file or folder inside the workspace")
    elif command in ("cat", "find", "grep", "head", "tail", "ls"):
        if command == "find" or command == "ls":
            resolved = os.path.realpath(workspace.root)
        elif input_text is None:
            raise ToolError("{} needs a workspace-relative path or stdin_text".format(command))
    if input_text is not None and path:
        raise ToolError("choose either path or stdin_text, not both")
    if not path and input_text is None and command not in ("find", "ls"):
        raise ToolError("{} needs a workspace-relative path or stdin_text".format(command))
    if command in ("find", "ls") and not os.path.isdir(resolved):
        raise ToolError("{} requires a folder path".format(command))
    if command in ("cat", "head", "tail", "grep") and resolved and os.path.isdir(resolved):
        raise ToolError("{} requires a file path; use list_files/search_files for folders".format(command))
    if resolved and os.path.isfile(resolved):
        try:
            size = os.path.getsize(resolved)
        except OSError as exc:
            raise ToolError("could not inspect file: {}".format(exc)) from None
        if size > MAX_FILE_BYTES:
            raise ToolError("file is larger than the {} byte command limit; use read_file/search_files or a Python program".format(MAX_FILE_BYTES))
    elif command == "ls":
        _check_directory_size(resolved, find=False)
    elif command == "find":
        maxdepth = 8
        for index, option in enumerate(values[:-1]):
            if option == "-maxdepth":
                maxdepth = int(values[index + 1])
                break
        if not any(value == "-maxdepth" for value in values):
            values.extend(["-maxdepth", str(maxdepth)])
        _check_directory_size(resolved, find=True, maxdepth=maxdepth)

    argv = [command]
    if command == "find":
        argv.append(resolved)
        argv.extend(values)
    else:
        argv.extend(values)
        if resolved:
            argv.append(resolved)
    if input_text is not None and command in ("cat", "find", "head", "tail", "ls"):
        raise ToolError("{} does not accept stdin_text".format(command))

    pyto = ios.is_pyto()
    lock = IN_PROCESS_EXECUTION_LANE.hold("Pyto command '{}'".format(command)) if pyto else _NullLock()
    with lock:
        process = subprocess.Popen(
            argv,
            cwd=workspace.root,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(
                input=input_text.encode("utf-8") if input_text is not None else None,
                timeout=None if pyto else 30,
            )
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise ToolError("{} exceeded the 30 second desktop command limit".format(command)) from None
    out = _decode(stdout)
    err = _decode(stderr)
    truncated = len(out) + len(err) > MAX_OUTPUT_CHARS
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... output shortened by pyto-harness"
        err = ""
    elif len(out) + len(err) > MAX_OUTPUT_CHARS:
        err = err[: max(0, MAX_OUTPUT_CHARS - len(out))] + "\n... output shortened by pyto-harness"
    return {
        "argv": argv,
        "stdout": out,
        "stderr": err,
        "returncode": getattr(process, "returncode", None),
        "truncated": truncated,
        "timeout_enforced": not pyto,
        "mode": "pyto embedded command" if pyto else "desktop subprocess",
        "no_matches": command == "grep" and getattr(process, "returncode", None) == 1 and not err.strip(),
    }

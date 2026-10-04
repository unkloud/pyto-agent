"""Agent loop: the iOS-aware system prompt, the turn loop, and the approval policy.

Shape of a turn:

1. the session log is projected back into provider messages (so resume is exact);
2. ``stream_sync`` runs in a worker thread and its deltas are relayed as events, so the
   UI paints tokens live without the loop ever blocking on the network;
3. tool calls in the assistant message are dispatched **concurrently** — the registry
   runs sync handlers off-loop — and the result messages are appended in the model's
   original order regardless of completion order, because the provider requires it;
4. ``finish`` (or a plain text answer with no tool calls) ends the turn.

Approval is a single policy function evaluated before dispatch, so there is no path a
tool can take that skips it.  The default policy auto-allows workspace file work and
reading device state, and requires a yes for anything that leaves the app: sharing,
opening URLs, running Shortcuts, notifications, speech, photos.  ``--yolo`` bypasses it.
An unattended run (a Shortcut, a headless invocation: nothing attached to answer) has no
human to ask, so ``run_program`` is denied there unless the user opts in with
``--allow-unattended-programs`` / ``allow_unattended_programs: true``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import budget, ios, pyto_api
from .config import Config
from .errors import HarnessError
from .llm import AssistantStream, LLMClient, LLMConfig, RetryPolicy, ToolCall, Usage
from .security import scrub_secrets, scrub_value
from .session import (
    SessionLog,
    assistant_message_event,
    tool_message_event,
    user_message_event,
)
from .textbudget import DEFAULT_MAX_CHARS, truncate_middle
from .tools import Decision, PolicyFn, ToolRegistry, ToolResult

# --------------------------------------------------------------------------------------
# Approval policy
# --------------------------------------------------------------------------------------

#: Tools that only touch the workspace or read device state: always allowed.
#: ``diagnose`` (without network), ``selftest`` and ``read_source`` are here because they
#: are read-only and are exactly what the agent must be able to run *before* asking for a
#: repair.  The tools that write to the harness's own source need a yes: see below.
AUTO_APPROVED_TOOLS = frozenset(
    {
        "write_program",
        "run_program",
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "clipboard_get",
        "memory_read",
        "memory_write",
        "memory_status",
        "calendar_list_events",
        "device_capabilities",
        "pyto_api",
        "diagnose",
        "selftest",
        "read_source",
        "finish",
    }
)

#: Tools whose whole purpose is to move data out of the app or act on the world.
#: These need a human yes unless ``--yolo``.
APPROVAL_REQUIRED_TOOLS = frozenset(
    {
        "share_text",
        "open_url",
        "shortcut_run",
        "shortcut_run_wait",
        "clipboard_set",
        "notify",
        "speak",
        "save_photo",
        "open_in_files",
        "calendar_add_event",
        "keepalive_start",
        "keepalive_stop",
        "apply_fix",
        "self_edit",
        "list_backups",
        "restore_backup",
    }
)

#: Human-readable hazard notes for the approval prompt.
TOOL_HAZARDS = {
    "share_text": "opens the share sheet, sending your text to another app",
    "open_url": "leaves this app and opens an external URL",
    "shortcut_run": "runs one of your Shortcuts, which can change data or spend money",
    "shortcut_run_wait": "runs one of your Shortcuts and waits for its output",
    "clipboard_set": "overwrites the system clipboard",
    "notify": "posts a notification",
    "speak": "plays audio out loud",
    "save_photo": "writes to your photo library",
    "open_in_files": "switches to the Files app",
    "calendar_add_event": "writes an event into your calendar",
    "keepalive_start": "keeps this app running in the background after you leave it",
    "keepalive_stop": "ends a background task",
    "apply_fix": "changes this harness's own settings or state (directories, file modes, api_base, model)",
    "self_edit": "rewrites the harness's own source code, gated by its offline tests",
    "list_backups": "lists the source snapshots this harness has taken",
    "restore_backup": "replaces the harness source with an older snapshot",
}


#: How much of one string argument the approval prompt shows.  The old 60 characters hid
#: the payload of exactly the tools that move data out of the app (the interesting part of
#: an exfiltration URL is by construction *after* the prefix), so the cap is now large
#: enough for a real URL, a real message and a real program, and anything cut is marked
#: explicitly.  A security decision is never made from a silent preview.
APPROVAL_ARG_CHARS = 4000

#: Bytes hashed for the ``run_program`` fingerprint line.  Bigger than any program a model
#: writes, small enough that the approval prompt cannot read a 4 GB file.
APPROVAL_HASH_BYTES = 1024 * 1024


def _render_argument(value: Any) -> str:
    """One argument, in full up to :data:`APPROVAL_ARG_CHARS`, with an explicit cut."""
    if not isinstance(value, str):
        return repr(value)
    if len(value) <= APPROVAL_ARG_CHARS:
        return repr(value)
    remaining = len(value) - APPROVAL_ARG_CHARS
    return "{} …({} more characters)".format(repr(value[:APPROVAL_ARG_CHARS]), remaining)


def _program_fingerprint(arguments: Mapping[str, Any], workspace: str = "") -> List[str]:
    """Path + SHA-256 of the bytes ``run_program`` would execute, for the prompt.

    For a workspace file the bytes on disk are hashed, so a user who approves sees the
    digest of what will actually run (and can compare it against the diff they expected).
    For inline source the digest is of the source text itself.  ``workspace`` is the
    resolved workspace the tool will resolve a relative path against, so the prompt shows
    the absolute path the human can go and read.
    """
    raw = arguments.get("path_or_source")
    if not isinstance(raw, str) or not raw:
        return []
    looks_like_path = "\n" not in raw and raw.strip().endswith(".py")
    if not looks_like_path:
        return [
            "  program: <inline source>",
            "  sha256 : {}".format(hashlib.sha256(raw.encode("utf-8")).hexdigest()),
        ]
    stripped = raw.strip()
    candidates = []
    if os.path.isabs(stripped):
        candidates.append(stripped)
    else:
        if workspace:
            candidates.append(os.path.join(workspace, stripped))
        candidates.append(os.path.abspath(os.path.expanduser(stripped)))
    for candidate in candidates:
        try:
            size = os.path.getsize(candidate)
            with open(candidate, "rb") as handle:
                data = handle.read(APPROVAL_HASH_BYTES)
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if size > APPROVAL_HASH_BYTES:
            digest += " (first {} bytes of {})".format(APPROVAL_HASH_BYTES, size)
        return [
            "  program: {} ({} bytes on disk)".format(os.path.abspath(candidate), size),
            "  sha256 : {}".format(digest),
        ]
    return [
        "  program: {} (not readable from this process; it is resolved inside the workspace)".format(stripped)
    ]


@dataclass
class ApprovalRequest:
    """What the UI is asked to confirm."""

    tool: str
    arguments: Mapping[str, Any]
    reason: str
    #: The resolved workspace, so a relative program path can be shown in full.
    workspace: str = ""

    def describe(self) -> str:
        rendered = ", ".join(
            "{}={}".format(key, _render_argument(value)) for key, value in sorted(self.arguments.items())
        )
        lines = ["{}({})".format(self.tool, rendered), "  why: {}".format(self.reason)]
        if self.tool == "run_program":
            lines.extend(_program_fingerprint(self.arguments, self.workspace))
        return "\n".join(lines)


#: ``(request) -> bool``.  Called from whatever thread dispatches the tool.
Prompter = Callable[[ApprovalRequest], bool]


def prompter_is_interactive(prompter: Optional[Prompter]) -> bool:
    """True when a human is attached who can actually answer an approval prompt.

    ``TerminalApprover`` carries an ``interactive`` flag (it is false when stdin is a pipe,
    which is the Shortcut/headless case).  A prompter without the flag is a caller-supplied
    one — a test double or a UI — and is taken at its word.
    """
    if prompter is None:
        return False
    flag = getattr(prompter, "interactive", None)
    return True if flag is None else bool(flag)


#: Why ``run_program`` is refused when nothing can approve it.  Kept in one place because
#: the sentence is the whole mitigation: the user has to know the trade they are making.
UNATTENDED_PROGRAM_REASON = (
    "run_program executes a program with this app's own authority (the files it can read, "
    "the network, this process's memory) and nothing is attached to approve it. If this "
    "unattended run is trusted, re-run with --allow-unattended-programs or set "
    "\"allow_unattended_programs\": true in the config file."
)


def make_policy(
    *,
    yolo: bool = False,
    prompter: Optional[Prompter] = None,
    allow: Sequence[str] = (),
    deny: Sequence[str] = (),
    unattended_programs: bool = False,
    interactive: Optional[bool] = None,
    workspace: str = "",
) -> PolicyFn:
    """Build the approval policy.

    Order: explicit deny, explicit allow, ``--yolo``, the auto-approved set, then the
    prompter.  A tool in none of those categories is **denied** rather than allowed: an
    unrecognised tool is exactly the case where a wrong guess is expensive.

    ``run_program`` is one deliberate exception to the auto-approved set: it stays AUTO
    while a human is attached (that is the product — the model writes a program and runs
    it without a prompt), but in an unattended run there is nobody to ask, so it fails
    closed unless ``unattended_programs`` (or ``--yolo``) says the user meant it.
    """
    deny_set = set(deny)
    allow_set = set(allow)
    interactive_session = prompter_is_interactive(prompter) if interactive is None else bool(interactive)

    def policy(name: str, arguments: Mapping[str, Any]) -> Decision:
        if name in deny_set:
            return Decision.deny("{} is on the deny list".format(name))
        if name in allow_set:
            return Decision.allow()
        if name == "run_program" and not yolo and not unattended_programs and not interactive_session:
            return Decision.deny(UNATTENDED_PROGRAM_REASON)
        if yolo:
            return Decision.allow()
        if name in AUTO_APPROVED_TOOLS:
            return Decision.allow()
        reason = TOOL_HAZARDS.get(name, "this tool can affect things outside the workspace")
        if prompter is None:
            if name in APPROVAL_REQUIRED_TOOLS:
                return Decision.deny(
                    "{} needs approval ({}) and no approver is attached; re-run with --yolo to allow it".format(
                        name, reason
                    )
                )
            return Decision.deny("{} is not on the allow list".format(name))
        request = ApprovalRequest(tool=name, arguments=arguments, reason=reason, workspace=workspace)
        try:
            granted = prompter(request)
        except Exception as exc:  # noqa: BLE001 - a broken prompter must not open the gate
            return Decision.deny("the approval prompt failed: {}: {}".format(type(exc).__name__, exc))
        return Decision.allow() if granted else Decision.deny("the user declined")

    return policy


def auto_prompter(answer: bool) -> Prompter:
    """Non-interactive prompter, for tests and for ``--yes``."""

    def prompter(request: ApprovalRequest) -> bool:
        return answer

    return prompter


# --------------------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a personal automation assistant running inside Pyto on the user's iPhone or iPad. \
You write and run small Python programs on the device for them.

How this environment works
- You are a guest in a sandboxed app. There is no background daemon and no server: when the \
user leaves the app, iOS suspends and eventually kills it. Anything worth keeping must exist as \
a file in the workspace, not in this conversation.
- Working directory: {workspace}
- {platform}
- Available device features: {capabilities}
- The Python standard library is installed, and so are Pyto's own modules: {pyto_modules}. \
Import them whenever they are the right tool -- they are the only way to reach the clipboard, \
the photo library, notifications, the calendar, the share sheet, Shortcuts, the motion and \
location sensors and a real UIKit window. `pip install` is not available, so a program may only \
import the standard library and the modules listed above; do not assume a shell, `bash`, `cron`, \
`launchd` or a daemon exists.
- There is no PTY and no real `subprocess`: on iOS a program you run executes *inside this \
app*, synchronously. `sys.exit()`, `input()`, curses and progress bars all misbehave, and a \
program blocked in a C call (DNS, sockets, a big decode) cannot be interrupted. Keep \
programs short, bounded and non-interactive, and give long loops their own deadline.
- The clipboard only works while this app is in the foreground, and Pyto stops every \
running script when free memory gets near 500 MB, so keep outputs and files small.

How to work
- Prefer writing a reusable `.py` program with `write_program` over doing work inline. The user \
can run it again later from Pyto or from a Shortcut, and it survives the app being killed.
- Run what you wrote with `run_program` before you claim it works. Report the real output.
- Before writing a program that imports a Pyto module, call `pyto_api` for that module (or read \
`PYTO_LIBS.md` in the workspace) and use only the members it reports. Pyto's API is small, \
version-specific and easy to misremember: do not guess names such as `photos.save_photo` or \
`pasteboard.set_clipboard`.
- If `run_program` reports an `AttributeError` or `ImportError` about a Pyto module, call \
`pyto_api` for that module and fix the name it suggests instead of guessing again.
- Keep programs short, print a clear summary at the end, and print what the user should do next.
- Paths the user gives you may be outside the workspace (Photos, iCloud Drive). If you cannot \
reach something, say so plainly and offer the closest thing that does work.
- When a task needs a capability that is unavailable, say which one and suggest the Shortcut or \
manual step that replaces it. Do not pretend an action happened.

Asking permission
- Ask before anything that shares data or spends money: the share sheet, opening external URLs, \
running Shortcuts, notifications, speech, writing to the photo library.
- Reading files in the workspace and running the programs you just wrote need no permission while \
the user is there to answer. In an unattended run (a Shortcut, a headless invocation) nothing is \
attached to approve, so `run_program` is denied unless the user allowed unattended programs; do \
not try to work around that, say what you would have run.

Fixing yourself
- When something fails twice for the same reason, call `diagnose` before improvising. It reports \
check ids, statuses and the exact human action for anything it cannot repair. Add `network=true` \
only when you need DNS/TLS/auth verified; that costs one tiny API request.
- If diagnose names a fix id, call `apply_fix` with it: creating directories, file modes, a torn \
session line, a wrong api_base or model name are all machine repairs.
- Only after that, if the problem is in this harness's own Python source, reach for `self_edit`: \
read the file with `read_source`, make the smallest possible change, run `selftest` first, and \
expect an automatic revert if the offline tests fail. `list_backups` and `restore_backup` can undo \
a bad change.
- Never edit files to work around a missing iOS permission, a missing entitlement or a feature Pyto \
does not have. Report the human action the doctor gave you instead; that is the honest answer.

Ending the turn
- Call `finish` with a short, non-technical summary: what you did, what it produced, and exactly \
what to run next. If you are waiting on the user, say so in the same summary.
"""


def build_system_prompt(config: Config, workspace: str, *, extra: str = "") -> str:
    """Render the system prompt for this device and workspace."""
    capabilities = ios.available_capabilities()
    native = sorted(name for name, present in capabilities.items() if present)
    missing = sorted(name for name, present in capabilities.items() if not present)
    rendered = SYSTEM_PROMPT.format(
        workspace=workspace,
        platform=ios.platform_label(),
        capabilities=", ".join(native) if native else "none detected",
        pyto_modules=", ".join(pyto_api.prompt_module_names()),
    )
    if missing:
        rendered += (
            "\nRight now these are NOT available and will only record an 'unsupported' result: {}.\n"
            "If a request depends on one of them, tell the user instead of calling the tool.\n".format(
                ", ".join(missing)
            )
        )
    if extra:
        rendered += "\n" + extra.strip() + "\n"
    return rendered


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


@dataclass
class Event:
    """One observable step of the loop.  ``kind`` is the discriminator."""

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, **self.data}


EVENT_KINDS = (
    "status",
    "turn.started",
    "delta",
    "reasoning.delta",
    "message.completed",
    "tool.started",
    "tool.completed",
    "tool.denied",
    "usage",
    "finished",
    "turn.limit",
    "error",
    "done",
)


@dataclass
class TurnResult:
    """Summary of one whole user turn."""

    turns: int = 0
    finished: bool = False
    message: str = ""
    tool_calls: int = 0
    stop: str = "stop"
    duration_ms: int = 0
    usage: Usage = field(default_factory=Usage)
    errors: List[str] = field(default_factory=list)


@dataclass
class LoopOptions:
    """Everything a turn needs."""

    client: LLMClient
    registry: ToolRegistry
    session: SessionLog
    system_prompt: str = ""
    max_turns: int = 8
    max_tool_result_chars: int = DEFAULT_MAX_CHARS
    spill_dir: str = ""
    max_parallel_tools: int = 4
    stream: bool = True
    #: Trim the session log at turn boundaries when it nears the memory budget.
    compact: bool = True
    on_event: Optional[Callable[[Event], None]] = None
    #: Checked between turns and between socket reads.
    stop: Optional[threading.Event] = None

    def __post_init__(self) -> None:
        # Capture the approval policy here, at construction, and freeze it for the run.
        # A program running in this process can still reach into the object graph (see
        # SECURITY.md); what this stops is the easy rebinding of the public attribute and
        # makes such an attempt deny loudly instead of silently opening the gate.
        lock = getattr(self.registry, "lock_policy", None)
        if callable(lock):
            lock()


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


async def run_turn(options: LoopOptions, prompt: str) -> Any:
    """Run one user turn, yielding :class:`Event` objects.  Async generator.

    The final ``done`` event carries the whole :class:`TurnResult` under ``result``,
    because an async generator cannot return a value in Python 3.10.
    """
    started = time.monotonic()
    session = options.session
    result = TurnResult()
    stop = options.stop or threading.Event()

    # Pyto stops every script near 500 MB free, so an unbounded log is a crash rather
    # than a slow leak.  Compaction happens here, at a turn boundary, and only ever cuts
    # on a user message (see SessionLog.compact).
    compacted = session.compact() if options.compact else None
    if compacted:
        yield _emit(options, Event("status", {"message": "compacted the session log", **compacted}))

    messages: List[Dict[str, Any]] = []
    if options.system_prompt:
        messages.append({"role": "system", "content": options.system_prompt})
    messages.extend(session.project())

    user_event = user_message_event(prompt)
    session.append("message.user", user_event)
    messages.append(user_event["message"])

    tool_schemas = options.registry.definitions()
    turns = 0
    while turns < max(1, options.max_turns):
        if stop.is_set():
            session.append("error", {"code": "CANCELLED", "message": "cancelled by the caller"})
            yield _emit(options, Event("error", {"code": "CANCELLED", "message": "cancelled"}))
            result.stop = "cancelled"
            break
        turns += 1
        session.append("turn.started", {"turn": turns, "messages": len(messages)})
        yield _emit(options, Event("turn.started", {"turn": turns}))

        stream: Optional[AssistantStream] = None
        failure: Optional[BaseException] = None
        async for item in _stream_assistant(options, messages, tool_schemas, turns, stop, session):
            if isinstance(item, Event):
                yield _emit(options, item)
            elif isinstance(item, AssistantStream):
                stream = item
            elif isinstance(item, BaseException):
                failure = item
        if failure is not None:
            if isinstance(failure, HarnessError):
                message = "{}: {}".format(failure.code, failure.message)
            else:
                message = "{}: {}".format(type(failure).__name__, failure)
            # A provider error body is foreign text: scrub it before it reaches the log,
            # the model and the console.
            message = scrub_secrets(message)
            session.append("turn.failed", {"turn": turns, "message": message})
            result.errors.append(message)
            result.stop = "error"
            yield _emit(options, Event("error", {"message": message, "turn": turns}))
            break
        if stop.is_set():
            result.stop = "cancelled"
            break
        if stream is None:  # pragma: no cover - _stream_assistant always yields one
            result.stop = "error"
            result.errors.append("no assistant message was produced")
            break

        if stream.usage.total_tokens:
            session.append("usage", stream.usage.to_wire())
            result.usage = stream.usage
            yield _emit(options, Event("usage", stream.usage.to_wire()))

        assistant_message = stream.to_message()
        session.append("message.assistant", assistant_message_event(assistant_message))
        messages.append(assistant_message)
        yield _emit(
            options,
            Event(
                "message.completed",
                {
                    "turn": turns,
                    "content": scrub_secrets(stream.text),
                    "reasoning": scrub_secrets(stream.reasoning_text),
                    "tool_calls": len(stream.tool_calls),
                    "finish_reason": stream.finish_reason,
                },
            ),
        )

        if not stream.tool_calls:
            session.append("turn.completed", {"turn": turns, "stop": stream.finish_reason or "stop"})
            result.finished = True
            result.message = stream.text.strip()
            result.stop = stream.finish_reason or "stop"
            break

        calls = _collect_calls(stream.tool_calls)
        result.tool_calls += len(calls)
        async for item in _dispatch(options, calls, turns):
            if isinstance(item, Event):
                yield _emit(options, item)
            else:
                results = item
                for call, tool_result in results:
                    message = tool_result.to_message(call[0], call[1])
                    session.append("message.tool", tool_message_event(message))
                    messages.append(message)
                    # `finish` reports itself through tool metadata: no shared mutable
                    # flag, so two sessions in one process cannot confuse each other.
                    if tool_result.metadata.get("finished"):
                        result.finished = True
                        result.message = str(tool_result.metadata.get("message") or "")

        if result.finished:
            session.append("finish", {"message": result.message})
            yield _emit(options, Event("finished", {"message": result.message, "turn": turns}))
            result.stop = "finish_tool"
            break

        if turns >= options.max_turns:
            session.append("turn.limit", {"turn": turns, "limit": options.max_turns})
            yield _emit(options, Event("turn.limit", {"turn": turns, "limit": options.max_turns}))
            result.stop = "turn_limit"
            break

    result.turns = turns
    result.duration_ms = int((time.monotonic() - started) * 1000)
    yield _emit(
        options,
        Event(
            "done",
            {
                "turns": turns,
                "stop": result.stop,
                "finished": result.finished,
                "message": result.message,
                "duration_ms": result.duration_ms,
                "tool_calls": result.tool_calls,
                "errors": list(result.errors),
                "usage": result.usage.to_wire(),
            },
        ),
    )



async def _stream_assistant(
    options: LoopOptions,
    messages: Sequence[Mapping[str, Any]],
    tool_schemas: Sequence[Mapping[str, Any]],
    turn: int,
    stop: threading.Event,
    session: SessionLog,
) -> Any:
    """Run one model request in a worker thread, yielding deltas as they arrive.

    Yields :class:`Event` deltas, then exactly one :class:`AssistantStream` on success or
    one exception instance on failure.  The worker thread is what keeps the event loop
    free; ``asyncio.to_thread`` on the consumer side makes the hand-off non-blocking.
    """
    pending: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

    def worker() -> None:
        try:
            stream = options.client.stream_sync(
                messages,
                tools=list(tool_schemas) or None,
                stream=options.stream,
                on_delta=lambda kind, text: pending.put(("delta", (kind, text))),
                on_usage=lambda usage: pending.put(("usage", usage)),
                stop=stop,
            )
            pending.put(("done", stream))
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            pending.put(("error", exc))

    thread = threading.Thread(target=worker, name="pyto-agent-llm", daemon=True)
    thread.start()
    deltas = 0
    while True:
        try:
            kind, payload = await asyncio.to_thread(pending.get, True, 0.25)
        except queue.Empty:
            if not thread.is_alive() and pending.empty():
                break
            continue
        if kind == "delta":
            delta_kind, text = payload
            deltas += 1
            yield Event("delta" if delta_kind == "content" else "reasoning.delta", {"text": text, "turn": turn})
        elif kind == "usage":
            yield Event("usage", payload.to_wire())
        elif kind == "done":
            yield payload
            return
        else:
            failed = "{}: {}".format(type(payload).__name__, payload)
            session.append("turn.failed", {"turn": turn, "message": scrub_secrets(failed)})
            yield payload
            return
    if deltas == 0:
        session.append("turn.failed", {"turn": turn, "message": "the model stream ended without events"})


def _collect_calls(tool_calls: Sequence[ToolCall]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """``(call_id, name, arguments)`` for each requested call.

    A call whose arguments are not valid JSON becomes a call to a synthetic
    ``__malformed__`` name so the dispatcher turns it into a model-visible tool error
    rather than dropping it — the provider requires a tool message for every tool_call id.
    """
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for index, call in enumerate(tool_calls):
        call_id = call.id or "call_{}".format(index)
        try:
            arguments = call.arguments()
        except HarnessError as exc:
            out.append((call_id, call.name, {"__malformed_arguments__": exc.message}))
            continue
        out.append((call_id, call.name, arguments))
    return out


async def _dispatch(
    options: LoopOptions, calls: Sequence[Tuple[str, str, Dict[str, Any]]], turn: int
) -> Any:
    """Approve and run a batch of calls concurrently.  Yields events, then the results."""
    runnable: List[Tuple[str, str, Dict[str, Any]]] = []
    results: Dict[str, ToolResult] = {}
    for call_id, name, arguments in calls:
        if "__malformed_arguments__" in arguments:
            detail = arguments["__malformed_arguments__"]
            results[call_id] = ToolResult.error(
                "{} could not be run: the model sent malformed JSON arguments. {}".format(name, detail),
                malformed=True,
            )
            yield Event("tool.denied", {"turn": turn, "id": call_id, "name": name, "reason": "malformed arguments"})
            continue
        decision = options.registry.check(name, arguments)
        if decision.allowed:
            runnable.append((call_id, name, arguments))
        else:
            results[call_id] = ToolResult.error(
                "{} was not run: denied by policy ({})".format(
                    name, decision.reason or "no reason given"
                ),
                denied=True,
                reason=decision.reason,
            )
            yield Event(
                "tool.denied", {"turn": turn, "id": call_id, "name": name, "reason": decision.reason}
            )

    for call_id, name, arguments in runnable:
        yield Event(
            "tool.started",
            {"turn": turn, "id": call_id, "name": name, "arguments": scrub_value(_short(arguments))},
        )

    if runnable:
        semaphore = asyncio.Semaphore(max(1, options.max_parallel_tools))

        async def run(call_id: str, name: str, arguments: Mapping[str, Any]) -> Tuple[str, ToolResult]:
            async with semaphore:
                # `execute`, not `invoke`: approval already happened above, and asking the
                # policy twice would prompt the user twice for one model request.
                return call_id, await options.registry.execute(name, arguments)

        tasks = [asyncio.ensure_future(run(cid, name, args)) for cid, name, args in runnable]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for (call_id, name, _args), outcome in zip(runnable, gathered):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                result = ToolResult.error("{}: {}".format(type(outcome).__name__, outcome))
                result.tool = name
            else:
                result = outcome[1]
            results[call_id] = result

    ordered: List[Tuple[Tuple[str, str, Dict[str, Any]], ToolResult]] = []
    for call in calls:
        call_id, name, _arguments = call
        result = results.get(call_id) or ToolResult.error("the tool call was never dispatched")
        result = _scrub_result(_apply_budget(options, name, result))
        options.session.append(
            "tool.completed",
            {
                "turn": turn,
                "id": call_id,
                "name": name,
                "is_error": result.is_error,
                "duration_ms": result.duration_ms,
                "chars": len(result.content),
                "truncated": bool(result.metadata.get("truncated")),
            },
        )
        yield Event(
            "tool.completed",
            {
                "turn": turn,
                "id": call_id,
                "name": name,
                "is_error": result.is_error,
                "content": result.content,
                "duration_ms": result.duration_ms,
                "truncated": bool(result.metadata.get("truncated")),
                "metadata": result.metadata,
            },
        )
        ordered.append((call, result))
    yield ordered


def _scrub_result(result: ToolResult) -> ToolResult:
    """Remove credentials by shape from a tool result before it goes anywhere.

    Everything downstream of this point is a copy of the same string: the tool message in
    the session log, the tool message sent to the provider, and the text the printer writes
    to the console.  Scrubbing once, here, is what makes "the key is never printed" true for
    a program that printed it.
    """
    content = scrub_secrets(result.content if isinstance(result.content, str) else str(result.content))
    metadata = scrub_value(dict(result.metadata)) if result.metadata else result.metadata
    if content == result.content and metadata == result.metadata:
        return result
    return ToolResult(
        content=content,
        is_error=result.is_error,
        metadata=metadata if isinstance(metadata, dict) else dict(result.metadata),
        duration_ms=result.duration_ms,
        tool=result.tool,
    )


def _apply_budget(options: LoopOptions, name: str, result: ToolResult) -> ToolResult:
    """Loop-level truncation: no single tool result may blow the context window."""
    text = result.content if isinstance(result.content, str) else str(result.content)
    if len(text) <= options.max_tool_result_chars:
        return result
    clamped = truncate_middle(
        text,
        limit=options.max_tool_result_chars,
        spill_dir=options.spill_dir or None,
        spill_name="tool-{}".format(name),
        label="{} output".format(name),
    )
    return ToolResult(
        content=clamped.text,
        is_error=result.is_error,
        metadata={
            **result.metadata,
            "truncated": True,
            "full_chars": clamped.full_chars,
            "spill_path": clamped.spill_path,
        },
        duration_ms=result.duration_ms,
        tool=result.tool or name,
    )


def _short(arguments: Mapping[str, Any], limit: int = 200) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + "... (+{} chars)".format(len(value) - limit)
        elif isinstance(value, list) and len(value) > 8:
            out[key] = list(value[:8]) + ["... (+{} more)".format(len(value) - 8)]
        else:
            out[key] = value
    return out


def _emit(options: LoopOptions, event: Event) -> Event:
    if options.on_event is not None:
        options.on_event(event)
    return event


# --------------------------------------------------------------------------------------
# Wiring helpers
# --------------------------------------------------------------------------------------


def client_from_config(config: Config) -> LLMClient:
    """Build the provider client from resolved configuration."""
    return LLMClient(
        LLMConfig(
            api_base=config.api_base,
            model=config.model,
            api_key=config.api_key,
            timeout=config.timeout,
            retry=RetryPolicy(),
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            extra_headers=dict(config.extra_headers),
        )
    )

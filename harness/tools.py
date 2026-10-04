"""Tool registry: declaration, validation, approval, dispatch, per-tool timeout.

The contract, and why each part exists:

* **A tool result is a value, never an exception.**  A validation failure, a handler
  crash, a timeout and a policy denial all come back as a ``ToolResult`` with
  ``is_error=True`` and a readable message, so the model can correct itself instead of
  the turn dying.  The only thing that propagates is cancellation.
* **The registry owns validation, not the handler.**  Every handler would otherwise
  re-implement the same argument checks, and a model typo would surface as a traceback
  rather than as "expected integer, got string".
* **Sync handlers run in a worker thread** (``asyncio.to_thread``).  Without this, a
  ``run_program`` call that takes 20 seconds would block the event loop and every other
  tool call in the same batch would silently serialise behind it.  There is a
  regression test for exactly that (``test_parallel_tools_actually_overlap``).
* **Per-tool timeout.**  ``asyncio.wait_for`` bounds the *await*.  Python cannot kill a
  thread, so a timed-out sync handler is abandoned rather than interrupted: the
  registry reports the timeout, and the orphaned thread is expected to be
  self-limiting (``run_program`` has its own process timeout for that reason).
* **Approval is a policy function**, evaluated before dispatch, so there is no separate
  code path a tool can take to bypass it.
"""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ApprovalDenied, ToolError, ToolTimeoutError, ValidationError
from .schema import assert_valid, coerce, summarize

#: Default per-tool timeout.  Long enough for a subprocess, short enough that a wedged
#: tool cannot hold a turn open on a phone.
DEFAULT_TOOL_TIMEOUT = 120.0


@dataclass
class ToolResult:
    """Outcome of one tool call.  Errors are values."""

    content: str
    is_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    #: Tool name, filled in by the registry so the loop can report it without a lookup.
    tool: str = ""

    @classmethod
    def ok(cls, content: str, **metadata: Any) -> "ToolResult":
        return cls(content=content, metadata=metadata)

    @classmethod
    def error(cls, content: str, **metadata: Any) -> "ToolResult":
        return cls(content=content, is_error=True, metadata=metadata)

    def to_message(self, call_id: str, tool_name: Optional[str] = None) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name or self.tool,
            "content": self.content,
        }


ToolHandler = Callable[..., Any]


@dataclass
class Decision:
    """Result of an approval check."""

    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "Decision":
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(False, reason)


#: ``(tool_name, arguments) -> bool | Decision``.  May be a coroutine function.
PolicyFn = Callable[[str, Mapping[str, Any]], Any]


@dataclass
class ToolDef:
    """One registered tool."""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    #: Seconds; ``None`` disables the timeout for this tool.
    timeout: Optional[float] = DEFAULT_TOOL_TIMEOUT
    #: True when the handler never blocks (pure in-memory work) and may stay on the loop.
    inline: bool = False
    #: Human-readable hazard note, surfaced by the approval prompt and the policy.
    danger: Optional[str] = None
    is_async: bool = False

    def schema(self) -> Dict[str, Any]:
        """The OpenAI ``tools[]`` entry for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Name -> :class:`ToolDef`, plus validation, approval and concurrent dispatch."""

    def __init__(self, *, policy: Optional[PolicyFn] = None) -> None:
        self._tools: Dict[str, ToolDef] = {}
        self.policy = policy
        #: name -> invocation count, for diagnostics.
        self.invocations: Dict[str, int] = {}
        #: Every approval decision, in order: ``(tool, allowed, reason)``.
        self.approvals: List[Tuple[str, bool, str]] = []

    # -- registration --------------------------------------------------------------

    def register(self, tool: ToolDef) -> ToolDef:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError("tool {!r} is already registered".format(tool.name))
        if not tool.description:
            raise ValueError("tool {!r} needs a description: it is what the model reads".format(tool.name))
        tool.is_async = inspect.iscoroutinefunction(tool.handler)
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = DEFAULT_TOOL_TIMEOUT,
        inline: bool = False,
        danger: Optional[str] = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form of :meth:`register`."""

        def decorate(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolDef(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=handler,
                    timeout=timeout,
                    inline=inline,
                    danger=danger,
                )
            )
            return handler

        return decorate

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDef:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolError("unknown tool {!r}; available tools: {}".format(name, known)) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def tools(self) -> List[ToolDef]:
        return [self._tools[name] for name in self.names()]

    def definitions(self, only: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """The ``tools`` array for a chat-completions request."""
        allow = set(only) if only is not None else None
        return [t.schema() for t in self._tools.values() if allow is None or t.name in allow]

    # -- approval ------------------------------------------------------------------

    def check(self, name: str, arguments: Mapping[str, Any]) -> Decision:
        """Run the policy.  A policy that raises denies the call: fail closed."""
        if self.policy is None:
            return Decision.allow()
        try:
            outcome = self.policy(name, arguments)
        except Exception as exc:  # noqa: BLE001 - a broken policy must not open the gate
            return Decision.deny("approval policy raised {}: {}".format(type(exc).__name__, exc))
        if isinstance(outcome, Decision):
            decision = outcome
        elif isinstance(outcome, bool):
            decision = Decision.allow() if outcome else Decision.deny("policy returned False")
        elif outcome is None:
            decision = Decision.allow()
        else:
            decision = Decision.allow() if outcome else Decision.deny("policy returned {!r}".format(outcome))
        self.approvals.append((name, decision.allowed, decision.reason))
        return decision

    # -- dispatch ------------------------------------------------------------------

    async def invoke(self, name: str, arguments: Any) -> ToolResult:
        """Validate, approve, then execute one call.  Never raises for tool-level faults.

        Approval lives here so a standalone call cannot skip it.  A caller that has
        *already* consulted the policy — the agent loop does, so it can report a denial as
        its own event — must call :meth:`execute` instead, or the user is asked twice.
        """
        started = time.monotonic()
        prepared = self._prepare(name, arguments, started)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, args = prepared

        decision = self.check(name, args)
        if not decision.allowed:
            reason = "denied by policy: {}".format(decision.reason or "no reason given")
            result = ToolResult.error(
                "{} was not run: {}".format(name, reason),
                denied=True,
                reason=decision.reason,
                policy_code=ApprovalDenied.code,
            )
            result.tool = name
            return self._timed(result, started)
        return await self._run(tool, name, args, started)

    async def execute(self, name: str, arguments: Any) -> ToolResult:
        """Validate and run, **without** consulting the policy.

        Only for callers that have already decided; :meth:`invoke` is the safe default.
        """
        started = time.monotonic()
        prepared = self._prepare(name, arguments, started)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, args = prepared
        return await self._run(tool, name, args, started)

    def _prepare(self, name: str, arguments: Any, started: float) -> Any:
        """Resolve and validate.  Returns ``(ToolDef, args)`` or an error result."""
        try:
            tool = self.get(name)
        except ToolError as exc:
            result = ToolResult.error(str(exc))
            result.tool = name
            return self._timed(result, started)

        try:
            args = coerce(arguments if isinstance(arguments, dict) else {}, tool.parameters)
            assert_valid(args, tool.parameters)
        except ValidationError as exc:
            result = ToolResult.error(
                "{}\nExpected arguments: {}".format(exc.message, summarize(tool.parameters)),
                validation=True,
            )
            result.tool = name
            return self._timed(result, started)
        return tool, args

    async def _run(self, tool: ToolDef, name: str, args: Mapping[str, Any], started: float) -> ToolResult:
        self.invocations[name] = self.invocations.get(name, 0) + 1
        try:
            result = await self._call(tool, args)
        except asyncio.CancelledError:
            raise
        except ToolTimeoutError as exc:
            result = ToolResult.error(str(exc), timeout=True, timeout_s=tool.timeout)
        except ToolError as exc:
            result = ToolResult.error("{}: {}".format(exc.code, exc.message))
        except Exception as exc:  # noqa: BLE001 - a handler bug is a model-visible error
            result = ToolResult.error("{}: {}".format(type(exc).__name__, exc), handler_crash=True)

        if not isinstance(result, ToolResult):
            result = ToolResult.ok(result if isinstance(result, str) else repr(result))
        result.tool = name
        return self._timed(result, started)

    async def _call(self, tool: ToolDef, args: Mapping[str, Any]) -> ToolResult:
        """Run one handler, honouring its timeout.

        A synchronous handler always runs in a worker thread — that is what makes
        ``parallel`` dispatch real instead of nominal, and what keeps a 30-second program
        from freezing every other call in the batch.  A handler declared ``inline`` is the
        one exception, for tools that only touch in-memory state.

        The timeout is a *bounded wait*, not ``asyncio.wait_for(tasks)``: the result comes
        back through a queue, so the caller returns at the deadline whether or not the
        handler has finished.  ``wait_for`` around ``to_thread`` cannot do that — it waits
        for the thread during its own cleanup, which silently turns a 0.2s timeout into a
        2s stall.
        """
        if tool.is_async:
            return await self._bounded_async(tool, tool.handler(**dict(args)))
        if tool.inline:
            return self._finish(tool.handler(**dict(args)))
        return await asyncio.to_thread(_invoke_bounded, tool.handler, dict(args), tool.timeout, tool.name)

    async def _bounded_async(self, tool: ToolDef, awaitable: Any) -> ToolResult:
        if tool.timeout is None:
            return self._finish(await awaitable)
        try:
            return self._finish(await asyncio.wait_for(awaitable, timeout=tool.timeout))
        except asyncio.TimeoutError:
            raise _timeout_error(tool.name, tool.timeout) from None

    @staticmethod
    def _finish(outcome: Any) -> ToolResult:
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult.ok(outcome if isinstance(outcome, str) else repr(outcome))

    @staticmethod
    def _timed(result: ToolResult, started: float) -> ToolResult:
        result.duration_ms = round((time.monotonic() - started) * 1000, 3)
        return result

    # -- batch dispatch ------------------------------------------------------------

    async def invoke_batch(
        self,
        calls: Sequence[Tuple[str, str, Mapping[str, Any]]],
        *,
        max_parallel: int = 4,
    ) -> List[Tuple[str, ToolResult]]:
        """Run several calls concurrently, preserving input order in the result.

        ``calls`` is ``(call_id, tool_name, arguments)``.  The semaphore is what makes
        "the model asked for 12 reads" not become 12 simultaneous file handles.
        """
        semaphore = asyncio.Semaphore(max(1, max_parallel))

        async def run(call_id: str, name: str, args: Mapping[str, Any]) -> Tuple[str, ToolResult]:
            async with semaphore:
                # `invoke` (not `execute`): a batch caller has not consulted the policy,
                # so the registry must.
                return call_id, await self.invoke(name, args)

        tasks = [asyncio.ensure_future(run(cid, name, args)) for cid, name, args in calls]
        if not tasks:
            return []
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[Tuple[str, ToolResult]] = []
        for (call_id, name, _args), outcome in zip(calls, gathered):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                result = ToolResult.error("{}: {}".format(type(outcome).__name__, outcome))
                result.tool = name
                out.append((call_id, result))
            else:
                out.append(outcome)
        return out


def _invoke_bounded(handler: ToolHandler, args: Dict[str, Any], timeout: Optional[float], name: str) -> ToolResult:
    """Call a sync handler with a bounded wait.  Runs in a worker thread.

    The handler itself runs in a *second* daemon thread so this function can give up at
    the deadline.  Python cannot kill a thread, so a timed-out handler is abandoned rather
    than stopped: the honest trade is that the model is told it timed out and the orphan
    is expected to be self-limiting (``run_program`` gives its own subprocess a deadline
    for exactly this reason).  A daemon thread cannot keep the interpreter alive.
    """
    if timeout is None:
        return _finish_static(handler(**args))
    outcome: "queue.Queue[Any]" = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            outcome.put((True, handler(**args)))
        except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
            outcome.put((False, exc))

    worker = threading.Thread(target=target, name="pyto-tool-{}".format(name), daemon=True)
    worker.start()
    # A timer rather than queue.get(timeout=...) so the socket/queue is never left in a
    # partially-read state, and so the result is still collected if it lands on the
    # deadline.
    timer = threading.Timer(timeout, outcome.put, args=((False, _TOOL_TIMEOUT),))
    timer.daemon = True
    timer.start()
    try:
        ok, payload = outcome.get()
    finally:
        timer.cancel()
    if not ok and payload is _TOOL_TIMEOUT:
        raise _timeout_error(name, timeout)
    if not ok:
        raise payload
    return _finish_static(payload)


class _Timeout(Exception):
    """Sentinel payload used to distinguish a timeout from a handler exception."""


_TOOL_TIMEOUT = _Timeout()


def _timeout_error(name: str, timeout: Optional[float]) -> ToolTimeoutError:
    return ToolTimeoutError(
        "{} timed out after {}s and was abandoned; narrow the request or raise the "
        "tool timeout".format(name, timeout)
    )


def _finish_static(outcome: Any) -> ToolResult:
    if isinstance(outcome, ToolResult):
        return outcome
    return ToolResult.ok(outcome if isinstance(outcome, str) else repr(outcome))


def build_policy(
    allow: Sequence[str] = (),
    deny: Sequence[str] = (),
    *,
    prompter: Optional[Callable[[str, Mapping[str, Any], str], bool]] = None,
) -> PolicyFn:
    """Compose a policy from allow/deny lists plus an optional interactive prompt.

    Evaluation order: explicit deny, then explicit allow, then the prompt.  A tool that
    appears in neither list and has no prompter is **denied** — an unknown tool must not
    default to allowed when the consequence is sharing the user's data.
    """
    allow_set = set(allow)
    deny_set = set(deny)

    def policy(name: str, arguments: Mapping[str, Any]) -> Decision:
        if name in deny_set:
            return Decision.deny("{} is on the deny list".format(name))
        if name in allow_set:
            return Decision.allow()
        if prompter is None:
            return Decision.deny("{} is not on the allow list".format(name))
        granted = prompter(name, arguments, "not auto-approved")
        return Decision.allow() if granted else Decision.deny("the user declined")

    return policy

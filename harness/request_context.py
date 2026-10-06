"""Bound provider request size independently from durable session-log limits.

The session remains the complete local record. Before each provider call this module keeps
the newest complete conversation turns, trims prior result text only when one turn itself
is too large, and never truncates the current user request or separates a tool call from
its result.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

# This request-size budget is independent from JSONL event count/bytes and single tool
# result limits. It includes serialized messages and tool schemas; it is a conservative
# byte proxy, not a provider-specific token counter.
MAX_PROVIDER_REQUEST_BYTES = 128 * 1024
_CONTEXT_NOTICE = (
    "Earlier complete chat turns are omitted from this provider request to fit its context "
    "budget; they remain in the local session log. If working on a saved program, use its "
    "versioned project brief and current workspace files for durable requirements. If a "
    "necessary requirement is missing from both, ask the user instead of guessing."
)
_TRIM_NOTICE = "\n\n[older content shortened for request context; the complete event remains in the local session log]\n\n"
_MIN_CONTENT_CHARS = 128


class RequestContextError(ValueError):
    """The current request cannot fit without dropping user intent or tool structure."""


@dataclass
class BoundedRequest:
    messages: List[Dict[str, Any]]
    payload_bytes: int
    omitted_turns: int = 0
    trimmed_messages: int = 0


def estimate_request_bytes(
    messages: Sequence[Mapping[str, Any]], tools: Optional[Sequence[Mapping[str, Any]]] = None
) -> int:
    """Estimate serialized messages plus tool schemas in the provider request body."""
    body = {"messages": list(messages), "tools": list(tools or [])}
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _turn_groups(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
    systems = [copy.deepcopy(dict(message)) for message in messages if message.get("role") == "system"]
    conversation = [copy.deepcopy(dict(message)) for message in messages if message.get("role") != "system"]
    groups: List[List[Dict[str, Any]]] = []
    prefix: List[Dict[str, Any]] = []
    for message in conversation:
        if message.get("role") == "user":
            groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            prefix.append(message)
    if prefix:
        if groups:
            groups[0] = prefix + groups[0]
        else:
            groups.append(prefix)
    return systems, groups


def _assemble(
    systems: Sequence[Mapping[str, Any]],
    groups: Sequence[Sequence[Mapping[str, Any]]],
    start: int,
    *,
    notice: bool,
) -> List[Dict[str, Any]]:
    output = [copy.deepcopy(dict(message)) for message in systems]
    if notice:
        if output:
            content = output[0].get("content", "")
            output[0]["content"] = (content + "\n\n" + _CONTEXT_NOTICE) if isinstance(content, str) else _CONTEXT_NOTICE
        else:
            output.append({"role": "system", "content": _CONTEXT_NOTICE})
    output.extend(copy.deepcopy(dict(message)) for group in groups[start:] for message in group)
    return output


def _clip(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    room = max(0, maximum - len(_TRIM_NOTICE))
    head = (room * 2) // 3
    tail = room - head
    return text[:head] + _TRIM_NOTICE + (text[-tail:] if tail else "")


def _validate_tool_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    pending = set()
    for message in messages:
        role = message.get("role")
        if role == "user" and pending:
            raise RequestContextError("provider history contains a tool call without all of its results")
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                call_id = call.get("id") if isinstance(call, Mapping) else None
                if not isinstance(call_id, str) or not call_id:
                    raise RequestContextError("provider history contains a tool call without a valid id")
                if call_id in pending:
                    raise RequestContextError("provider history contains a duplicate tool call id")
                pending.add(call_id)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise RequestContextError("provider history contains an orphaned tool result")
            pending.remove(call_id)
    if pending:
        raise RequestContextError("provider history contains a tool call without its result")


def bound_provider_request(
    messages: Sequence[Mapping[str, Any]],
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    max_bytes: int = MAX_PROVIDER_REQUEST_BYTES,
) -> BoundedRequest:
    """Keep the newest complete turns and bound old result text without losing new intent."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise RequestContextError("provider request context budget must be a positive byte count")
    systems, groups = _turn_groups(messages)
    tool_schemas = list(tools or [])

    full = _assemble(systems, groups, 0, notice=False)
    size = estimate_request_bytes(full, tool_schemas)
    if size <= max_bytes:
        _validate_tool_pairs(full)
        return BoundedRequest(full, size)

    # Omit only whole older user turns. A tool declaration and its results stay together
    # because the complete turn group is retained or discarded as a unit.
    for start in range(1, len(groups)):
        candidate = _assemble(systems, groups, start, notice=True)
        size = estimate_request_bytes(candidate, tool_schemas)
        if size <= max_bytes:
            _validate_tool_pairs(candidate)
            return BoundedRequest(candidate, size, omitted_turns=start)

    # The newest turn plus system/tool definitions still does not fit. Preserve its user
    # request and every tool-call/result envelope while shortening prior assistant/tool
    # content with an explicit notice. Never silently truncate user-authored text.
    omitted = max(0, len(groups) - 1)
    latest = copy.deepcopy(groups[-1]) if groups else []
    trimmed = set()
    while True:
        candidate = _assemble(systems, [latest], 0, notice=bool(omitted))
        size = estimate_request_bytes(candidate, tool_schemas)
        if size <= max_bytes:
            _validate_tool_pairs(candidate)
            return BoundedRequest(candidate, size, omitted_turns=omitted, trimmed_messages=len(trimmed))

        candidates = [
            (len(str(message.get("content", ""))), index)
            for index, message in enumerate(latest)
            if message.get("role") != "user"
            and isinstance(message.get("content"), str)
            and len(message["content"]) > _MIN_CONTENT_CHARS
        ]
        if not candidates:
            raise RequestContextError(
                "The current user request and required system/tool definitions exceed the provider context budget. "
                "No request was sent and the complete session remains saved; shorten the request or raise the configured budget."
            )
        _length, index = max(candidates)
        content = latest[index]["content"]
        new_limit = max(_MIN_CONTENT_CHARS, int(len(content) * 0.65))
        latest[index]["content"] = _clip(content, new_limit)
        trimmed.add(index)

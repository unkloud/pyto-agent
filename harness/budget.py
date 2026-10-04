"""Size budgets, because iOS kills the whole process rather than one allocation.

Pyto stops **every** running script once free memory drops to roughly 500 MB, so an
unbounded session log or a chatty program does not degrade the harness — it takes the
whole session down with it.  The constants here are the numbers all the other modules
agree on, and :func:`memory_pressure` / :func:`check_budget` are the two checks the loop
and the tools run before they append or write anything large.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

#: Stop appending to a session log after this many events; compact instead.
MAX_SESSION_EVENTS = 4000
#: Or after this many bytes on disk (~4 MB of JSONL).
MAX_SESSION_BYTES = 4 * 1024 * 1024
#: Events kept after a compaction.  Must be large enough to hold every assistant/tool
#: pair in the current turn, or the projection would drop a tool result mid-conversation.
KEEP_RECENT_EVENTS = 600
#: Warn (do not refuse) when free memory drops below this.
LOW_MEMORY_WARN_BYTES = 700 * 1024 * 1024
#: Hard stop for a single file write, so one tool call cannot exhaust the process.
MAX_WRITE_BYTES = 8 * 1024 * 1024


def log_bytes(path: Optional[str]) -> int:
    """Size of a file in bytes, or 0 when it does not exist yet."""
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def estimate_event_bytes(event: Any) -> int:
    """Approximate on-disk size of one event row."""
    try:
        return len(json.dumps(event.to_wire(), separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def memory_pressure() -> Dict[str, Any]:
    """Free-memory report from Pyto, when the interpreter exposes it."""
    from . import ios

    available = ios.available_memory_bytes()
    if available is None:
        return {"supported": False, "available_bytes": None, "warning": None}
    warning = None
    if available < LOW_MEMORY_WARN_BYTES:
        warning = (
            "only {:.0f} MB of memory is free; Pyto stops every script near 500 MB, so keep "
            "outputs small".format(available / (1024 * 1024))
        )
    return {"supported": True, "available_bytes": available, "warning": warning}


def check_budget(path: Optional[str], event_count: int) -> Optional[str]:
    """Return a reason to compact the session log, or ``None`` when it is still small."""
    if event_count >= MAX_SESSION_EVENTS:
        return "{} events (limit {})".format(event_count, MAX_SESSION_EVENTS)
    size = log_bytes(path)
    if size >= MAX_SESSION_BYTES:
        return "{:.1f} MB on disk (limit {:.1f} MB)".format(
            size / (1024 * 1024), MAX_SESSION_BYTES / (1024 * 1024)
        )
    return None


def check_write_size(size: int) -> Optional[str]:
    """Return a reason to refuse a write, or ``None`` when it fits the budget."""
    if size > MAX_WRITE_BYTES:
        return "the content is {:.1f} MB, above the {:.1f} MB single-write limit".format(
            size / (1024 * 1024), MAX_WRITE_BYTES / (1024 * 1024)
        )
    return None


def first_incomplete_turn_index(events: Iterable[Any]) -> Optional[int]:
    """Index of the oldest event that belongs to the conversation's current tail.

    Compaction must never cut between an assistant message that declares tool calls and
    the tool messages that answer them: the provider rejects an orphaned tool result, and
    a tool result without its declaration is silently dropped by the projection.  So the
    keep-window is advanced until it starts at a *user* message.
    """
    collected: List[Any] = list(events)
    for index, event in enumerate(collected):
        if getattr(event, "type", None) == "message.user":
            return index
    return None

"""Tool-result truncation with spill-to-file.

A tool that returns 4 MB of stdout must not enter the conversation.  The policy is
head + tail with an explicit notice in the middle naming the spill file, so the
model knows the full output exists and can ask for a slice of it (``read_file``
with an offset) instead of silently reasoning over half a log.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from .security import mkdir_private, scrub_secrets, write_private

#: Loop-level default budget for a single tool result, in characters.
DEFAULT_MAX_CHARS = 12000
#: Fraction of the budget given to the head; the tail gets the rest.
HEAD_RATIO = 0.6

DEFAULT_SPILL_DIR = "tool-output"


@dataclass
class Truncated:
    """The clamped text plus what was removed."""

    text: str
    truncated: bool
    full_chars: int
    spill_path: Optional[str] = None


def truncate_middle(
    text: str,
    limit: int = DEFAULT_MAX_CHARS,
    *,
    spill_dir: Optional[str] = None,
    spill_name: str = "output",
    label: str = "tool output",
) -> Truncated:
    """Clamp ``text`` to ``limit`` chars, keeping both ends and spilling the whole body.

    When ``spill_dir`` is given (and writable) the *complete* text is written there and
    the notice tells the model the path.  When it is not, the notice says so instead of
    pretending a path exists.

    The text is scrubbed **before** it is clamped, so neither the model-visible copy nor
    the spill file (which is the whole, untruncated body) can carry a credential that a
    program or a provider error echoed back.
    """
    text = scrub_secrets(text)
    if limit <= 0 or len(text) <= limit:
        return Truncated(text=text, truncated=False, full_chars=len(text))

    path = _spill(text, spill_dir, spill_name) if spill_dir else None
    head_chars = int(limit * HEAD_RATIO)
    tail_chars = limit - head_chars
    omitted = len(text) - head_chars - tail_chars
    where = (
        "the full {:,}-character body was saved to {}".format(len(text), path)
        if path
        else "the full {:,}-character body was NOT saved (no spill file available)".format(len(text))
    )
    notice = (
        "\n\n... [{} truncated: {} characters omitted; {}; "
        "re-run with a narrower request to see the missing part] ...\n\n".format(
            label, omitted, where
        )
    )
    clamped = text[:head_chars] + notice + (text[-tail_chars:] if tail_chars else "")
    return Truncated(text=clamped, truncated=True, full_chars=len(text), spill_path=path)


def _spill(text: str, spill_dir: str, spill_name: str) -> Optional[str]:
    """Best-effort write of the full text; returns the path or ``None``.

    Spilling must never break a turn: a full disk or a read-only container turns into a
    missing path in the notice, not an exception out of the tool layer.  The spill file is
    created ``0600`` in a ``0700`` directory: it holds the complete output the model was
    not allowed to see, which is exactly the material a hostile app would want.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in spill_name)[:60] or "output"
    try:
        mkdir_private(spill_dir)
        path = os.path.join(spill_dir, "{}-{}.txt".format(safe, int(time.time() * 1000)))
        write_private(path, scrub_secrets(text))
        return path
    except OSError:  # pragma: no cover - depends on the host filesystem
        return None

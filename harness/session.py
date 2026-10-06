"""Append-only JSONL session log: versioned header, append, resume, projection.

On-disk format, one JSON object per line, UTF-8, ``\\n`` terminated::

    {"kind":"header","version":1,"id":"...","created_at":1712345678901,"workspace":"/..."}
    {"kind":"event","seq":0,"time":1712345678902,"type":"message.user","data":{...}}
    {"kind":"event","seq":1,"time":1712345678903,"type":"message.assistant","data":{...}}
    ...

Why this shape, for a phone: iOS kills backgrounded apps without warning, so the log
is the only durable state.  One writer, append only, ``fsync`` on every event — a kill
at any instant leaves a valid prefix, and the next launch resumes from it.  Nothing is
ever rewritten, so there is no window in which the file is half-updated.

* **Versioned header.** The first row carries ``version``; a newer version is refused
  rather than guessed at.
* **Torn tail recovery.** A partially written final line is dropped with a warning
  instead of raising, because that is exactly what an iOS kill produces.
* **Projection is a pure function of the event list**, which is what makes replay
  deterministic and resume identical to never having been interrupted.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError, SessionFormatError
from .home import WORKAROUND, expand_user_path
from .security import mkdir_private, open_private, scrub_value

CURRENT_VERSION = 1
HEADER_KIND = "header"
EVENT_KIND = "event"

#: The provider protocol shape of a projected message.  Recorded next to every
#: projected message so a future change can migrate instead of mis-reading.
PROJECTION_SHAPE = "chat-completions.v1"

#: Event types that project back into the conversation sent to the model.
PROJECTED_TYPES = frozenset({"message.user", "message.assistant", "message.tool"})

#: Event types that exist only for the UI / diagnostics and never project.
TELEMETRY_TYPES = frozenset(
    {
        "session.started",
        "session.resumed",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.limit",
        "usage",
        "tool.intent",
        "tool.started",
        "tool.completed",
        "tool.denied",
        "tool.approval",
        "delta",
        "reasoning.delta",
        "error",
        "finish",
    }
)

_SECRET_MARKERS = ("api_key", "apikey", "authorization", "token", "secret", "password")


def now_ms() -> int:
    return int(time.time() * 1000)


def redact(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively replace credential-looking values.  Applied before anything hits disk.

    Key-name based (``api_key``, ``token``, …) *and* value-shape based, so a bare secret
    that arrived under an innocent key name is scrubbed too.
    """
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
            out[key] = "<redacted>"
        elif isinstance(value, Mapping):
            out[key] = redact(value)
        elif isinstance(value, list):
            out[key] = [redact(v) if isinstance(v, Mapping) else scrub_value(v) for v in value]
        else:
            out[key] = scrub_value(value)
    return out


@dataclass
class SessionHeader:
    """Logical session metadata, written once as the first row."""

    version: int = CURRENT_VERSION
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: int = field(default_factory=now_ms)
    workspace: str = ""
    model: str = ""
    api_base: str = ""
    #: Free-form, redacted before it reaches disk.
    config: Dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> Dict[str, Any]:
        wire: Dict[str, Any] = {
            "kind": HEADER_KIND,
            "version": self.version,
            "id": self.id,
            "created_at": self.created_at,
            "workspace": self.workspace,
            "model": self.model,
        }
        if self.api_base:
            wire["api_base"] = self.api_base
        if self.config:
            wire["config"] = redact(self.config)
        return wire

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> "SessionHeader":
        version = wire.get("version")
        if not isinstance(version, int):
            raise SessionFormatError("session header is missing an integer `version`")
        if version > CURRENT_VERSION:
            raise SessionFormatError(
                "session version {} is newer than this build supports ({})".format(version, CURRENT_VERSION)
            )
        while version < CURRENT_VERSION:
            # Adjacent migrations only.  v0 used `cwd` and had no `model`.
            if version == 0:
                wire = dict(wire)
                wire["workspace"] = wire.get("workspace", wire.get("cwd", ""))
                wire["model"] = wire.get("model", "")
                version = 1
            else:  # pragma: no cover - no other historical versions exist yet
                raise SessionFormatError("no migration from session version {}".format(version))
        return cls(
            version=CURRENT_VERSION,
            id=str(wire.get("id") or uuid.uuid4().hex),
            created_at=int(wire.get("created_at") or now_ms()),
            workspace=str(wire.get("workspace") or ""),
            model=str(wire.get("model") or ""),
            api_base=str(wire.get("api_base") or ""),
            config=dict(wire.get("config") or {}),
        )


@dataclass
class SessionEvent:
    seq: int
    type: str
    time: int
    data: Dict[str, Any]

    def to_wire(self) -> Dict[str, Any]:
        return {"kind": EVENT_KIND, "seq": self.seq, "time": self.time, "type": self.type, "data": self.data}

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> "SessionEvent":
        seq = wire.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise SessionFormatError("event row is missing an integer `seq`")
        kind = wire.get("type")
        if not isinstance(kind, str):
            raise SessionFormatError("event row is missing a string `type`")
        data = wire.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SessionFormatError("event {} has a non-object `data`".format(seq))
        return cls(seq=seq, type=kind, time=int(wire.get("time") or now_ms()), data=data)


# --------------------------------------------------------------------------------------
# Event payload helpers (the shapes the loop appends)
# --------------------------------------------------------------------------------------


def user_message_event(text: str) -> Dict[str, Any]:
    return {"message": {"role": "user", "content": text}, "projectionShape": PROJECTION_SHAPE}


def assistant_message_event(message: Mapping[str, Any]) -> Dict[str, Any]:
    return {"message": dict(message), "projectionShape": PROJECTION_SHAPE}


def tool_message_event(
    message: Mapping[str, Any], *, recovery: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    event = {"message": dict(message), "projectionShape": PROJECTION_SHAPE}
    if recovery is not None:
        event["recovery"] = dict(recovery)
    return event


def completed_tool_event(message: Mapping[str, Any], **details: Any) -> Dict[str, Any]:
    """Persist a tool result and its provider message in one durable event."""
    return {"message": dict(message), "projectionShape": PROJECTION_SHAPE, **details}


# --------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------


def _prune_orphan_tool_messages(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop ``role=tool`` messages that no preceding assistant turn declared.

    A tool result without its ``tool_calls`` declaration is a hard 400 from every
    OpenAI-compatible provider.  This happens legitimately: the app can be killed
    between writing the assistant row and the tool rows.
    """
    declared = set()
    out: List[Dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                if isinstance(call, Mapping):
                    declared.add(call.get("id"))
        if message.get("role") == "tool" and message.get("tool_call_id") not in declared:
            continue
        out.append(message)
    return out


def project(events: Iterable[SessionEvent]) -> List[Dict[str, Any]]:
    """Pure projection: events -> provider messages.  Deterministic by construction.

    New tool results live in their ``tool.completed`` event so the result and completion
    marker cannot be separated by an app kill. Older logs stored a separate
    ``message.tool`` event. Associate either form with its assistant declaration and place
    it immediately after that declaration in provider history.
    """
    accepted = list(events)
    results: Dict[Tuple[int, int], Dict[str, Any]] = {}
    assistant_messages: Dict[int, Dict[str, Any]] = {}

    for event in accepted:
        if event.type != "message.assistant" or event.data.get("projectionShape") != PROJECTION_SHAPE:
            continue
        message = event.data.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            assistant_messages[event.seq] = message

    # Match each result to the earliest preceding declaration for that id. Provider call
    # ids are expected to be unique, but this remains well-defined for old or synthetic
    # logs that reuse one.
    pending: Dict[str, List[Tuple[int, int]]] = {}
    result_events: Dict[int, List[Tuple[str, Dict[str, Any]]]] = {}
    for event in accepted:
        message = None
        if event.type in ("message.tool", "tool.completed", "tool.reconciled"):
            if event.data.get("projectionShape") == PROJECTION_SHAPE:
                candidate = event.data.get("message")
                if isinstance(candidate, dict) and candidate.get("role") == "tool":
                    message = candidate
        if message is None:
            continue
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            result_events.setdefault(event.seq, []).append((call_id, message))

    for event in accepted:
        if event.type == "message.assistant" and event.seq in assistant_messages:
            for index, call in enumerate(assistant_messages[event.seq].get("tool_calls") or ()):
                if not isinstance(call, Mapping):
                    continue
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id:
                    pending.setdefault(call_id, []).append((event.seq, index))
        for call_id, message in result_events.get(event.seq, ()):
            waiting = pending.get(call_id) or []
            if waiting:
                results[waiting.pop(0)] = message

    projected: List[Dict[str, Any]] = []
    for event in accepted:
        if event.type not in ("message.user", "message.assistant"):
            continue
        if event.data.get("projectionShape") != PROJECTION_SHAPE:
            continue
        message = event.data.get("message")
        if not isinstance(message, dict) or "role" not in message:
            continue
        # Deep copy via JSON so a caller mutating the result cannot corrupt the log.
        copied = json.loads(json.dumps(message))
        projected.append(copied)
        if message.get("role") == "assistant":
            for index, call in enumerate(message.get("tool_calls") or ()):
                if not isinstance(call, Mapping):
                    continue
                result = results.get((event.seq, index))
                if result is not None:
                    projected.append(json.loads(json.dumps(result)))
    return _prune_orphan_tool_messages(projected)


# --------------------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------------------


class SessionLog:
    """Append-only writer/reader for one JSONL session file."""

    def __init__(
        self,
        header: SessionHeader,
        events: Optional[List[SessionEvent]] = None,
        path: Optional[str] = None,
    ) -> None:
        self.header = header
        self._events: List[SessionEvent] = list(events or [])
        self._next_seq = (self._events[-1].seq + 1) if self._events else 0
        self._handle = None  # type: ignore[var-annotated]
        #: Remembered separately from the handle so `path` still answers after close().
        self._path = path
        self.warnings: List[str] = []

    # -- lifecycle -----------------------------------------------------------------

    @classmethod
    def create(
        cls,
        path: str,
        *,
        workspace: str = "",
        header: Optional[SessionHeader] = None,
        config: Optional[Mapping[str, Any]] = None,
    ) -> "SessionLog":
        resolved = _resolve(path)
        header = header or SessionHeader(workspace=workspace)
        if config:
            header.config.update(config)
        try:
            mkdir_private(os.path.dirname(resolved) or ".")
        except OSError as exc:
            # EPERM/EACCES/EROFS on the sessions directory: say what to do about it
            # instead of surfacing a bare errno (this is the Pyto no-home failure shape).
            raise ConfigError(
                "sessions directory {} could not be created: {}: {}.\n{}".format(
                    os.path.dirname(resolved) or ".", type(exc).__name__, exc, WORKAROUND
                )
            ) from exc
        log = cls(header, path=resolved)
        log._handle = open_private(resolved, append=True)
        log._append_row(header.to_wire())
        log._flush(sync=True)
        return log

    @classmethod
    def resume(cls, path: str, *, writable: bool = True) -> "SessionLog":
        resolved = _resolve(path)
        if not os.path.exists(resolved):
            raise FileNotFoundError(resolved)
        header, events, warnings = read_log(resolved)
        log = cls(header, events, path=resolved)
        log.warnings.extend(warnings)
        if writable:
            # 0600 at creation and tightened if an older run (or another tool) left the
            # log group-readable.  A chmod that cannot happen raises instead of leaving
            # the session readable by everything on the device.
            log._handle = open_private(resolved, append=True)
        return log

    @classmethod
    def open_or_create(cls, path: str, **kwargs: Any) -> "SessionLog":
        """Resume when the file exists, create otherwise.  This is the CLI behaviour."""
        resolved = _resolve(path)
        if os.path.exists(resolved) and os.path.getsize(resolved) > 0:
            return cls.resume(resolved)
        return cls.create(resolved, **kwargs)

    @property
    def path(self) -> Optional[str]:
        if self._path is not None:
            return self._path
        return getattr(self._handle, "name", None)

    @property
    def events(self) -> List[SessionEvent]:
        return list(self._events)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                os.fsync(self._handle.fileno())
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> "SessionLog":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- append --------------------------------------------------------------------

    def append(self, type: str, data: Optional[Mapping[str, Any]] = None, *, time: Optional[int] = None) -> SessionEvent:
        """Append one event and return it with its assigned ``seq``.

        ``time`` exists so a log can be replayed into a new file with the original
        timestamps preserved (see ``test_replay_is_stable``).
        """
        if self._handle is None:
            raise SessionFormatError("session log is not open for writing")
        seq = self._next_seq
        event = SessionEvent(
            seq=seq,
            type=type,
            time=now_ms() if time is None else int(time),
            # Every row is scrubbed by shape on the way in: a tool result that echoed the
            # key, or a provider error body that quoted it, must not become durable state.
            data=scrub_value(dict(data or {})),
        )
        self._append_row(event.to_wire())
        self._flush(sync=True)
        self._events.append(event)
        self._next_seq = seq + 1
        return event

    def _append_row(self, row: Mapping[str, Any]) -> None:
        line = json.dumps(row, separators=(",", ":"), ensure_ascii=False, sort_keys=False)
        self._handle.write(line + "\n")  # type: ignore[union-attr]

    def _flush(self, *, sync: bool) -> None:
        self._handle.flush()  # type: ignore[union-attr]
        if sync:
            try:
                os.fsync(self._handle.fileno())  # type: ignore[union-attr]
            except OSError:  # pragma: no cover - some iOS paths refuse fsync
                pass

    # -- projection ----------------------------------------------------------------

    def project(self, *, up_to: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rebuild the provider message list.  Pure in the accepted event slice."""
        accepted = self._events if up_to is None else [e for e in self._events if e.seq <= up_to]
        return project(accepted)

    def reconcile_tool_calls(self) -> List[Dict[str, str]]:
        """Close interrupted assistant tool calls without replaying their handlers.

        A durable ``tool.started`` event means the handler may have made a side effect.
        Without a durable result, that outcome is unknown. A declaration with no start
        record was never launched. Both cases get a provider-valid tool message, persisted
        as one event so another interruption cannot repeat the reconciliation.
        """
        pending: Dict[str, Dict[str, Any]] = {}
        for event in self._events:
            if event.type == "message.assistant":
                if event.data.get("projectionShape") != PROJECTION_SHAPE:
                    continue
                assistant = event.data.get("message")
                if not isinstance(assistant, Mapping):
                    continue
                for call in assistant.get("tool_calls") or ():
                    if not isinstance(call, Mapping):
                        continue
                    call_id = call.get("id")
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, Mapping) else "tool"
                    if isinstance(call_id, str) and call_id:
                        pending[call_id] = {
                            "name": str(name or "tool"),
                            "started": False,
                            "denied": "",
                            "completion_without_result": False,
                        }
            elif event.type == "tool.started":
                call_id = event.data.get("id")
                if isinstance(call_id, str) and call_id in pending:
                    pending[call_id]["started"] = True
            elif event.type == "tool.denied":
                call_id = event.data.get("id")
                if isinstance(call_id, str) and call_id in pending:
                    pending[call_id]["denied"] = str(event.data.get("reason") or "")
            elif event.type in ("message.tool", "tool.completed", "tool.reconciled"):
                message = event.data.get("message")
                call_id = None
                if isinstance(message, Mapping) and message.get("role") == "tool":
                    call_id = message.get("tool_call_id")
                if call_id is None:
                    call_id = event.data.get("id")
                if isinstance(call_id, str) and call_id in pending:
                    if isinstance(message, Mapping) and message.get("role") == "tool":
                        pending.pop(call_id, None)
                    elif event.type == "tool.completed":
                        pending[call_id]["completion_without_result"] = True

        reconciled: List[Dict[str, str]] = []
        for call_id, record in pending.items():
            name = str(record["name"])
            denied = str(record["denied"] or "")
            if denied and not record["started"]:
                state = "denied"
                content = "Not run: approval was denied. The action was not replayed after the interruption."
                message = "The interrupted {} action was denied and was not run.".format(name.replace("_", " "))
            elif record["started"] or record["completion_without_result"]:
                state = "unknown"
                content = (
                    "Outcome unknown: Pyto stopped after recording that this operation was starting, "
                    "but before saving its result. It was not run again to avoid duplicating a side effect. "
                    "Check the device or workspace for its effects before deciding whether to retry."
                )
                message = (
                    "After interruption, the outcome of {} is unknown. It was not run again; check the "
                    "device or workspace for effects before retrying."
                ).format(name.replace("_", " "))
            else:
                state = "not_started"
                content = (
                    "Not run: Pyto stopped before this operation was launched. It was not replayed. "
                    "Send a new request if you still want it."
                )
                message = "After interruption, {} had not started and was not run. Send a new request if you still want it.".format(
                    name.replace("_", " ")
                )
            self.append(
                "message.tool",
                tool_message_event(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": content,
                    },
                    recovery={"state": state, "reason": denied},
                ),
            )
            reconciled.append({"tool": name, "state": state, "message": message})
        return reconciled

    def replay(self) -> Dict[str, Any]:
        """Deterministic digest of the log, used by tests and resume diagnostics."""
        digest = hashlib.sha256()
        for event in self._events:
            digest.update(json.dumps(event.to_wire(), separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return {
            "events": len(self._events),
            "messages": len(self.project()),
            "last_seq": self._events[-1].seq if self._events else -1,
            "digest": digest.hexdigest(),
        }

    def to_jsonl(self) -> str:
        """The whole log re-serialised, for byte-comparison in tests."""
        rows = [json.dumps(self.header.to_wire(), separators=(",", ":"), ensure_ascii=False, sort_keys=False)]
        rows.extend(
            json.dumps(e.to_wire(), separators=(",", ":"), ensure_ascii=False, sort_keys=False)
            for e in self._events
        )
        return "\n".join(rows) + "\n"


    # -- compaction ----------------------------------------------------------------

    def compact(
        self, *, keep_recent: Optional[int] = None, force: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Trim old events, atomically, and return a summary of what happened.

        Pyto stops every script near 500 MB of free memory, so an unbounded log is a
        crash, not a slow leak.  Compaction rewrites the file through a temporary file and
        ``os.replace`` (atomic on the same filesystem), so a kill mid-compaction leaves
        the previous log intact rather than a half-written one.

        The keep-window is moved forward to the first ``message.user`` inside it, because
        starting it mid-turn would either orphan a tool result (a provider 400) or drop a
        tool message the model is still waiting on.
        """
        from .budget import KEEP_RECENT_EVENTS, check_budget, first_incomplete_turn_index

        keep = KEEP_RECENT_EVENTS if keep_recent is None else int(keep_recent)
        budget_reason = check_budget(self.path, len(self._events))
        if budget_reason is None and not force:
            return None
        reason = budget_reason or "forced compaction (keep_recent={})".format(keep)
        if len(self._events) <= keep:
            return None
        start = len(self._events) - keep
        tail = self._events[start:]
        offset = first_incomplete_turn_index(tail)
        if offset is None:
            return None  # nothing safe to cut (no user message in the window)
        kept = tail[offset:]
        dropped = len(self._events) - len(kept)
        if dropped <= 0:
            return None
        path = self.path
        if path is None:
            return None
        temporary = path + ".compact"
        try:
            with open_private(temporary, truncate=True) as handle:
                handle.write(
                    json.dumps(self.header.to_wire(), separators=(",", ":"), ensure_ascii=False, sort_keys=False)
                    + "\n"
                )
                for index, event in enumerate(kept):
                    row = dict(event.to_wire())
                    row["seq"] = index  # renumber so seq stays contiguous
                    handle.write(
                        json.dumps(row, separators=(",", ":"), ensure_ascii=False, sort_keys=False) + "\n"
                    )
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:  # pragma: no cover - some iOS paths refuse fsync
                    pass
            if self._handle is not None:
                self._handle.close()
            os.replace(temporary, path)
            if self._handle is not None:
                self._handle = open_private(path, append=True)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return None
        self._events = [
            SessionEvent(seq=index, type=event.type, time=event.time, data=event.data)
            for index, event in enumerate(kept)
        ]
        self._next_seq = len(self._events)
        summary = {
            "reason": reason,
            "dropped_events": dropped,
            "kept_events": len(self._events),
            "bytes_after": os.path.getsize(path) if os.path.exists(path) else 0,
        }
        self.warnings.append("compacted session log: {dropped_events} events dropped ({reason})".format(**summary))
        return summary


def _resolve(path: str) -> str:
    """A session path: expanded or refused, never a literal ``~`` directory."""
    return expand_user_path(path, what="session path")


def read_log(path: str) -> Tuple[SessionHeader, List[SessionEvent], List[str]]:
    """Parse a log file.  Returns ``(header, events, warnings)``; tolerates a torn tail."""
    warnings: List[str] = []
    header: Optional[SessionHeader] = None
    events: List[SessionEvent] = []
    expected = 0
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                if lineno == 1:
                    raise SessionFormatError("{}: header row is not JSON: {}".format(path, exc)) from exc
                warnings.append("dropped unparseable row {} (torn tail?): {}".format(lineno, exc))
                break
            if not isinstance(row, dict):
                raise SessionFormatError("{}: row {} is not a JSON object".format(path, lineno))
            kind = row.get("kind")
            if lineno == 1:
                if kind != HEADER_KIND:
                    raise SessionFormatError("{}: first row is not a session header (kind={!r})".format(path, kind))
                header = SessionHeader.from_wire(row)
                continue
            if kind != EVENT_KIND:
                warnings.append("skipped row {} with unknown kind {!r}".format(lineno, kind))
                continue
            try:
                event = SessionEvent.from_wire(row)
            except SessionFormatError as exc:
                warnings.append("dropped row {}: {}".format(lineno, exc))
                break
            if event.seq != expected:
                warnings.append("event seq {} at row {} does not follow {}".format(event.seq, lineno, expected - 1))
            expected = event.seq + 1
            events.append(event)
    if header is None:
        raise SessionFormatError("{}: no header row found".format(path))
    return header, events, warnings


def new_session_path(directory: str, *, label: str = "session") -> str:
    """A fresh, sortable session path inside ``directory``."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)[:32].strip("-") or "session"
    return os.path.join(
        expand_user_path(directory, what="sessions directory"), "{}-{}-{}.jsonl".format(stamp, safe, uuid.uuid4().hex[:6])
    )

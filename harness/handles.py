"""Session-scoped local data handles for capability-pipeline prototypes.

Handles keep file contents in a private workspace directory. Callers exchange only
opaque IDs and metadata; local transforms consume handles without returning contents to
the model. This module is not a sandbox and is not registered as a model-facing tool.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import secrets
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .security import chmod_private, mkdir_private, open_private

MAX_HANDLE_BYTES = 8 * 1024 * 1024
MAX_TEXT_TRANSFORM_BYTES = 1024 * 1024
OPERATION_STATES = frozenset(("ok", "failed", "cancelled", "indeterminate"))


@dataclass(frozen=True)
class DataHandle:
    """Public metadata for a session-owned artifact; never includes a file path."""

    handle_id: str
    type: str
    shape: Dict[str, Any]
    size_bytes: int
    preview_policy: str = "none"
    scope: str = "workspace-session"
    lifetime: str = "until-store-close"

    def to_dict(self) -> Dict[str, Any]:
        """Return JSON-friendly metadata without exposing artifact contents or paths."""
        return {
            "handle_id": self.handle_id,
            "type": self.type,
            "shape": dict(self.shape),
            "size_bytes": self.size_bytes,
            "preview_policy": self.preview_policy,
            "scope": self.scope,
            "lifetime": self.lifetime,
        }


@dataclass(frozen=True)
class OperationResult:
    """Outcome of an operation, separate from the state of any returned handle."""

    state: str
    handle: Optional[DataHandle] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in OPERATION_STATES:
            raise ValueError("unsupported operation state {!r}".format(self.state))
        if self.state == "ok" and self.error:
            raise ValueError("a successful operation cannot carry an error")
        if self.state != "ok" and not self.error:
            raise ValueError("a non-successful operation needs an error code")

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"state": self.state, "details": dict(self.details)}
        if self.handle is not None:
            result["handle"] = self.handle.to_dict()
        if self.error:
            result["error"] = self.error
        return result


class LocalHandleStore:
    """A temporary, workspace-confined store for a single local pipeline run.

    ``workspace`` is the harness :class:`Workspace` object. Handles are opaque outside
    this instance and are deleted by :meth:`close`; they are not written to session logs.
    """

    def __init__(self, workspace: Any, *, session_id: Optional[str] = None) -> None:
        self.workspace = workspace
        self.session_id = session_id or secrets.token_hex(8)
        directory_tag = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in self.session_id
        )[:16] or "session"
        parent = workspace.resolve(".pyto-handles")
        mkdir_private(parent)
        self._directory = tempfile.mkdtemp(prefix="{}-".format(directory_tag), dir=parent)
        if os.name == "posix":
            os.chmod(self._directory, 0o700)
        self._paths: Dict[str, str] = {}
        self._metadata: Dict[str, DataHandle] = {}
        self._closed = False

    def __enter__(self) -> "LocalHandleStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Delete this run's temporary artifacts and invalidate all its handles."""
        if self._closed:
            return
        shutil.rmtree(self._directory, ignore_errors=True)
        self._paths.clear()
        self._metadata.clear()
        self._closed = True

    def ingest_file(self, workspace_path: str) -> OperationResult:
        """Copy a workspace file into the private store and return metadata only."""
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        try:
            source = self.workspace.resolve(workspace_path, must_exist=True)
            if not os.path.isfile(source):
                return OperationResult("failed", error="source_not_a_file")
            size = os.path.getsize(source)
            if size > MAX_HANDLE_BYTES:
                return OperationResult("failed", error="file_exceeds_handle_size_limit")
            handle_id = secrets.token_urlsafe(18)
            target = os.path.join(self._directory, handle_id + ".data")
            with open(source, "rb") as source_file, open_private(
                target, exclusive=True, binary=True
            ) as target_file:
                shutil.copyfileobj(source_file, target_file, length=64 * 1024)
            if os.name == "posix":
                chmod_private(target)
            media_type, _encoding = mimetypes.guess_type(source)
            media_type = media_type or "application/octet-stream"
            shape: Dict[str, Any]
            try:
                with open(target, "rb") as artifact:
                    artifact.read().decode("utf-8")
                media_type = "text/plain"
                shape = {"kind": "text", "encoding": "utf-8"}
            except (UnicodeDecodeError, OSError):
                shape = {"kind": "binary", "media_type": media_type}
            handle = DataHandle(handle_id, media_type, shape, size)
            self._paths[handle_id] = target
            self._metadata[handle_id] = handle
            return OperationResult("ok", handle=handle)
        except FileNotFoundError:
            return OperationResult("failed", error="source_not_found")
        except (OSError, ValueError):
            return OperationResult("failed", error="source_read_failed")

    def transform_text(
        self, handle_id: str, transform: Callable[[str], str], *, output_shape: Optional[Dict[str, Any]] = None
    ) -> OperationResult:
        """Apply a local text transform and store its output without returning text."""
        source_handle = self._metadata.get(handle_id)
        source_path = self._paths.get(handle_id)
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        if source_handle is None or source_path is None:
            return OperationResult("failed", error="unknown_handle")
        if source_handle.type != "text/plain":
            return OperationResult("failed", error="handle_is_not_utf8_text")
        if source_handle.size_bytes > MAX_TEXT_TRANSFORM_BYTES:
            return OperationResult("failed", error="text_exceeds_transform_size_limit")
        try:
            with open(source_path, "rb") as source_file:
                text = source_file.read(MAX_TEXT_TRANSFORM_BYTES + 1).decode("utf-8")
            transformed = transform(text)
            if not isinstance(transformed, str):
                return OperationResult("failed", error="transform_must_return_text")
            payload = transformed.encode("utf-8")
            return self._store_bytes(
                payload,
                type_name="text/plain",
                shape=output_shape or {"kind": "text", "encoding": "utf-8"},
            )
        except UnicodeDecodeError:
            return OperationResult("failed", error="text_decode_failed")
        except OSError:
            return OperationResult("failed", error="handle_read_failed")
        except Exception:
            return OperationResult("failed", error="transform_failed")

    def write_file(self, handle_id: str, destination: str, *, overwrite: bool = False) -> OperationResult:
        """Materialize a handle inside the workspace using an atomic file replace."""
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        source = self._paths.get(handle_id)
        handle = self._metadata.get(handle_id)
        if source is None or handle is None:
            return OperationResult("failed", error="unknown_handle")
        try:
            target = self.workspace.resolve(destination)
            if os.path.isdir(target):
                return OperationResult("failed", error="destination_is_directory")
            if os.path.exists(target) and not overwrite:
                return OperationResult("failed", error="destination_exists")
            parent = os.path.dirname(target) or self.workspace.root
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".handle-write-", dir=parent)
            os.close(fd)
            try:
                with open(source, "rb") as source_file, open_private(
                    temporary, truncate=True, binary=True
                ) as target_file:
                    shutil.copyfileobj(source_file, target_file, length=64 * 1024)
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return OperationResult(
                "ok",
                handle=handle,
                details={"workspace_path": self.workspace.relative(target), "bytes_written": handle.size_bytes},
            )
        except (OSError, ValueError):
            return OperationResult("failed", error="destination_write_failed")

    def metadata(self, handle_id: str) -> Optional[DataHandle]:
        """Return metadata for a live handle, never its path or content."""
        return self._metadata.get(handle_id)

    def _store_bytes(self, payload: bytes, *, type_name: str, shape: Dict[str, Any]) -> OperationResult:
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        if len(payload) > MAX_HANDLE_BYTES:
            return OperationResult("failed", error="output_exceeds_handle_size_limit")
        handle_id = secrets.token_urlsafe(18)
        path = os.path.join(self._directory, handle_id + ".data")
        try:
            with open_private(path, exclusive=True, binary=True) as target_file:
                target_file.write(payload)
            if os.name == "posix":
                chmod_private(path)
        except OSError:
            return OperationResult("failed", error="handle_write_failed")
        handle = DataHandle(handle_id, type_name, dict(shape), len(payload))
        self._paths[handle_id] = path
        self._metadata[handle_id] = handle
        return OperationResult("ok", handle=handle)

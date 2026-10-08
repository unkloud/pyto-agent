"""Persistent, workspace-confined typed artifacts for local capability pipelines.

Handle metadata and operation outcomes remain separate. Closing a store releases the
current object's state but keeps artifacts for a later process/session to reopen.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .security import chmod_private, mkdir_private, open_private

MAX_HANDLE_BYTES = 8 * 1024 * 1024
MAX_TEXT_TRANSFORM_BYTES = 1024 * 1024
OPERATION_STATES = frozenset(("ok", "failed", "cancelled", "indeterminate"))
STORE_DIR = ".pyto-handles"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
HANDLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


@dataclass(frozen=True)
class DataHandle:
    """Public metadata for a persistent artifact; never includes its local file path."""

    handle_id: str
    type: str
    shape: Dict[str, Any]
    size_bytes: int
    preview_policy: str = "none"
    scope: str = "workspace"
    lifetime: str = "until-explicit-delete"

    def to_dict(self) -> Dict[str, Any]:
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
    """Outcome of one operation, stored separately from handle metadata."""

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
    """Durable artifact store rooted in a harness :class:`Workspace`.

    ``close`` does not delete data. Open a new instance with the same workspace to load
    stable IDs after a session restart. Call :meth:`delete` when an artifact's lifetime
    ends. The store is local persistence, not a security sandbox.
    """

    def __init__(self, workspace: Any, *, session_id: Optional[str] = None) -> None:
        self.workspace = workspace
        self.session_id = session_id or secrets.token_hex(8)  # retained for caller compatibility
        lexical_directory = os.path.join(workspace.root, STORE_DIR)
        if os.path.islink(lexical_directory):
            raise ValueError("handle store directory cannot be a symbolic link")
        self._directory = workspace.resolve(STORE_DIR)
        mkdir_private(self._directory)
        if os.name == "posix":
            os.chmod(self._directory, 0o700)
        self._manifest_path = os.path.join(self._directory, MANIFEST_NAME)
        self._paths: Dict[str, str] = {}
        self._metadata: Dict[str, DataHandle] = {}
        self._closed = False
        self._load_manifest()

    def __enter__(self) -> "LocalHandleStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release this object's caches while keeping persisted artifacts available."""
        self._paths.clear()
        self._metadata.clear()
        self._closed = True

    def _load_manifest(self) -> None:
        if os.path.islink(self._manifest_path):
            raise ValueError("handle manifest cannot be a symbolic link")
        if not os.path.exists(self._manifest_path):
            return
        try:
            if os.path.getsize(self._manifest_path) > 2_000_000:
                raise ValueError("handle manifest exceeds its size limit")
            with open(self._manifest_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("could not read handle manifest: {}".format(exc)) from exc
        if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
            raise ValueError("unsupported handle manifest version")
        records = payload.get("handles")
        if not isinstance(records, list):
            raise ValueError("handle manifest entries must be a list")
        for record in records:
            if not isinstance(record, dict):
                continue
            handle_id = record.get("handle_id")
            if not isinstance(handle_id, str) or not HANDLE_ID_RE.fullmatch(handle_id):
                continue
            filename = handle_id + ".data"
            path = os.path.join(self._directory, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            shape = record.get("shape")
            size = record.get("size_bytes")
            type_name = record.get("type")
            if not isinstance(shape, dict) or not isinstance(size, int) or size < 0 or not isinstance(type_name, str):
                continue
            try:
                if os.path.getsize(path) != size:
                    continue
            except OSError:
                continue
            metadata = DataHandle(
                handle_id=handle_id,
                type=type_name,
                shape=dict(shape),
                size_bytes=size,
                preview_policy=str(record.get("preview_policy", "none")),
                scope="workspace",
                lifetime="until-explicit-delete",
            )
            self._metadata[handle_id] = metadata
            self._paths[handle_id] = path

    def _persist_manifest(self) -> None:
        records = []
        for handle_id in sorted(self._metadata):
            handle = self._metadata[handle_id]
            record = handle.to_dict()
            record["artifact_file"] = handle_id + ".data"
            records.append(record)
        serialized = json.dumps(
            {"version": MANIFEST_VERSION, "handles": records},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary = self._manifest_path + ".{}.tmp".format(secrets.token_hex(8))
        try:
            with open_private(temporary, exclusive=True, truncate=False) as handle:
                handle.write(serialized)
            os.replace(temporary, self._manifest_path)
            if os.name == "posix":
                chmod_private(self._manifest_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _path(self, handle_id: str) -> Optional[str]:
        if self._closed:
            return None
        path = self._paths.get(handle_id)
        if path is None or os.path.islink(path) or not os.path.isfile(path):
            return None
        return path

    def ingest_file(self, workspace_path: str) -> OperationResult:
        """Copy a workspace file into the private persistent store and return metadata."""
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        try:
            source = self.workspace.resolve(workspace_path, must_exist=True)
            if not os.path.isfile(source):
                return OperationResult("failed", error="source_not_a_file")
            size = os.path.getsize(source)
            if size > MAX_HANDLE_BYTES:
                return OperationResult("failed", error="file_exceeds_handle_size_limit")
            handle_id = secrets.token_urlsafe(24)
            target = os.path.join(self._directory, handle_id + ".data")
            with open(source, "rb") as source_file, open_private(target, exclusive=True, binary=True) as target_file:
                shutil.copyfileobj(source_file, target_file, length=64 * 1024)
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
            result = self._store_metadata(handle_id, target, media_type, shape, size)
            if result.state != "ok":
                try:
                    os.unlink(target)
                except OSError:
                    pass
            return result
        except FileNotFoundError:
            return OperationResult("failed", error="source_not_found")
        except (OSError, ValueError):
            return OperationResult("failed", error="source_read_failed")

    def transform_text(
        self,
        handle_id: str,
        transform: Callable[[str], str],
        *,
        output_shape: Optional[Dict[str, Any]] = None,
    ) -> OperationResult:
        """Apply a local text transform and store its output without returning text."""
        source_handle = self._metadata.get(handle_id)
        source_path = self._path(handle_id)
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
            return self._store_bytes(
                transformed.encode("utf-8"),
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
        source = self._path(handle_id)
        handle = self._metadata.get(handle_id)
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
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
                with open(source, "rb") as source_file, open_private(temporary, truncate=True, binary=True) as target_file:
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
        if self._closed or self._path(handle_id) is None:
            return None
        return self._metadata.get(handle_id)

    def delete(self, handle_id: str) -> OperationResult:
        """Explicitly delete one artifact and its metadata."""
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        path = self._paths.get(handle_id)
        if path is None:
            return OperationResult("failed", error="unknown_handle")
        try:
            os.unlink(path)
            self._paths.pop(handle_id, None)
            handle = self._metadata.pop(handle_id, None)
            self._persist_manifest()
            return OperationResult("ok", handle=handle, details={"deleted": True})
        except OSError:
            return OperationResult("failed", error="handle_delete_failed")

    def _store_bytes(self, payload: bytes, *, type_name: str, shape: Dict[str, Any]) -> OperationResult:
        if self._closed:
            return OperationResult("failed", error="handle_store_closed")
        if len(payload) > MAX_HANDLE_BYTES:
            return OperationResult("failed", error="output_exceeds_handle_size_limit")
        handle_id = secrets.token_urlsafe(24)
        path = os.path.join(self._directory, handle_id + ".data")
        try:
            with open_private(path, exclusive=True, binary=True) as target_file:
                target_file.write(payload)
            result = self._store_metadata(handle_id, path, type_name, dict(shape), len(payload))
            if result.state != "ok":
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return result
        except (OSError, TypeError, ValueError):
            return OperationResult("failed", error="handle_write_failed")

    def _store_metadata(
        self, handle_id: str, path: str, type_name: str, shape: Dict[str, Any], size_bytes: int
    ) -> OperationResult:
        handle = DataHandle(handle_id, type_name, dict(shape), size_bytes)
        self._paths[handle_id] = path
        self._metadata[handle_id] = handle
        try:
            self._persist_manifest()
        except OSError:
            self._paths.pop(handle_id, None)
            self._metadata.pop(handle_id, None)
            return OperationResult("failed", error="handle_metadata_write_failed")
        return OperationResult("ok", handle=handle)

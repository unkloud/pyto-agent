"""Plan and apply a reviewed move of loose files into grouped subfolders.

Planning is read-only. ``apply_plan`` requires an explicit confirmation flag, rechecks the
entire plan before making changes, refuses collisions, and rolls back completed moves if a
later move fails.
"""

from __future__ import annotations

import errno
import os
import shutil
from typing import Any, Dict, List, Mapping

MAX_FILES = 500
MAX_GROUP_NAME_CHARS = 80
_LINK_UNSUPPORTED = {
    getattr(errno, "EPERM", 1),
    getattr(errno, "EACCES", 13),
    getattr(errno, "EXDEV", 18),
    getattr(errno, "EMLINK", 31),
    getattr(errno, "ENOSYS", 38),
    getattr(errno, "ENOTSUP", 95),
    getattr(errno, "EOPNOTSUPP", 95),
}


class OrganizerError(ValueError):
    """A plan is invalid, stale, or could not be applied safely."""


def _safe_group_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > MAX_GROUP_NAME_CHARS
        or value.strip() in (".", "..")
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise OrganizerError("Choose a short folder name without path separators.")
    return value.strip()


def _group_for(filename: str, group_by: str) -> str:
    if group_by == "extension":
        extension = os.path.splitext(filename)[1]
        return extension[1:].casefold() if extension else "No extension"
    if group_by == "first letter":
        stem = os.path.splitext(filename)[0]
        letter = next((character for character in stem if character.isalpha()), "#")
        return letter.upper()
    raise OrganizerError("Group by extension or first letter.")


def build_plan(
    folder: str,
    *,
    group_by: str = "extension",
    collection_name: str = "Organized",
    max_files: int = 200,
) -> Dict[str, Any]:
    """Return a deterministic move plan without creating folders or changing files."""
    root = os.path.realpath(os.path.expanduser(folder))
    if not os.path.isdir(root) or not os.access(root, os.R_OK | os.X_OK):
        raise OrganizerError("The selected folder is missing or cannot be read: {}".format(folder))
    if group_by not in ("extension", "first letter"):
        raise OrganizerError("Group by extension or first letter.")
    collection = _safe_group_name(collection_name)
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= MAX_FILES:
        raise OrganizerError("The file limit must be from 1 to {}.".format(MAX_FILES))

    try:
        entries = sorted(os.scandir(root), key=lambda item: (item.name.casefold(), item.name))
    except OSError as exc:
        raise OrganizerError("Could not read the selected folder: {}".format(exc)) from exc
    files = [entry for entry in entries if entry.is_file(follow_symlinks=False)]
    selected = files[:max_files]
    moves: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    directories = set()

    for entry in selected:
        group = _group_for(entry.name, group_by)
        source_relative = entry.name
        destination_relative = os.path.join(collection, group, entry.name)
        destination = os.path.join(root, destination_relative)
        parent = os.path.dirname(destination)
        # A file/symlink where a new directory should be is a visible conflict.
        if os.path.lexists(destination) or (
            os.path.lexists(os.path.join(root, collection))
            and not os.path.isdir(os.path.join(root, collection))
        ) or (
            os.path.lexists(parent)
            and not os.path.isdir(parent)
        ):
            skipped.append({"source": source_relative, "reason": "destination already exists or is blocked"})
            continue
        moves.append({"source": source_relative, "destination": destination_relative})
        directories.add(os.path.join(collection, group))

    return {
        "root": root,
        "group_by": group_by,
        "collection_name": collection,
        "moves": moves,
        "directories": sorted(directories, key=lambda value: (value.count(os.sep), value)),
        "skipped": skipped,
        "unplanned_count": max(0, len(files) - len(selected)),
    }


def _checked_path(root: str, relative: str) -> str:
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise OrganizerError("The saved plan contains an invalid path.")
    normalized = os.path.normpath(relative)
    if normalized in (".", "..") or normalized.startswith(".." + os.sep):
        raise OrganizerError("The saved plan contains a path outside the selected folder.")
    absolute = os.path.realpath(os.path.join(root, normalized))
    try:
        if os.path.commonpath((root, absolute)) != root:
            raise OrganizerError("The saved plan contains a path outside the selected folder.")
    except ValueError as exc:
        raise OrganizerError("The saved plan contains an invalid path.") from exc
    return absolute


def _assert_no_symlink_ancestors(root: str, path: str) -> None:
    relative = os.path.relpath(path, root)
    current = root
    components = relative.split(os.sep)
    for component in components[:-1]:
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise OrganizerError("A destination folder is a symbolic link: {}".format(current))
        if os.path.lexists(current) and not os.path.isdir(current):
            raise OrganizerError("A destination folder is blocked by a file: {}".format(current))


def _move_no_replace(source: str, destination: str) -> None:
    """Move a regular file without replacing a destination that appeared meanwhile."""
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise
        # Some iOS document providers do not implement hard links. Exclusive creation
        # preserves the no-overwrite guarantee for the copy fallback.
        try:
            with open(source, "rb") as incoming, open(destination, "xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
            shutil.copystat(source, destination)
        except Exception:
            try:
                os.unlink(destination)
            except OSError:
                pass
            raise
    try:
        os.unlink(source)
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise


def apply_plan(plan: Mapping[str, Any], *, confirmed: bool = False) -> Dict[str, Any]:
    """Apply a reviewed plan after a separate explicit confirmation."""
    if confirmed is not True:
        raise OrganizerError("No files were moved. Explicit confirmation of the reviewed plan is required.")
    if not isinstance(plan, Mapping):
        raise OrganizerError("The move plan is invalid.")
    root = plan.get("root")
    moves = plan.get("moves")
    if not isinstance(root, str) or not os.path.isdir(root) or not isinstance(moves, list):
        raise OrganizerError("The selected folder or move plan is no longer available.")

    checked = []
    destinations = set()
    for item in moves:
        if not isinstance(item, Mapping):
            raise OrganizerError("The move plan contains an invalid item.")
        source = _checked_path(root, item.get("source"))
        destination = _checked_path(root, item.get("destination"))
        if os.path.dirname(os.path.relpath(source, root)) not in ("", "."):
            raise OrganizerError("The move plan may only move files from the selected folder's top level.")
        if destination in destinations:
            raise OrganizerError("The move plan contains duplicate destinations.")
        destinations.add(destination)
        _assert_no_symlink_ancestors(root, destination)
        if os.path.islink(source) or not os.path.isfile(source) or not os.access(source, os.R_OK):
            raise OrganizerError("A planned file is missing or cannot be read: {}".format(item.get("source")))
        if os.path.lexists(destination):
            raise OrganizerError("A planned destination already exists: {}".format(item.get("destination")))
        checked.append((source, destination))

    created = []
    completed = []
    try:
        for _source, destination in checked:
            parent = os.path.dirname(destination)
            missing = []
            current = parent
            while current != root and not os.path.exists(current):
                missing.append(current)
                current = os.path.dirname(current)
            for directory in reversed(missing):
                os.mkdir(directory)
                created.append(directory)
        for source, destination in checked:
            _move_no_replace(source, destination)
            completed.append((source, destination))
    except Exception as exc:
        rollback_errors = []
        for source, destination in reversed(completed):
            try:
                _move_no_replace(destination, source)
            except Exception as rollback_exc:  # report incomplete rollback explicitly
                rollback_errors.append(str(rollback_exc))
        for directory in reversed(created):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        detail = "Move failed; completed moves were rolled back: {}".format(exc)
        if rollback_errors:
            detail += " Rollback needs attention: {}".format("; ".join(rollback_errors))
        raise OrganizerError(detail) from exc

    return {
        "root": root,
        "moved": len(completed),
        "moves": [
            {"source": os.path.relpath(source, root), "destination": os.path.relpath(destination, root)}
            for source, destination in completed
        ],
        "created_directories": [os.path.relpath(path, root) for path in created],
    }


def undo_plan(applied: Mapping[str, Any], *, confirmed: bool = False) -> Dict[str, Any]:
    """Reverse a completed move after a separate explicit confirmation.

    All paths and conflicts are checked before the first move. If a later move fails, the
    already-restored files are moved back to their organized destinations where possible.
    Directories created by the original apply are removed only if they are still empty.
    """
    if confirmed is not True:
        raise OrganizerError("No files were moved back. Explicit confirmation of the undo is required.")
    if not isinstance(applied, Mapping):
        raise OrganizerError("The saved apply result is invalid.")
    root = applied.get("root")
    moves = applied.get("moves")
    created_directories = applied.get("created_directories", [])
    if not isinstance(root, str) or not os.path.isdir(root) or not isinstance(moves, list):
        raise OrganizerError("The selected folder or saved apply result is no longer available.")
    if not isinstance(created_directories, list):
        raise OrganizerError("The saved apply result has an invalid directory list.")

    checked = []
    checked_directories = []
    for relative in created_directories:
        if not isinstance(relative, str) or not relative or os.path.isabs(relative):
            raise OrganizerError("The saved apply result contains an invalid created directory.")
        normalized = os.path.normpath(relative)
        if normalized in (".", "..") or normalized.startswith(".." + os.sep):
            raise OrganizerError("The saved apply result contains a directory outside the selected folder.")
        directory = os.path.abspath(os.path.join(root, normalized))
        try:
            if os.path.commonpath((root, directory)) != root:
                raise OrganizerError("The saved apply result contains a directory outside the selected folder.")
        except ValueError as exc:
            raise OrganizerError("The saved apply result contains an invalid directory.") from exc
        checked_directories.append(directory)
    sources = set()
    for item in moves:
        if not isinstance(item, Mapping):
            raise OrganizerError("The saved apply result contains an invalid move.")
        original = _checked_path(root, item.get("source"))
        organized = _checked_path(root, item.get("destination"))
        if os.path.dirname(os.path.relpath(original, root)) not in ("", "."):
            raise OrganizerError("Undo may only restore files to the selected folder's top level.")
        if original in sources:
            raise OrganizerError("The saved apply result contains duplicate original paths.")
        sources.add(original)
        _assert_no_symlink_ancestors(root, original)
        if os.path.lexists(original):
            raise OrganizerError("An original path is occupied; undo stopped before moving files: {}".format(item.get("source")))
        if os.path.islink(organized) or not os.path.isfile(organized) or not os.access(organized, os.R_OK):
            raise OrganizerError("An organized file is missing or cannot be read: {}".format(item.get("destination")))
        checked.append((organized, original))

    completed = []
    try:
        for organized, original in checked:
            _move_no_replace(organized, original)
            completed.append((organized, original))
    except Exception as exc:
        rollback_errors = []
        for organized, original in reversed(completed):
            try:
                _move_no_replace(original, organized)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = "Undo failed; completed moves were rolled back: {}".format(exc)
        if rollback_errors:
            detail += " Rollback needs attention: {}".format("; ".join(rollback_errors))
        raise OrganizerError(detail) from exc

    removed_directories = []
    for target in sorted(checked_directories, key=lambda value: (value.count(os.sep), value), reverse=True):
        if os.path.islink(target):
            continue
        try:
            _assert_no_symlink_ancestors(root, target)
        except OrganizerError:
            continue
        try:
            os.rmdir(target)
            removed_directories.append(os.path.relpath(target, root))
        except OSError:
            # Preserve a non-empty or inaccessible folder; undo only removes empty folders
            # created by the apply operation.
            continue

    return {
        "root": root,
        "restored": len(completed),
        "moves": [
            {"source": os.path.relpath(original, root), "destination": os.path.relpath(organized, root)}
            for organized, original in completed
        ],
        "removed_directories": removed_directories,
    }

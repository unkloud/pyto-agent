"""No-LLM manual launcher for Goal 15 Pyto capability checks.

Run this script in the target Pyto installation. The first ordinary run creates a private
fixture and persistent handle. Close/reopen Pyto, then run the script again; the second run
checks the handle and records workspace persistence automatically. ``--stress`` enables
optional location and photo-picker checks. No Shortcut is enumerated or invoked.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import capability_inventory, ios
from harness.config import default_state_dir, default_workspace
from harness.security import chmod_private, mkdir_private, open_private
from harness.tools_ios import Workspace
from harness.handles import LocalHandleStore

CHECK_ID = "goal15.workspace-persistence"
FIXTURE_DIR = ".capability-checks"
PENDING_NAME = "pending-restart.json"
FIXTURE_TEXT = "pyto-harness capability check\nlocal persistence fixture\n"
MINIMAL_CHECKS = (
    ("clipboard.read", "pasteboard", ("string",)),
    ("clipboard.write", "pasteboard", ("set_string",)),
    ("file.import", "file_system", ("import_file",)),
    ("file.share", "file_system", ("share_files",)),
    ("notification.send", "notifications", ("Notification", "send_notification")),
    ("speech.speak", "speech", ("say", "wait")),
    ("photo.pick", "photos", ("pick_photo",)),
    ("photo.capture", "photos", ("take_photo",)),
    ("photo.write", "photos", ("save_image",)),
    ("calendar.read", "calendar_events", ("get_events",)),
    ("calendar.write", "calendar_events", ("save_event",)),
    ("location.read", "location", ("start_updating", "get_location", "stop_updating")),
    ("shortcut.run", "xcallback", ("open_url",)),
    ("background.keepalive", "background", ("BackgroundTask",)),
)


def _private_json(path: str) -> Optional[Dict[str, Any]]:
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > 64 * 1024:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None


def _write_pending(path: str, value: Dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".goal15-pending-", dir=os.path.dirname(path))
    try:
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        if os.name == "posix":
            chmod_private(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _minimal_paths(report: List[Dict[str, Any]], state_dir: str) -> None:
    """Check imports/member lookup only; a pass is not recorded as verified."""
    for capability_id, module_name, members in MINIMAL_CHECKS:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError) as exc:
            recorded = capability_inventory.record_unavailable(
                state_dir,
                capability_id,
                failed_step="import {}".format(module_name),
                error_type=type(exc).__name__,
                detail=str(exc),
                test_id=CHECK_ID,
            )
            report.append({"id": capability_id, "result": "unavailable" if recorded else "unverified", "step": "import module"})
            continue
        missing = [member for member in members if not hasattr(module, member)]
        if missing:
            exc = AttributeError("missing documented member(s): {}".format(", ".join(missing)))
            recorded = capability_inventory.record_unavailable(
                state_dir,
                capability_id,
                failed_step="lookup {}.{}".format(module_name, missing[0]),
                error_type=type(exc).__name__,
                detail=str(exc),
                test_id=CHECK_ID,
            )
            report.append({"id": capability_id, "result": "unavailable" if recorded else "unverified", "step": "member lookup"})
        else:
            report.append({"id": capability_id, "result": "minimal_path_pass_unverified", "step": "import and documented member lookup"})


def _workspace_restart_check(workspace: Workspace, report: List[Dict[str, Any]], state_dir: str) -> None:
    fixture_dir = workspace.resolve(FIXTURE_DIR)
    mkdir_private(fixture_dir)
    marker_path = os.path.join(fixture_dir, PENDING_NAME)
    if os.path.islink(marker_path):
        report.append({"id": "workspace.persist", "result": "skipped", "step": "pending marker is a symbolic link and was left untouched"})
        return
    marker = _private_json(marker_path)
    if marker is None:
        fd, source_path = tempfile.mkstemp(prefix="goal15-", suffix=".txt", dir=fixture_dir)
        os.close(fd)
        with open_private(source_path, truncate=True) as handle:
            handle.write(FIXTURE_TEXT)
        relative_source = workspace.relative(source_path)
        store = LocalHandleStore(workspace)
        ingested = store.ingest_file(relative_source)
        if ingested.state != "ok" or ingested.handle is None:
            store.close()
            report.append({"id": "workspace.persist", "result": "failed", "step": "create persistent artifact"})
            return
        marker = {
            "version": 1,
            "handle_id": ingested.handle.handle_id,
            "source_path": relative_source,
            "expected_sha256": hashlib.sha256(FIXTURE_TEXT.encode("utf-8")).hexdigest(),
            "created_at": int(time.time()),
        }
        _write_pending(marker_path, marker)
        store.close()
        report.append({"id": "workspace.persist", "result": "pending_restart", "step": "artifact saved; rerun after closing and reopening Pyto"})
        return

    if (
        marker.get("version") != 1
        or not isinstance(marker.get("handle_id"), str)
        or not isinstance(marker.get("expected_sha256"), str)
        or not isinstance(marker.get("source_path"), str)
        or not marker["source_path"].startswith(FIXTURE_DIR + "/goal15-")
    ):
        report.append({"id": "workspace.persist", "result": "skipped", "step": "pending marker is malformed and was left untouched"})
        return

    store = LocalHandleStore(workspace)
    handle_id = marker.get("handle_id")
    metadata = store.metadata(handle_id) if isinstance(handle_id, str) else None
    output_path = None
    try:
        if metadata is None:
            report.append({"id": "workspace.persist", "result": "unverified", "step": "handle metadata missing after restart"})
            return
        output_name = os.path.join(FIXTURE_DIR, "roundtrip-{}.txt".format(handle_id[:12]))
        written = store.write_file(handle_id, output_name)
        if written.state != "ok":
            report.append({"id": "workspace.persist", "result": "unverified", "step": "retrieve handle after restart"})
            return
        output_path = workspace.resolve(output_name, must_exist=True)
        with open(output_path, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        expected = marker.get("expected_sha256")
        if actual != expected:
            report.append({"id": "workspace.persist", "result": "unverified", "step": "retrieved artifact checksum mismatch"})
            return
        stored = capability_inventory.record_verified(
            state_dir,
            "workspace.persist",
            test_id=CHECK_ID,
            result="typed artifact ID and payload survived close/reopen and matched the local fixture checksum",
        )
        report.append({"id": "workspace.persist", "result": "verified" if stored else "unverified", "step": "cross-restart retrieval and checksum"})
    finally:
        try:
            if isinstance(handle_id, str) and store.metadata(handle_id) is not None:
                store.delete(handle_id)
        except Exception:
            pass
        store.close()
        for relative in (marker.get("source_path"), output_path and workspace.relative(output_path), FIXTURE_DIR + "/" + PENDING_NAME):
            if not relative:
                continue
            try:
                target = workspace.resolve(str(relative), must_exist=True)
                if os.path.isfile(target) and not os.path.islink(target):
                    os.unlink(target)
            except (OSError, ValueError):
                pass


def _stress_checks(report: List[Dict[str, Any]], state_dir: str) -> None:
    """Opt-in personal-data/UI checks. Never record a cancellation or transient error."""
    try:
        location = importlib.import_module("location")
        location.start_updating()
        try:
            position = location.get_location()
        finally:
            location.stop_updating()
        if position is None:
            report.append({"id": "location.read", "result": "unverified", "step": "no location returned; no evidence changed"})
        else:
            latitude = float(position.latitude)
            longitude = float(position.longitude)
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                stored = capability_inventory.record_verified(
                    state_dir,
                    "location.read",
                    test_id="goal15.stress.location-read",
                    result="location API returned numeric coordinates in valid ranges; coordinate values were discarded",
                )
                report.append({"id": "location.read", "result": "verified" if stored else "unverified", "step": "valid result; coordinates discarded"})
            else:
                report.append({"id": "location.read", "result": "unverified", "step": "invalid coordinate range; no evidence changed"})
    except PermissionError as exc:
        stored = capability_inventory.record_unavailable(
            state_dir,
            "location.read",
            failed_step="request current location",
            error_type="PermissionError",
            detail=str(exc),
            test_id="goal15.stress.location-read",
        )
        report.append({"id": "location.read", "result": "unavailable" if stored else "unverified", "step": "permission failure"})
    except BaseException as exc:
        report.append({"id": "location.read", "result": "cancelled_or_incidental", "step": type(exc).__name__})

    try:
        photos = importlib.import_module("photos")
        image = photos.pick_photo()
        if image is None:
            report.append({"id": "photo.pick", "result": "cancelled_or_incidental", "step": "no photo selected; no evidence changed"})
        else:
            dimensions = getattr(image, "size", None)
            if isinstance(dimensions, tuple) and len(dimensions) == 2 and all(isinstance(item, int) and item > 0 for item in dimensions):
                stored = capability_inventory.record_verified(
                    state_dir,
                    "photo.pick",
                    test_id="goal15.stress.photo-pick",
                    result="photo picker returned an image with positive dimensions; image data was discarded",
                )
                report.append({"id": "photo.pick", "result": "verified" if stored else "unverified", "step": "image shape validated; image data discarded"})
            else:
                report.append({"id": "photo.pick", "result": "unverified", "step": "picker result shape was not usable"})
    except PermissionError as exc:
        stored = capability_inventory.record_unavailable(
            state_dir,
            "photo.pick",
            failed_step="open photo picker",
            error_type="PermissionError",
            detail=str(exc),
            test_id="goal15.stress.photo-pick",
        )
        report.append({"id": "photo.pick", "result": "unavailable" if stored else "unverified", "step": "permission failure"})
    except BaseException as exc:
        report.append({"id": "photo.pick", "result": "cancelled_or_incidental", "step": type(exc).__name__})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress", action="store_true", help="also run opt-in location and photo-picker checks")
    args = parser.parse_args(argv)
    report: List[Dict[str, Any]] = []
    runtime = capability_inventory.runtime_fingerprint()
    report.append({"id": "runtime", "result": "ready" if all(runtime.get(key) not in (None, "", "unknown") for key in capability_inventory.FINGERPRINT_FIELDS) else "incomplete", "runtime": runtime, "date": datetime.date.today().isoformat()})
    if not ios.is_pyto():
        report.append({"id": "device_checks", "result": "skipped", "step": "launch this script in the target Pyto installation"})
        print(json.dumps({"checks": report}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    state_dir = default_state_dir()
    capability_inventory.initialize_override(state_dir, runtime=runtime)
    _minimal_paths(report, state_dir)
    _workspace_restart_check(Workspace(default_workspace()), report, state_dir)
    if args.stress:
        _stress_checks(report, state_dir)
    print(json.dumps({"checks": report, "stress_enabled": bool(args.stress)}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

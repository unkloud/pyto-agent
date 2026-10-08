"""Tests for documentation inventory, target-device evidence and typed artifacts."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from harness import capability_inventory

from .support import TempDirTestCase


RUNTIME = {
    "device_model": "iPhone17,3",
    "ios_version": "26.1",
    "pyto_version": "19.1",
    "pyto_build": "500",
}


class TestCapabilityInventory(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = self.path("state")

    def read_override(self):
        with open(capability_inventory.override_path(self.state_dir), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_global_inventory_has_documented_unverified_points_without_signatures(self) -> None:
        inventory = capability_inventory.load_global()
        self.assertEqual(inventory["version"], 1)
        self.assertGreaterEqual(len(inventory["entries"]), 10)
        for point in inventory["entries"]:
            self.assertRegex(point["id"], r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
            self.assertEqual(point["status"], "unverified")
            self.assertTrue(point["documentation_links"])
            self.assertNotIn("signature", point)

    def test_override_records_only_substantive_failure_with_exact_step(self) -> None:
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=True):
            written = capability_inventory.record_unavailable(
                self.state_dir,
                "clipboard.read",
                failed_step="import pasteboard",
                error_type="ImportError",
                detail="module unavailable",
                test_id="probe_clipboard.py",
                runtime=RUNTIME,
                date="2026-10-08",
            )
        self.assertTrue(written)
        override = self.read_override()
        point = override["entries"]["clipboard.read"]
        self.assertEqual(override["version"], 1)
        self.assertEqual(point["id"], "clipboard.read")
        self.assertTrue(point["documentation_links"])
        self.assertEqual(point["status"], "unavailable")
        self.assertEqual(point["evidence"]["failed_step"], "import pasteboard")
        self.assertEqual(point["date"], "2026-10-08")

    def test_incidental_or_incomplete_failures_do_not_write_override(self) -> None:
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=True):
            self.assertFalse(capability_inventory.record_unavailable(
                self.state_dir,
                "clipboard.read",
                failed_step="call pasteboard.string",
                error_type="TimeoutError",
                detail="timed out",
                test_id="probe_clipboard.py",
                runtime=RUNTIME,
            ))
            self.assertFalse(capability_inventory.record_unavailable(
                self.state_dir,
                "clipboard.read",
                failed_step="call pasteboard.string",
                error_type="OSError",
                detail="temporary socket failure",
                test_id="probe_clipboard.py",
                runtime=RUNTIME,
            ))
            self.assertFalse(capability_inventory.record_unavailable(
                self.state_dir,
                "clipboard.read",
                failed_step="",
                error_type="PermissionError",
                detail="denied",
                test_id="probe_clipboard.py",
                runtime=RUNTIME,
            ))
            self.assertFalse(capability_inventory.record_unavailable(
                self.state_dir,
                "clipboard.read",
                failed_step="import pasteboard",
                error_type="ImportError",
                detail="module unavailable",
                test_id="probe_clipboard.py",
                runtime={**RUNTIME, "pyto_build": "unknown"},
            ))
        self.assertFalse(os.path.exists(capability_inventory.override_path(self.state_dir)))

    def test_verified_requires_pyto_and_automated_end_to_end_evidence(self) -> None:
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=False):
            self.assertFalse(capability_inventory.record_verified(
                self.state_dir,
                "workspace.persist",
                test_id="restart-check",
                result="handle reloaded and checksum matched",
                runtime=RUNTIME,
            ))
        self.assertFalse(os.path.exists(capability_inventory.override_path(self.state_dir)))
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=True):
            self.assertFalse(capability_inventory.record_verified(
                self.state_dir,
                "workspace.persist",
                test_id="restart-check",
                result="",
                runtime=RUNTIME,
            ))
            self.assertTrue(capability_inventory.record_verified(
                self.state_dir,
                "workspace.persist",
                test_id="goal15.workspace-persistence",
                result="handle reloaded and checksum matched",
                runtime=RUNTIME,
                date="2026-10-08",
            ))
        point = capability_inventory.effective_inventory(self.state_dir, runtime=RUNTIME)["entries"]
        workspace_point = next(item for item in point if item["id"] == "workspace.persist")
        self.assertEqual(workspace_point["status"], "verified")
        self.assertEqual(workspace_point["evidence_source"], "device_override")

    def test_evidence_becomes_unverified_after_any_fingerprint_change(self) -> None:
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=True):
            self.assertTrue(capability_inventory.record_verified(
                self.state_dir,
                "workspace.persist",
                test_id="goal15.workspace-persistence",
                result="handle checksum matched",
                runtime=RUNTIME,
                date="2026-10-08",
            ))
        for field, changed in (
            ("device_model", "iPad"),
            ("ios_version", "26.2"),
            ("pyto_version", "19.2"),
            ("pyto_build", "501"),
        ):
            with self.subTest(field=field):
                current = dict(RUNTIME, **{field: changed})
                inventory = capability_inventory.effective_inventory(self.state_dir, runtime=current)
                point = next(item for item in inventory["entries"] if item["id"] == "workspace.persist")
                self.assertEqual(point["status"], "unverified")
                self.assertEqual(point["evidence_source"], "stale_device_override")
        saved = self.read_override()["entries"]["workspace.persist"]
        self.assertEqual(saved["status"], "verified", "stale evidence remains available for audit")

    def test_prompt_context_surfaces_links_and_statuses(self) -> None:
        prompt = capability_inventory.render_prompt_context(self.state_dir, runtime=RUNTIME)
        self.assertIn("clipboard.read — unverified", prompt)
        self.assertIn("https://pyto.readthedocs.io/en/latest/library/pasteboard.html", prompt)
        self.assertIn("signatures are in the linked official docs", prompt)


if __name__ == "__main__":
    unittest.main()

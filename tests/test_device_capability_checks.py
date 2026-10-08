"""The Pyto launcher is offline, no-LLM, and records only target-device evidence."""

from __future__ import annotations

import json
import os
import runpy
from unittest import mock

from harness import capability_inventory
from harness.tools_ios import Workspace

from .support import ROOT, TempDirTestCase


SCRIPT = os.path.join(ROOT, "device_capability_checks.py")
RUNTIME = {
    "device_model": "iPhone17,3",
    "ios_version": "26.1",
    "pyto_version": "19.1",
    "pyto_build": "500",
}


class TestDeviceCapabilityChecks(TempDirTestCase):
    def test_desktop_run_skips_device_checks_without_writing_override(self) -> None:
        namespace = runpy.run_path(SCRIPT, run_name="test_device_capability_checks")
        with mock.patch.object(namespace["ios"], "is_pyto", return_value=False):
            result = namespace["main"]([])
        self.assertEqual(result, 0)
        self.assertFalse(os.path.exists(capability_inventory.override_path(self.path("state"))))

    def test_workspace_check_records_verified_only_after_restart_retrieval(self) -> None:
        namespace = runpy.run_path(SCRIPT, run_name="test_device_capability_checks")
        workspace = Workspace(self.path("workspace"))
        state_dir = self.path("state")
        first_report = []
        namespace["capability_inventory"].initialize_override(state_dir, runtime=RUNTIME)
        with mock.patch.object(namespace["ios"], "is_pyto", return_value=True), mock.patch.object(
            capability_inventory, "runtime_fingerprint", return_value=RUNTIME
        ):
            namespace["_workspace_restart_check"](workspace, first_report, state_dir)
            self.assertEqual(first_report[0]["result"], "pending_restart")
            self.assertFalse(os.path.exists(capability_inventory.override_path(state_dir)) and self._has_verified(state_dir))

            second_report = []
            namespace["_workspace_restart_check"](Workspace(self.path("workspace")), second_report, state_dir)
        self.assertEqual(second_report[0]["result"], "verified")
        self.assertTrue(self._has_verified(state_dir))

    def _has_verified(self, state_dir):
        path = capability_inventory.override_path(state_dir)
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("entries", {}).get("workspace.persist", {}).get("status") == "verified"

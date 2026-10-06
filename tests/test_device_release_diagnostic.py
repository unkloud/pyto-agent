"""The device report distinguishes scripted checks from native acceptance."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

import device_release_diagnostic as diagnostic


class TestDeviceReleaseDiagnostic(unittest.TestCase):
    def test_scripted_scope_is_the_seven_non_ui_candidates(self) -> None:
        titles = [title.split(" · ", 1)[0] for title, _scope, _cases in diagnostic.SCRIPTED_TEST_GROUPS]
        self.assertEqual(titles, ["03", "06", "07", "08", "09", "12"])
        self.assertEqual(len(diagnostic.SCRIPTED_TEST_GROUPS) + 1, 7)  # Objective-C recipe is the seventh.
        case_names = [case for _title, _scope, cases in diagnostic.SCRIPTED_TEST_GROUPS for case in cases]
        self.assertTrue(all("tests.test_" in case for case in case_names))
        self.assertFalse(any("subprocess" in case or "preview" in case for case in case_names))

    def test_behavior_suite_skips_without_claiming_desktop_results_as_device_evidence(self) -> None:
        with mock.patch.object(diagnostic, "_is_ios_runtime", return_value=False):
            results = diagnostic.run_device_behavior_checks(Path("/not-used"), diagnostic.RELEASE_VERSION)
        self.assertEqual(len(results), 7)
        self.assertTrue(all(item["status"] == "SKIPPED" for item in results))
        self.assertTrue(all("outside iOS/Pyto" in item["detail"] for item in results))

    def test_report_preserves_manual_not_run_statuses_after_scripted_checks(self) -> None:
        doctor_result = {
            "status": "PASS",
            "summary": "doctor ok",
            "rows": [],
            "migration": "",
            "version": diagnostic.RELEASE_VERSION,
        }
        scripted = [
            {"title": title, "status": "PASS", "detail": "one fixture passed"}
            for title, _scope, _cases in diagnostic.SCRIPTED_TEST_GROUPS
        ]
        scripted.insert(
            5,
            {"title": "11 · Read-only Objective-C recipes", "status": "PASS", "detail": "read-only recipe passed"},
        )
        with (
            mock.patch.object(diagnostic, "find_harness_root", return_value=None),
            mock.patch.object(diagnostic, "_device_probe", return_value={"status": "SKIPPED", "detail": "desktop"}),
            mock.patch.object(diagnostic, "run_local_doctor", return_value=doctor_result),
            mock.patch.object(diagnostic, "run_device_behavior_checks", return_value=scripted),
            mock.patch.object(diagnostic, "_is_ios_runtime", return_value=False),
        ):
            report = diagnostic.build_report(Path("diagnostic.py"), None)

        self.assertIn("### Focused on-device behavior scripts", report)
        self.assertIn("Scripted results do not change the manual checklist statuses", report)
        self.assertEqual(report.count("**Status:** `NOT RUN`"), len(diagnostic.MANUAL_CHECKS))
        self.assertEqual(report.count("| **PASS** | one fixture passed |"), len(diagnostic.SCRIPTED_TEST_GROUPS))

    def test_safe_test_failure_keeps_exception_detail_and_redacts_private_values(self) -> None:
        class FailedCase:
            @staticmethod
            def id() -> str:
                return "suite.TestDeviceCase.test_failure"

        summary = diagnostic._safe_test_failure(
            FailedCase(),
            "Traceback (most recent call last):\n"
            "  File '/private/var/mobile/user data/test.py', line 12\n"
            "AssertionError: got /private/var/mobile/user data sk-12345678901234567890 "
            "https://user:pass@example.test/path",
        )
        self.assertIn("test_failure (AssertionError:", summary)
        self.assertIn("<path>", summary)
        self.assertIn("<redacted>", summary)
        self.assertIn("<URL>", summary)
        self.assertNotIn("/private/var/mobile", summary)
        self.assertNotIn("sk-12345678901234567890", summary)
        self.assertNotIn("user:pass", summary)


if __name__ == "__main__":
    unittest.main()

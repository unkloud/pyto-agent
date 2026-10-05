"""Read-only availability checks for optional Python libraries."""

from __future__ import annotations

import unittest

from harness.errors import ToolError
from harness.module_tools import inspect_modules


class TestModuleCapabilities(unittest.TestCase):
    def test_reports_known_and_unknown_top_level_imports(self) -> None:
        result = inspect_modules(["sys", "pyto_harness_module_does_not_exist"])
        by_name = {item["name"]: item["status"] for item in result["modules"]}
        self.assertEqual(by_name["sys"], "available")
        self.assertEqual(by_name["pyto_harness_module_does_not_exist"], "not found")

    def test_rejects_dotted_names_and_empty_lists(self) -> None:
        for names in ([], ["sys.path"], ["numpy;print(1)"]):
            with self.subTest(names=names), self.assertRaises(ToolError):
                inspect_modules(names)

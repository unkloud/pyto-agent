"""Tests for batch examples and the explicitly interactive PytoUI scaffold.

Batch examples use only the standard library and default to non-destructive behavior. The
interactive scaffold may use PytoUI and standard-library networking after the user taps its
Refresh button; it does not contact the network during import or startup.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import types
import unittest
from unittest import mock

from .support import ROOT, TempDirTestCase

EXAMPLES = os.path.join(ROOT, "examples")


def load_example(name: str):
    """Import an example as a module without executing its ``__main__`` block."""
    path = os.path.join(EXAMPLES, name)
    return runpy.run_path(path, run_name="example_under_test")


class TestRenameByDate(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_example("rename_by_date.py")
        self.folder = self.path("shots")
        os.makedirs(self.folder, exist_ok=True)

    def touch(self, name: str, when: float = None) -> str:
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        if when is not None:
            os.utime(path, (when, when))
        return path

    def test_date_parsing_variants(self) -> None:
        import datetime

        parse = self.module["parse_date"]
        self.assertEqual(parse("IMG_2024-03-02_09-14-22"), datetime.date(2024, 3, 2))
        self.assertEqual(parse("scan 03-02-2024"), datetime.date(2024, 2, 3))
        self.assertEqual(parse("nothing here"), None)

    def test_plan_is_chronological_and_counted(self) -> None:
        self.touch("Photo 2024-03-02 at 09.14.22.jpeg")
        self.touch("Photo 2024-03-02 at 10.15.00.jpeg")
        plan = self.module["plan_renames"](self.folder, "*")
        names = [new for _old, new, _reason in plan]
        self.assertEqual(names[0], "2024-03-02_001_Photo.jpeg")
        self.assertEqual(names[1], "2024-03-02_002_Photo.jpeg")

    def test_dry_run_changes_nothing(self) -> None:
        before = self.touch("IMG_4821.PNG")
        plan = self.module["plan_renames"](self.folder, "*")
        self.assertTrue(plan)
        self.assertTrue(os.path.exists(before))

    def test_apply_renames(self) -> None:
        self.touch("scan 03-02-2024.pdf")
        plan = self.module["plan_renames"](self.folder, "*")
        renamed, problems = self.module["apply_plan"](self.folder, plan)
        self.assertEqual(problems, [])
        self.assertEqual(renamed, 1)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "2024-02-03_001_scan.pdf")))

    def test_apply_never_overwrites(self) -> None:
        """A collision must be refused, and the existing file left exactly as it was."""
        # An already-correct name is not re-planned (it would only churn), so the
        # collision is exercised by handing `apply_plan` a plan that would hit it.
        self.touch("a 2024-03-02.txt")
        occupied = self.touch("2024-03-02_001_a.txt")
        with open(occupied, "w", encoding="utf-8") as handle:
            handle.write("ORIGINAL")
        renamed, problems = self.module["apply_plan"](
            self.folder, [("a 2024-03-02.txt", "2024-03-02_001_a.txt", "test")]
        )
        self.assertEqual(renamed, 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("already exists", problems[0])
        with open(occupied, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ORIGINAL", "the existing file must be untouched")
        self.assertTrue(os.path.exists(os.path.join(self.folder, "a 2024-03-02.txt")))

    def test_main_dry_run_via_argv(self) -> None:
        self.touch("IMG_4821.PNG")
        code = self.module["main"](["--folder", self.folder])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "IMG_4821.PNG")))

    def test_missing_folder_is_reported(self) -> None:
        self.assertEqual(self.module["main"](["--folder", self.path("nope")]), 2)


class TestFolderOrganizer(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_example("folder_organizer_logic.py")
        self.folder = self.path("選んだ folder")
        os.makedirs(self.folder, exist_ok=True)

    def write(self, name: str, contents: str = "data") -> str:
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        return path

    def test_plan_is_read_only_and_groups_unicode_names(self) -> None:
        source = self.write("Résumé 1.PDF", "résumé")
        plan = self.module["build_plan"](
            self.folder, group_by="extension", collection_name="My files"
        )
        self.assertEqual(len(plan["moves"]), 1)
        self.assertEqual(plan["moves"][0]["source"], "Résumé 1.PDF")
        self.assertEqual(plan["moves"][0]["destination"], os.path.join("My files", "pdf", "Résumé 1.PDF"))
        self.assertTrue(os.path.exists(source))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "My files")))

    def test_apply_requires_confirmation_and_moves_reviewed_files(self) -> None:
        source = self.write("scan 1.pdf", "pdf bytes")
        self.write("notes.txt", "text bytes")
        plan = self.module["build_plan"](self.folder)
        with self.assertRaisesRegex(self.module["OrganizerError"], "Explicit confirmation"):
            self.module["apply_plan"](plan)
        self.assertTrue(os.path.exists(source))
        applied = self.module["apply_plan"](plan, confirmed=True)
        self.assertEqual(applied["moved"], 2)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "Organized", "pdf", "scan 1.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "Organized", "txt", "notes.txt")))
        self.assertFalse(os.path.exists(source))
        with self.assertRaisesRegex(self.module["OrganizerError"], "Explicit confirmation"):
            self.module["undo_plan"](applied)
        undone = self.module["undo_plan"](applied, confirmed=True)
        self.assertEqual(undone["restored"], 2)
        self.assertTrue(os.path.isfile(source))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Organized")))
        with open(source, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "pdf bytes")

    def test_undo_detects_any_occupied_original_before_moving_files(self) -> None:
        source = self.write("report.pdf", "report")
        other = self.write("notes.txt", "notes")
        plan = self.module["build_plan"](self.folder)
        applied = self.module["apply_plan"](plan, confirmed=True)
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("keep the newer note")

        with self.assertRaisesRegex(self.module["OrganizerError"], "original path is occupied"):
            self.module["undo_plan"](applied, confirmed=True)
        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "Organized", "pdf", "report.pdf")))
        with open(other, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep the newer note")

    def test_stale_collision_refuses_the_whole_plan_without_overwriting(self) -> None:
        source = self.write("report.pdf", "source")
        plan = self.module["build_plan"](self.folder)
        destination = os.path.join(self.folder, "Organized", "pdf", "report.pdf")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write("keep this")
        with self.assertRaisesRegex(self.module["OrganizerError"], "destination already exists"):
            self.module["apply_plan"](plan, confirmed=True)
        self.assertTrue(os.path.exists(source))
        with open(destination, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep this")

    def test_first_letter_group_and_file_limit_are_clear(self) -> None:
        self.write("zeta.txt")
        self.write("alpha.txt")
        self.write("東京.txt")
        plan = self.module["build_plan"](self.folder, group_by="first letter", max_files=2)
        self.assertEqual(len(plan["moves"]), 2)
        self.assertEqual(plan["unplanned_count"], 1)
        self.assertEqual(
            [os.path.basename(os.path.dirname(item["destination"])) for item in plan["moves"]],
            ["A", "Z"],
        )


class TestNoteDigest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_example("note_digest.py")
        self.folder = self.path("notes")
        os.makedirs(self.folder, exist_ok=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_sections_are_classified(self) -> None:
        text = "# Day\n\n- shipped the fix\n\n## Next\n- write the changelog\n"
        buckets = self.module["classify"](self.module["split_sections"](text))
        self.assertIn("shipped the fix", buckets["done"])
        self.assertIn("write the changelog", buckets["next"])

    def test_checkboxes_become_open_items(self) -> None:
        text = "- [ ] email Sam\n- [x] book venue\n"
        checked, unchecked = self.module["checklists"](text)
        self.assertEqual(unchecked, ["email Sam"])
        self.assertEqual(checked, ["book venue"])

    def test_digest_mentions_the_counts(self) -> None:
        path = self.write("2024-03-02.md", "# Day\n- did a thing\n- [ ] do another\n")
        notes = {os.path.basename(path): self.module["read_note"](path)}
        digest = self.module["build_digest"](notes)
        self.assertIn("2024-03-02", digest)
        self.assertIn("What happened", digest)
        self.assertIn("do another", digest)

    def test_empty_folder_is_reported(self) -> None:
        self.assertEqual(self.module["main"](["--folder", self.folder]), 1)

    def test_outdigest_is_written(self) -> None:
        self.write("a.md", "# A\n- one\n")
        target = self.path("digest.md")
        code = self.module["main"](["--folder", self.folder, "--outdigest", target])
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            self.assertIn("What happened", handle.read())


class TestClipboardNote(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_example("clipboard_note.py")

    def test_classification_buckets(self) -> None:
        buckets = self.module["classify"](
            [
                "https://example.com/a",
                "me@example.com",
                "Total: $12.00",
                "Date: 2024-05-01",
                "Key: value",
                "- [ ] a task",
                "plain sentence",
            ]
        )
        self.assertEqual(buckets["links"], ["https://example.com/a"])
        self.assertIn("me@example.com", buckets["contacts"])
        self.assertIn("Total: $12.00", buckets["money"])
        self.assertEqual(buckets["checklist"], ["[ ] a task"])
        self.assertEqual(buckets["pairs"], [("Key", "value")])
        self.assertEqual(buckets["plain"], ["plain sentence"])

    def test_render_has_sections_and_provenance(self) -> None:
        note = self.module["render_note"]("https://x.example\n- [ ] do it", "Title", ["t"], "stdin")
        self.assertIn("## Title", note)
        self.assertIn("### Links", note)
        self.assertIn("### Checklist", note)
        self.assertIn("captured from stdin", note)

    def test_from_file_to_out(self) -> None:
        source = self.path("clip.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("https://example.com\n")
        target = self.path("note.md")
        code = self.module["main"](["--from-file", source, "--out", target, "--title", "T"])
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            self.assertIn("https://example.com", handle.read())

    def test_append_keeps_both_entries(self) -> None:
        source = self.path("clip.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("first\n")
        target = self.path("note.md")
        self.module["main"](["--from-file", source, "--out", target, "--title", "One"])
        self.module["main"](["--from-file", source, "--out", target, "--title", "Two"])
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("## One", text)
        self.assertIn("## Two", text)

    def test_missing_bridge_is_reported_not_raised(self) -> None:
        text, source = self.module["clipboard_text"]()
        self.assertEqual(text, "")
        self.assertIn("clipboard", source)


class TestExamplesThroughRun(TempDirTestCase):
    """Run an example the way the harness would: through `run_program`."""

    def test_run_program_executes_an_example_from_the_workspace(self) -> None:
        registry = self.make_registry()
        # Copy the example into the workspace, which is what the agent would do with
        # `write_program`, then run it exactly as the agent would.
        source = os.path.join(EXAMPLES, "note_digest.py")
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        registry._tools["write_program"].handler(
            path="note_digest.py", source=body, purpose="summarise notes"
        )
        notes = os.path.join(self.workspace_dir, "notes")
        os.makedirs(notes, exist_ok=True)
        with open(os.path.join(notes, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("# A\n- one thing\n- [ ] another thing\n")

        result = registry._tools["run_program"].handler(
            path_or_source="note_digest.py",
            args=["--folder", "notes"],
        )
        self.assertFalse(result.is_error, result.content)
        self.assertIn("What happened", result.content)
        self.assertIn("one thing", result.content)
        self.assertIn("exit code: 0", result.content)

    def test_run_program_executes_the_rename_example(self) -> None:
        registry = self.make_registry()
        with open(os.path.join(EXAMPLES, "rename_by_date.py"), encoding="utf-8") as handle:
            registry._tools["write_program"].handler(
                path="rename_by_date.py", source=handle.read(), purpose="rename by date"
            )
        shots = os.path.join(self.workspace_dir, "shots")
        os.makedirs(shots, exist_ok=True)
        with open(os.path.join(shots, "scan 03-02-2024.pdf"), "w", encoding="utf-8") as handle:
            handle.write("x")
        dry = registry._tools["run_program"].handler(
            path_or_source="rename_by_date.py", args=["--folder", "shots"]
        )
        self.assertIn("Dry run", dry.content)
        applied = registry._tools["run_program"].handler(
            path_or_source="rename_by_date.py", args=["--folder", "shots", "--apply"]
        )
        self.assertIn("Renamed 1 file(s)", applied.content)
        self.assertTrue(os.path.exists(os.path.join(shots, "2024-02-03_001_scan.pdf")))


class TestExamplesAreStdlibOnly(unittest.TestCase):
    def test_examples_parse_as_python_310_and_import_only_stdlib(self) -> None:
        import ast

        allowed = {
            "__future__", "argparse", "datetime", "fnmatch", "json", "os", "re", "runpy", "sys", "typing",
            "textwrap", "collections", "pathlib", "shutil", "hashlib", "math", "csv", "io", "time", "errno",
            "threading", "urllib", "interactive_logic", "folder_organizer_logic",
        }
        # Pyto bridges and Objective-C framework modules are imported only when their
        # device-specific functions run, so loading the examples remains safe off-device.
        optional_on_device = {
            "pasteboard", "share", "pyto", "notifications", "speech", "photos", "Foundation", "UIKit"
        }
        interactive_only = {"pyto_ui"}
        interactive_examples = {"interactive_app_scaffold.py", "folder_organizer_app.py"}
        for name in sorted(os.listdir(EXAMPLES)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(EXAMPLES, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path, feature_version=(3, 10))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in optional_on_device:
                            continue
                        if top in interactive_only:
                            self.assertIn(name, interactive_examples, "{} imports {}".format(name, alias.name))
                            continue
                        self.assertIn(top, allowed, "{} imports {}".format(name, alias.name))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.level:
                        continue  # relative import inside a package: not used here
                    if node.module.split(".")[0] in optional_on_device:
                        continue
                    if node.module.split(".")[0] in interactive_only:
                        self.assertIn(name, interactive_examples, "{} imports {}".format(name, node.module))
                        continue
                    self.assertIn(
                        node.module.split(".")[0], allowed, "{} imports {}".format(name, node.module)
                    )

    def test_example_help_works_in_a_subprocess(self) -> None:
        for name in ("rename_by_date.py", "note_digest.py", "clipboard_note.py"):
            result = subprocess.run(
                [sys.executable, os.path.join(EXAMPLES, name), "--help"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)


class TestObjectiveCFrameworkRecipes(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_example("objc_framework_recipes.py")

    def test_foundation_recipe_reads_the_main_bundle_path(self) -> None:
        foundation = types.ModuleType("Foundation")
        foundation.NSBundle = types.SimpleNamespace(
            mainBundle=types.SimpleNamespace(
                bundleURL=types.SimpleNamespace(path="/Pyto/Pyto.app")
            )
        )
        with mock.patch.dict(sys.modules, {"Foundation": foundation}):
            self.assertEqual(self.module["app_bundle_path"](), "/Pyto/Pyto.app")

    def test_uikit_recipe_reads_documented_device_properties(self) -> None:
        device = types.SimpleNamespace(model="iPhone", systemName="iOS", systemVersion="18.0")
        uikit = types.ModuleType("UIKit")
        uikit.UIDevice = types.SimpleNamespace(currentDevice=lambda: device)
        with mock.patch.dict(sys.modules, {"UIKit": uikit}):
            self.assertEqual(
                self.module["device_summary"](),
                {"model": "iPhone", "system": "iOS", "version": "18.0"},
            )

    def test_desktop_run_reports_missing_framework_instead_of_claiming_success(self) -> None:
        output = []
        with mock.patch.dict(sys.modules, {"Foundation": None, "UIKit": None}):
            with mock.patch("builtins.print", side_effect=output.append):
                self.module["main"]()
        self.assertIn("Objective-C recipe unavailable", output[0])
        self.assertIn("does not establish", output[0])


if __name__ == "__main__":
    unittest.main()

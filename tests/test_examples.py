"""Tests for the example programs, and for running an example through `run_program`.

The examples are what the agent is supposed to produce, so they are held to the same bar
as the harness: stdlib only, no network, no destructive default.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import unittest

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
            "textwrap", "collections", "pathlib", "shutil", "hashlib", "math", "csv", "io", "time",
        }
        # Pyto bridges the examples may use, always inside a `try: import` guard so the
        # program still runs off-device.
        optional_on_device = {"pasteboard", "share", "pyto", "notifications", "speech", "photos"}
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
                        self.assertIn(top, allowed, "{} imports {}".format(name, alias.name))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.level:
                        continue  # relative import inside a package: not used here
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


if __name__ == "__main__":
    unittest.main()

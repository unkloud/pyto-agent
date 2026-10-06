"""Shortcuts can invoke only an explicitly opted-in saved batch program."""

from __future__ import annotations

import contextlib
import io
import os

import run as harness_run
from harness import programs
from harness.tools_ios import Workspace

from .support import TempDirTestCase


class TestShortcutSavedRun(TempDirTestCase):
    def save(self, source: str, *, mode: str = "batch", input_schema=None):
        os.makedirs(self.workspace_dir, exist_ok=True)
        entry = os.path.join(self.workspace_dir, "shortcut program.py")
        with open(entry, "w", encoding="utf-8") as handle:
            handle.write(source)
        return programs.register(
            Workspace(self.workspace_dir),
            title="Shortcut program",
            purpose="A test automation.",
            entry_file="shortcut program.py",
            mode=mode,
            required_capabilities=[],
            input_schema=input_schema,
        )

    def invoke(self, record, *, values=(), allow=True):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = harness_run.run_saved_program_command(
                self.make_config(api_key=None),
                record["id"],
                input_assignments=values,
                shortcut=True,
                allow_unattended_saved_programs=allow,
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_shortcut_arguments_parse_without_shell_or_url_decoding(self) -> None:
        args = harness_run.build_parser().parse_args(
            [
                "--shortcut-run",
                "program-123",
                "--allow-unattended-saved-programs",
                "--input",
                "title=Café tea & toast = 2",
            ]
        )
        self.assertEqual(args.shortcut_run, "program-123")
        self.assertTrue(args.allow_unattended_saved_programs)
        self.assertEqual(args.program_inputs, ["title=Café tea & toast = 2"])

    def test_unattended_saved_program_flag_cannot_be_used_with_normal_chat_run(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            harness_run.main(["--allow-unattended-saved-programs", "task"])
        self.assertEqual(raised.exception.code, 2)

    def test_shortcut_requires_explicit_unattended_opt_in_before_execution(self) -> None:
        record = self.save("open('ran.txt', 'w').write('yes')\n")
        result, stdout, stderr = self.invoke(record, allow=False)
        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--allow-unattended-saved-programs", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "ran.txt")))

    def test_shortcut_runs_trusted_batch_program_with_validated_unicode_input_without_api_key(self) -> None:
        record = self.save(
            "def main(inputs):\n    print(inputs['title'])\n",
            input_schema=[{"name": "title", "label": "Title", "type": "text"}],
        )
        result, stdout, stderr = self.invoke(
            record,
            values=["title=Café tea & toast = 2"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("Café tea & toast = 2", stdout)
        self.assertEqual(stderr, "")

    def test_shortcut_rejects_invalid_or_missing_input_before_running_code(self) -> None:
        record = self.save(
            "open('ran.txt', 'w').write('yes')\n",
            input_schema=[{"name": "title", "label": "Title", "type": "text"}],
        )
        result, _stdout, stderr = self.invoke(record, values=[])
        self.assertEqual(result, 1)
        self.assertIn("Title is required", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "ran.txt")))

    def test_shortcut_does_not_open_file_picker_for_at_pick(self) -> None:
        record = self.save(
            "open('ran.txt', 'w').write('yes')\n",
            input_schema=[{"name": "folder", "label": "Folder", "type": "folder"}],
        )
        result, _stdout, stderr = self.invoke(record, values=["folder=@pick"])
        self.assertEqual(result, 1)
        self.assertIn("Shortcuts cannot open Pyto's file/folder picker", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "ran.txt")))

    def test_shortcut_rejects_interactive_apps_without_running_them(self) -> None:
        record = self.save("open('ran.txt', 'w').write('yes')\n", mode="app")
        result, stdout, stderr = self.invoke(record)
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("saved batch programs only", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "ran.txt")))


if __name__ == "__main__":
    import unittest

    unittest.main()

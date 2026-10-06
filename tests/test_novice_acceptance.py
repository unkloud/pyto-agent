"""A small desktop acceptance pack for the novice device-programming workflow.

These tests exercise saved-program persistence and honest permission-denial behavior. The
companion acceptance README joins them with the existing organizer, interactive-preview,
and interruption-recovery journeys; mocked UI and native bridges are not device evidence.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from harness import ios, programs
from harness.tools_ios import Workspace

from .support import ROOT, TempDirTestCase


class TestSavedClipboardNotebookJourney(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.makedirs(self.workspace_dir, exist_ok=True)
        self.entry = os.path.join(self.workspace_dir, "clipboard_note.py")
        shutil.copyfile(os.path.join(ROOT, "examples", "clipboard_note.py"), self.entry)
        self.inputs = [
            {"name": "text", "label": "Text", "type": "text", "required": False},
            {"name": "title", "label": "Title", "type": "text", "required": False},
            {"name": "tags", "label": "Tags", "type": "text", "required": False},
        ]
        self.record = programs.register(
            Workspace(self.workspace_dir),
            title="Clipboard notebook",
            purpose="Save copied text as a structured note.",
            entry_file="clipboard_note.py",
            mode="batch",
            required_capabilities=["pasteboard"],
            input_schema=self.inputs,
        )

    def _run_in_fresh_cli_process(self, *, text: str, title: str, tags: str):
        isolated_home = self.path("isolated-home")
        os.makedirs(isolated_home, exist_ok=True)
        env = os.environ.copy()
        for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "PYTO_HARNESS_API_KEY"):
            env.pop(key, None)
        env["PYTO_HARNESS_HOME"] = isolated_home
        command = [
            sys.executable,
            os.path.join(ROOT, "run.py"),
            "--workspace",
            self.workspace_dir,
            "--run-saved",
            self.record["id"],
            "--input",
            "text=" + text,
            "--input",
            "title=" + title,
            "--input",
            "tags=" + tags,
        ]
        started = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result, (time.perf_counter() - started) * 1000

    def test_save_reopen_run_twice_without_model_and_edit_selected_program(self) -> None:
        first_text = "https://example.org/trip?seat=A=B\n- [ ] pack tea\n"
        second_text = "Bring the paper map.\n"
        first, first_ms = self._run_in_fresh_cli_process(
            text=first_text, title="Trip", tags="travel, inbox"
        )
        second, second_ms = self._run_in_fresh_cli_process(
            text=second_text, title="Packing", tags="travel"
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(second.stderr, "")
        self.assertIn("Created", first.stdout)
        self.assertIn("Appended to", second.stdout)
        self.assertGreaterEqual(first_ms, 0)
        self.assertGreaterEqual(second_ms, 0)

        note_path = os.path.join(
            self.workspace_dir, "notes", datetime.date.today().isoformat() + ".md"
        )
        with open(note_path, encoding="utf-8") as handle:
            note = handle.read()
        self.assertIn("## Trip", note)
        self.assertIn("## Packing", note)
        self.assertIn("https://example.org/trip?seat=A=B", note)
        self.assertIn("#travel, #inbox", note)
        self.assertIn("Bring the paper map.", note)

        # A new Workspace instance models reopening the library from its persisted index.
        reopened = Workspace(self.workspace_dir)
        stored = programs.find_program(reopened, self.record["id"])
        self.assertEqual(stored["entry_file"], "clipboard_note.py")
        self.assertEqual(stored["last_verification_result"]["status"], "passed")
        edit_prompt = programs.build_edit_prompt(
            stored,
            "Add a checkbox section for packing tasks.",
            workspace=reopened,
        )
        self.assertIn('"entry_file": "clipboard_note.py"', edit_prompt)
        self.assertIn("Add a checkbox section for packing tasks.", edit_prompt)

        # The values are run-time input only; the saved library stores the schema, not data.
        with open(os.path.join(self.workspace_dir, programs.METADATA_FILE), encoding="utf-8") as handle:
            metadata = handle.read()
        self.assertNotIn("Bring the paper map.", metadata)
        if os.environ.get("PYTO_HARNESS_ACCEPTANCE_TIMINGS") == "1":
            print(
                "saved_notebook_fresh_process_ms: first={:.1f}, second={:.1f}".format(
                    first_ms, second_ms
                )
            )

    def test_saved_notebook_uses_clipboard_when_text_input_is_omitted(self) -> None:
        pasteboard = types.ModuleType("pasteboard")
        pasteboard.get = lambda: "Copied from the device bridge\n- [ ] file this"  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"pasteboard": pasteboard}):
            module = runpy.run_path(self.entry, run_name="saved_notebook_acceptance")
            with contextlib.redirect_stdout(io.StringIO()):
                result = module["main"]({"text": None, "title": "Clipboard", "tags": "inbox"})

        self.assertEqual(result, 0)
        target = os.path.join(
            self.workspace_dir, "notes", datetime.date.today().isoformat() + ".md"
        )
        with open(target, encoding="utf-8") as handle:
            note = handle.read()
        self.assertIn("Copied from the device bridge", note)
        self.assertIn("[ ] file this", note)


class TestPermissionDenialAcceptance(TempDirTestCase):
    def test_photos_denial_is_reported_as_failure_without_claiming_a_save(self) -> None:
        image_path = self.path("sample.png")
        with open(image_path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        photos = types.ModuleType("photos")

        def deny(_path: str) -> None:
            raise PermissionError("user denied photo-library access")

        photos.save_image = deny  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"photos": photos}):
            result = ios.save_photo(image_path)

        self.assertFalse(result.ok)
        self.assertTrue(result.supported)
        self.assertEqual(result.method, "photos.save_image")
        self.assertIn("PermissionError", result.detail)
        self.assertIn("permission is required", result.detail)
        self.assertIn("user denied", result.detail)


if __name__ == "__main__":
    unittest.main()

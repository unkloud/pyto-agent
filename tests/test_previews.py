"""Desktop contract tests for static preview checks and PytoUI lifecycle handling."""

from __future__ import annotations

import json
import importlib.util
import os
import sys
import tempfile
import threading
import time
import types
import urllib.request
import unittest
from unittest import mock

from harness import unix_tools
from harness.errors import ToolError
from harness.previews import PreviewMonitor, run_preview, validate_source
from harness.tools_ios import default_context, build_registry

from .support import TempDirTestCase


class FakeView:
    def __init__(self) -> None:
        self.title = ""
        self.background_color = None
        self.width = 390
        self.height = 780
        self.subviews = []
        self.closed = False

    def add_subview(self, item) -> None:
        self.subviews.append(item)

    def close(self) -> None:
        self.closed = True


class FakeElement:
    def __init__(self, text="", placeholder=None, title="") -> None:
        self.text = text
        self.placeholder = placeholder
        self.title = title
        self.frame = (0, 0, 0, 0)
        self.action = None
        self.text_color = None
        self.number_of_lines = 1


class FakePytoUI(types.ModuleType):
    def __init__(self, *, actions=(), entered=None, release=None) -> None:
        super().__init__("pyto_ui")
        self.View = FakeView
        self.Label = lambda text="": FakeElement(text=text)
        self.TextView = lambda: FakeElement()
        self.TextField = lambda text="", placeholder=None: FakeElement(text=text, placeholder=placeholder)
        self.Button = lambda title="": FakeElement(title=title)
        self.COLOR_SYSTEM_BACKGROUND = "system-background"
        self.COLOR_SECONDARY_SYSTEM_BACKGROUND = "secondary-system-background"
        self.COLOR_SYSTEM_RED = "system-red"
        self.actions_to_fire = list(actions)
        self.entered = entered or threading.Event()
        self.release = release
        self.presentations = 0
        self.last_view = None

    def show_view(self, view, mode=None) -> None:
        self.presentations += 1
        self.last_view = view
        self.entered.set()
        for label, value in getattr(self, "text_values", {}).items():
            for item in view.subviews:
                if isinstance(item, FakeElement) and item.placeholder == label:
                    item.text = value
        if self.release is not None:
            self.release.wait()
        else:
            for title in self.actions_to_fire:
                for item in list(view.subviews):
                    if isinstance(item, FakeElement) and item.title == title and item.action is not None:
                        item.action(item)


class TestPreviewValidation(TempDirTestCase):
    def test_syntax_error_is_reported_with_line_and_column(self) -> None:
        result = validate_source("if True print('bad')\n", path="broken.py", workspace_root=self.workspace_dir)
        self.assertFalse(result["passed"])
        self.assertFalse(result["syntax_passed"])
        self.assertIn("SyntaxError at line 1", result["errors"][0])

    def test_missing_top_level_import_is_reported_without_running_the_program(self) -> None:
        marker = self.path("import-was-run")
        module = os.path.join(self.workspace_dir, "side_effect_module.py")
        os.makedirs(self.workspace_dir)
        with open(module, "w", encoding="utf-8") as handle:
            handle.write("open({!r}, 'w').write('ran')\n".format(marker))
        source = "import side_effect_module\nimport definitely_missing_preview_dependency\n"
        result = validate_source(source, path=os.path.join(self.workspace_dir, "app.py"), workspace_root=self.workspace_dir)
        self.assertFalse(result["passed"])
        self.assertIn("definitely_missing_preview_dependency", result["errors"][0])
        self.assertFalse(os.path.exists(marker))

    def test_local_and_relative_imports_are_reported_as_static_checks(self) -> None:
        os.makedirs(self.workspace_dir)
        with open(os.path.join(self.workspace_dir, "helper.py"), "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        result = validate_source(
            "import helper\nfrom . import sibling\n",
            path=os.path.join(self.workspace_dir, "app.py"),
            workspace_root=self.workspace_dir,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["imports"], ["helper"])
        self.assertEqual(result["relative_imports"], ["."])

    def test_interactive_scaffold_pure_logic_is_independently_testable(self) -> None:
        logic_path = os.path.join(os.path.dirname(__file__), "..", "examples", "interactive_logic.py")
        spec = importlib.util.spec_from_file_location("interactive_logic_test", logic_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.update_count(8), 9)
        self.assertEqual(module.validate_form("  note ✓  "), "note ✓")
        with self.assertRaisesRegex(ValueError, "Enter a note"):
            module.validate_form("   ")
        self.assertEqual(module.decode_status(b"  status\n"), "status")
        self.assertIn("�", module.decode_status(b"\xff"))


class TestPreviewMonitor(unittest.TestCase):
    def test_guard_records_success_and_surfaces_callback_errors(self) -> None:
        monitor = PreviewMonitor()
        displayed = []
        seen = []

        def broken(_sender):
            raise ValueError("bad callback")

        wrapped = monitor.guard(broken, on_error=lambda error: displayed.append(str(error)), label="save")
        wrapped(object())
        monitor.guard(lambda _sender: seen.append("ok"))(object())

        state = monitor.snapshot()
        self.assertEqual(state["callback_attempts"], 2)
        self.assertEqual(state["callback_successes"], 1)
        self.assertTrue(state["interaction_verified"])
        self.assertEqual(displayed, ["bad callback"])
        self.assertEqual(state["callback_errors"], ["save — ValueError: bad callback"])


class TestInteractivePreview(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._original_pyto_ui = sys.modules.get("pyto_ui")
        self.addCleanup(self._restore_pyto_ui)

    def _restore_pyto_ui(self) -> None:
        if self._original_pyto_ui is None:
            sys.modules.pop("pyto_ui", None)
        else:
            sys.modules["pyto_ui"] = self._original_pyto_ui

    def _write(self, relative: str, source: str) -> str:
        target = os.path.join(self.workspace_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(source)
        return target

    def _run(self, target: str):
        return run_preview(target, workspace_root=self.workspace_dir)

    def test_saved_input_app_receives_values_through_main_contract(self) -> None:
        fake_ui = FakePytoUI()
        sys.modules["pyto_ui"] = fake_ui
        target = self._write(
            "input_app.py",
            """import pyto_ui as ui
def main(inputs):
    view = ui.View()
    view.add_subview(ui.Label(inputs['title']))
    harness_preview.present(view, ui)
""",
        )
        result = run_preview(
            target,
            workspace_root=self.workspace_dir,
            input_values={"title": "Résumé"},
        )
        self.assertFalse(result["is_error"], result)
        self.assertTrue(result["monitor"]["preview_opened"])
        self.assertEqual(fake_ui.last_view.subviews[0].text, "Résumé")

    def test_folder_organizer_preview_is_read_only_until_apply_button(self) -> None:
        examples = os.path.join(os.path.dirname(__file__), "..", "examples")
        for name in ("folder_organizer_app.py", "folder_organizer_logic.py"):
            with open(os.path.join(examples, name), encoding="utf-8") as handle:
                self._write(name, handle.read())
        folder = self.path("source files")
        os.mkdir(folder)
        source = os.path.join(folder, "résumé.pdf")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("keep this file")
        target = os.path.join(self.workspace_dir, "folder_organizer_app.py")
        values = {
            "folder": folder,
            "group_by": "extension",
            "collection_name": "Sorted",
            "max_files": 20,
        }

        preview_ui = FakePytoUI()
        sys.modules["pyto_ui"] = preview_ui
        preview = run_preview(target, workspace_root=self.workspace_dir, input_values=values)
        self.assertFalse(preview["is_error"], preview)
        self.assertTrue(os.path.isfile(source))
        self.assertFalse(os.path.exists(os.path.join(folder, "Sorted")))
        self.assertIn("résumé.pdf", preview_ui.last_view.subviews[2].text)

        apply_ui = FakePytoUI(actions=("Apply these moves", "Undo these moves"))
        sys.modules["pyto_ui"] = apply_ui
        applied_and_undone = run_preview(target, workspace_root=self.workspace_dir, input_values=values)
        self.assertFalse(applied_and_undone["is_error"], applied_and_undone)
        destination = os.path.join(folder, "Sorted", "pdf", "résumé.pdf")
        self.assertTrue(os.path.isfile(source))
        self.assertFalse(os.path.exists(destination))
        self.assertFalse(os.path.exists(os.path.join(folder, "Sorted")))
        self.assertEqual(applied_and_undone["monitor"]["callback_successes"], 2)
        self.assertIn("Restored 1 file(s)", apply_ui.last_view.subviews[3].text)

    def test_scaffold_counter_form_and_mocked_network_run_through_preview(self) -> None:
        fake_ui = FakePytoUI(actions=("Add one", "Save note", "Refresh", "Close"))
        fake_ui.text_values = {"A note to save": "  Hello 🚀  "}
        sys.modules["pyto_ui"] = fake_ui

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return "Mocked network status ✓".encode("utf-8")

        with open(
            os.path.join(os.path.dirname(__file__), "..", "examples", "interactive_app_scaffold.py"),
            encoding="utf-8",
        ) as handle:
            scaffold_source = handle.read()
        target = self._write("interactive_app_scaffold.py", scaffold_source)
        with open(
            os.path.join(os.path.dirname(__file__), "..", "examples", "interactive_logic.py"),
            encoding="utf-8",
        ) as handle:
            self._write("interactive_logic.py", handle.read())
        with mock.patch.object(urllib.request, "urlopen", return_value=Response()) as urlopen:
            result = self._run(target)

        self.assertFalse(result["is_error"], result)
        self.assertTrue(result["validation"]["passed"])
        self.assertTrue(result["monitor"]["preview_opened"])
        self.assertTrue(result["monitor"]["preview_closed"])
        self.assertTrue(result["monitor"]["interaction_verified"])
        self.assertEqual(result["monitor"]["callback_successes"], 4)
        self.assertFalse(result["execution_lane_busy"])
        self.assertEqual(urlopen.call_args.args[0], "https://www.iana.org/domains/reserved")
        with open(os.path.join(self.workspace_dir, "interactive_app_state.json"), encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state, {"count": 1, "note": "Hello 🚀"})
        self.assertTrue(any("Mocked network status ✓" in item.text for item in fake_ui.last_view.subviews))

        # A second launch reloads the state written by the first preview.
        fake_ui.actions_to_fire = ["Close"]
        fake_ui.text_values = {}
        second = self._run(target)
        self.assertFalse(second["is_error"], second)
        self.assertTrue(
            any(item.placeholder == "A note to save" and item.text == "Hello 🚀" for item in fake_ui.last_view.subviews),
            [(getattr(item, "placeholder", None), getattr(item, "text", None)) for item in fake_ui.last_view.subviews],
        )

    def test_callback_error_is_returned_and_preview_closes_cleanly(self) -> None:
        fake_ui = FakePytoUI(actions=("Break", "Close"))
        sys.modules["pyto_ui"] = fake_ui
        target = self._write(
            "broken_callback.py",
            """import pyto_ui as ui
view = ui.View()
message = ui.Label(\"\")
view.add_subview(message)
button = ui.Button(title=\"Break\")
def broken(_sender):
    raise ValueError(\"callback exploded\")
button.action = harness_preview.guard(broken, on_error=lambda exc: setattr(message, \"text\", str(exc)), label=\"break button\")
view.add_subview(button)
close = ui.Button(title=\"Close\")
close.action = harness_preview.guard(lambda _sender: harness_preview.close(view), label=\"close\")
view.add_subview(close)
harness_preview.present(view, ui)
""",
        )
        result = self._run(target)
        self.assertTrue(result["is_error"])
        self.assertTrue(result["monitor"]["preview_opened"])
        self.assertTrue(result["monitor"]["preview_closed"])
        self.assertEqual(result["monitor"]["callback_errors"], ["break button — ValueError: callback exploded"])
        self.assertEqual(fake_ui.last_view.subviews[0].text, "callback exploded")
        self.assertTrue(result["monitor"]["stop_requested"])

    def test_preview_tool_has_no_batch_timeout_and_requires_an_explicit_present_call(self) -> None:
        fake_ui = FakePytoUI()
        sys.modules["pyto_ui"] = fake_ui
        registry = build_registry(default_context(self.workspace_dir, self.path("spill")))
        self.assertIsNone(registry.get("preview_program").timeout)
        target = self._write("no_view.py", "import pyto_ui as ui\nview = ui.View()\n")
        result = self._run(target)
        self.assertTrue(result["is_error"])
        self.assertFalse(result["monitor"]["presentation_requested"])
        self.assertIn("harness_preview.present", result["error"])

    def test_surviving_background_work_keeps_the_lane_until_it_exits(self) -> None:
        fake_ui = FakePytoUI()
        sys.modules["pyto_ui"] = fake_ui
        hooks = types.ModuleType("preview_test_hooks")
        hooks.release = threading.Event()
        sys.modules["preview_test_hooks"] = hooks
        self.addCleanup(sys.modules.pop, "preview_test_hooks", None)
        target = self._write(
            "survivor.py",
            """import threading
import preview_test_hooks as hooks
import pyto_ui as ui
worker = threading.Thread(target=hooks.release.wait, name=\"preview-owned-survivor\", daemon=True)
worker.start()
view = ui.View()
harness_preview.present(view, ui)
""",
        )
        result = self._run(target)
        self.assertTrue(result["is_error"])
        self.assertTrue(result["cleanup_pending"])
        self.assertTrue(result["execution_lane_busy"])
        self.assertIn("preview-owned-survivor", result["surviving_threads"])
        with self.assertRaises(ToolError):
            unix_tools.IN_PROCESS_EXECUTION_LANE.ensure_idle("read the workspace")
        hooks.release.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                unix_tools.IN_PROCESS_EXECUTION_LANE.ensure_idle("read the workspace")
                break
            except ToolError:
                time.sleep(0.02)
        else:
            self.fail("the preview worker did not release the execution lane after its child exited")

    def test_view_open_longer_than_batch_limit_is_not_timed_out(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        fake_ui = FakePytoUI(entered=entered, release=release)
        sys.modules["pyto_ui"] = fake_ui
        target = self._write(
            "long_preview.py",
            "import pyto_ui as ui\nview = ui.View()\nharness_preview.present(view, ui)\n",
        )
        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.update(self._run(target)), name="long-preview-test"
        )
        worker.start()
        self.assertTrue(entered.wait(5.0), "preview did not reach show_view")
        time.sleep(30.1)
        self.assertTrue(worker.is_alive(), "preview was incorrectly bounded by the batch timeout")
        release.set()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(outcome["is_error"], outcome)
        self.assertTrue(outcome["monitor"]["preview_opened"])
        self.assertTrue(outcome["monitor"]["preview_closed"])
        self.assertFalse(outcome["execution_lane_busy"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

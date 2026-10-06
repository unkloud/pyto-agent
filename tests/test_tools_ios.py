"""Model-facing tool tests: the workspace jail, program runs, memory and budgets."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import unittest

from harness import budget, ios, programs
from harness.errors import ToolError
from harness.tools_ios import Workspace, _load_memory, default_context

from .support import TempDirTestCase


class TestWorkspaceJail(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Workspace(self.workspace_dir)

    def test_relative_path_resolves_inside(self) -> None:
        resolved = self.workspace.resolve("notes/today.md")
        self.assertTrue(resolved.startswith(os.path.realpath(self.workspace_dir)))

    def test_parent_escape_is_refused(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.workspace.resolve("../outside.py")
        self.assertIn("outside the workspace", str(caught.exception))

    def test_deep_escape_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.resolve("a/b/../../../etc/passwd")

    def test_absolute_path_outside_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.resolve("/etc/passwd")

    def test_absolute_path_inside_is_allowed(self) -> None:
        inside = os.path.join(self.workspace_dir, "ok.txt")
        self.assertEqual(self.workspace.resolve(inside), os.path.realpath(inside))

    def test_symlink_escape_is_refused(self) -> None:
        link = os.path.join(self.workspace_dir, "link")
        try:
            os.symlink(self.tmp, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ToolError):
            self.workspace.resolve("link/escape.txt")

    def test_empty_path_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.resolve("")

    def test_nul_byte_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.resolve("a\x00b")

    def test_must_exist(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.resolve("missing.txt", must_exist=True)


class TestWriteProgram(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = self.make_registry()

    def call(self, name: str, **args):
        return self.registry._tools[name].handler(**args)

    def test_writes_the_file_with_a_header(self) -> None:
        result = self.call("write_program", path="demo.py", source="print('hi')\n", purpose="say hi")
        self.assertFalse(result.is_error)
        path = os.path.join(self.workspace_dir, "demo.py")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("say hi", text.splitlines()[0])
        self.assertIn("pyto-harness", text)
        self.assertIn("print('hi')", text)

    def test_workspace_escape_is_refused(self) -> None:
        result = self.registry._tools["write_program"].handler
        with self.assertRaises(ToolError):
            result(path="../escape.py", source="print(1)")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.py")))

    def test_absolute_escape_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.call("write_program", path="/tmp/should-not-exist-pyto.py", source="print(1)")
        self.assertFalse(os.path.exists("/tmp/should-not-exist-pyto.py"))

    def test_py_extension_is_added(self) -> None:
        self.call("write_program", path="noext", source="print(1)")
        self.assertTrue(os.path.exists(os.path.join(self.workspace_dir, "noext.py")))

    def test_overwrite_is_reported(self) -> None:
        self.call("write_program", path="a.py", source="print(1)")
        result = self.call("write_program", path="a.py", source="print(2)")
        self.assertTrue(result.metadata.get("overwrote"))
        with open(os.path.join(self.workspace_dir, "a.py"), encoding="utf-8") as handle:
            self.assertIn("print(2)", handle.read())

    def test_subdirectory_is_created(self) -> None:
        self.call("write_program", path="tools/x.py", source="print(1)")
        self.assertTrue(os.path.exists(os.path.join(self.workspace_dir, "tools", "x.py")))

    def test_register_and_list_saved_program_tools(self) -> None:
        self.call("write_program", path="tools/daily.py", source="print('daily')", purpose="Daily job")
        registered = self.call(
            "register_program",
            title="Daily job",
            purpose="Print the daily status.",
            entry_file="tools/daily.py",
            mode="batch",
            required_capabilities=["files"],
        )
        self.assertFalse(registered.is_error, registered.content)
        self.assertIn("/run {}".format(registered.metadata["program_id"]), registered.content)
        listed = self.call("list_saved_programs")
        self.assertFalse(listed.is_error, listed.content)
        self.assertEqual(listed.metadata["count"], 1)
        self.assertIn("Daily job", listed.content)

    def test_project_brief_tools_read_and_update_only_the_selected_program(self) -> None:
        self.call("write_program", path="daily.py", source="print('daily')")
        registered = self.call(
            "register_program",
            title="Daily job",
            purpose="Print the daily status.",
            entry_file="daily.py",
            mode="batch",
        )
        program_id = registered.metadata["program_id"]

        updated = self.call(
            "project_brief_update",
            program_id=program_id,
            requirements=["Keep the report on this device."],
            decisions=["Use a weekly heading."],
            related_files=["daily.py"],
            unresolved=["Choose the export format."],
        )
        self.assertFalse(updated.is_error, updated.content)
        loaded = self.call("project_brief_read", program_id=program_id)
        self.assertFalse(loaded.is_error, loaded.content)
        detail = json.loads(loaded.content)
        self.assertEqual(detail["project_brief"]["requirements"], ["Keep the report on this device."])
        self.assertEqual(detail["files"], [{"path": "daily.py", "status": "present"}])

        self.call(
            "project_brief_update",
            program_id=program_id,
            unresolved=[],
        )
        again = json.loads(self.call("project_brief_read", program_id=program_id).content)
        self.assertEqual(again["project_brief"]["requirements"], detail["project_brief"]["requirements"])
        self.assertEqual(again["project_brief"]["unresolved"], [])

    def test_project_brief_tool_rejects_paths_outside_workspace(self) -> None:
        self.call("write_program", path="daily.py", source="print('daily')")
        registered = self.call(
            "register_program",
            title="Daily job",
            purpose="Print the daily status.",
            entry_file="daily.py",
            mode="batch",
        )
        with self.assertRaisesRegex(ToolError, "stay inside the workspace"):
            self.call("project_brief_update", program_id=registered.metadata["program_id"], related_files=["../secrets.txt"])

    def test_saved_program_index_cannot_be_overwritten_by_file_tools(self) -> None:
        with self.assertRaises(ToolError):
            self.call("write_file", path="pyto-programs.json", content="{}")

    def test_rewriting_a_registered_program_clears_its_previous_verification(self) -> None:
        self.call("write_program", path="daily.py", source="print('first')")
        registered = self.call(
            "register_program",
            title="Daily",
            purpose="Print a daily message.",
            entry_file="daily.py",
            mode="batch",
        )
        context = self.registry.context
        programs.update_verification(context.workspace, registered.metadata["program_id"], status="passed", summary="It ran.")
        rewritten = self.call("write_program", path="daily.py", source="print('second')")
        self.assertIn("previous verification was cleared", rewritten.content)
        stored = programs.find_program(context.workspace, registered.metadata["program_id"])
        self.assertEqual(stored["last_verification_result"]["status"], "not_run")

class TestRunProgram(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = self.make_registry()
        self.original = ios._REAL_SUBPROCESS
        self.addCleanup(setattr, ios, "_REAL_SUBPROCESS", self.original)

    def call(self, **args):
        return self.registry._tools["run_program"].handler(**args)

    def test_stdout_is_captured(self) -> None:
        result = self.call(path_or_source="print('hello from the program')")
        self.assertFalse(result.is_error)
        self.assertIn("hello from the program", result.content)
        self.assertIn("exit code: 0", result.content)

    def test_arguments_are_passed(self) -> None:
        self.registry._tools["write_program"].handler(
            path="argv.py", source="import sys\nprint('ARGS', sys.argv[1:])\n"
        )
        result = self.call(path_or_source="argv.py", args=["one", "two"])
        self.assertIn("ARGS ['one', 'two']", result.content)

    def test_registered_input_program_uses_validated_main_contract(self) -> None:
        self.registry._tools["write_program"].handler(
            path="input_program.py",
            source="def main(inputs):\n    print(type(inputs['count']).__name__, inputs['count'])\n",
        )
        self.registry._tools["register_program"].handler(
            title="Input program",
            purpose="Print a typed value.",
            entry_file="input_program.py",
            mode="batch",
            input_schema=[{"name": "count", "label": "Count", "type": "number", "integer": True}],
        )
        result = self.call(path_or_source="input_program.py", input_values={"count": "12"})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("int 12", result.content)
        with self.assertRaisesRegex(ToolError, "whole number"):
            self.call(path_or_source="input_program.py", input_values={"count": "12.5"})

    def test_nonzero_exit_is_an_error_result(self) -> None:
        result = self.call(path_or_source="import sys\nsys.exit(3)\n")
        self.assertTrue(result.is_error)
        self.assertIn("exit code: 3", result.content)

    def test_traceback_lands_in_stderr(self) -> None:
        result = self.call(path_or_source="raise ValueError('boom')\n")
        self.assertTrue(result.is_error)
        self.assertIn("ValueError", result.content)
        self.assertIn("stderr", result.content)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.call(path_or_source="nothing_here.py")

    def test_escape_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.call(path_or_source="../../../etc/passwd.py")

    def test_real_subprocess_kills_on_timeout(self) -> None:
        ios._REAL_SUBPROCESS = True
        self.registry._tools["write_program"].handler(path="spin.py", source="import time\ntime.sleep(30)\n")
        started = time.monotonic()
        result = self.call(path_or_source="spin.py", timeout_s=1.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.is_error)
        self.assertTrue(result.metadata["timed_out"])
        self.assertEqual(result.metadata["mode"], "subprocess")
        self.assertLess(elapsed, 6.0)

    def test_in_process_timeout_interrupts_a_pure_python_loop(self) -> None:
        """On Pyto there is no real fork, so the timeout must be cooperative."""
        ios._REAL_SUBPROCESS = False  # simulate Pyto's fake subprocess
        self.registry._tools["write_program"].handler(
            path="spin2.py", source="print('started', flush=True)\nwhile True:\n    pass\n"
        )
        started = time.monotonic()
        result = self.call(path_or_source="spin2.py", timeout_s=1.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.metadata["timed_out"])
        self.assertEqual(result.metadata["mode"], "runpy")
        self.assertEqual(result.metadata["returncode"], 124)
        self.assertIn("started", result.content)
        self.assertIn("cooperatively", result.content)
        self.assertLess(elapsed, 12.0)

    def test_in_process_captures_stdout_and_exit_code(self) -> None:
        ios._REAL_SUBPROCESS = False
        result = self.call(path_or_source="import sys\nprint('captured')\nsys.exit(2)\n")
        self.assertIn("captured", result.content)
        self.assertEqual(result.metadata["returncode"], 2)
        self.assertEqual(result.metadata["mode"], "runpy")

    def test_in_process_runs_do_not_overlap_or_mix_output(self) -> None:
        ios._REAL_SUBPROCESS = False
        start = threading.Barrier(3)
        results = {}

        def invoke(label: str) -> None:
            start.wait()
            results[label] = self.call(
                path_or_source=(
                    "import time\nprint('{0}-start')\ntime.sleep(0.1)\nprint('{0}-end')\n".format(label)
                ),
                timeout_s=1.0,
            )

        workers = [threading.Thread(target=invoke, args=(label,)) for label in ("first", "second")]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=5.0)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), 2)
        completed = [label for label, result in results.items() if not result.is_error]
        rejected = [label for label, result in results.items() if result.metadata.get("execution_lane_busy")]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(rejected), 1)
        output = results[completed[0]].content
        self.assertIn(completed[0] + "-start", output)
        self.assertIn(completed[0] + "-end", output)
        self.assertNotIn(rejected[0] + "-start", output)

    def test_in_process_survivor_holds_the_lane_until_it_exits(self) -> None:
        ios._REAL_SUBPROCESS = False
        first = self.call(
            path_or_source="import time\ntime.sleep(4)\nprint('late completion')\n",
            timeout_s=0.5,
        )
        self.assertTrue(first.is_error)
        self.assertTrue(first.metadata["execution_lane_busy"])
        self.assertFalse(first.metadata["timeout_enforced"])
        self.assertIn("may still change workspace files or process state", first.content)

        rejected = self.call(path_or_source="print('must not launch')\n", timeout_s=1.0)
        self.assertTrue(rejected.is_error)
        self.assertTrue(rejected.metadata["execution_lane_busy"])
        self.assertNotIn("must not launch", rejected.content)

        deadline = time.monotonic() + 6.0
        while True:
            recovered = self.call(path_or_source="print('lane recovered')\n", timeout_s=1.0)
            if not recovered.metadata.get("execution_lane_busy"):
                break
            if time.monotonic() >= deadline:
                self.fail("the execution lane was not released after the worker exited")
            time.sleep(0.05)
        self.assertFalse(recovered.is_error)
        self.assertIn("lane recovered", recovered.content)

    def test_in_process_waits_for_program_threads_and_restores_state_after_error(self) -> None:
        ios._REAL_SUBPROCESS = False
        original_argv = sys.argv
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        original_cwd = os.getcwd()
        marker = "HARNESS_TEST_ENV_VALUE"
        old_marker = os.environ.get(marker)

        def restore_marker() -> None:
            if old_marker is None:
                os.environ.pop(marker, None)
            else:
                os.environ[marker] = old_marker

        self.addCleanup(restore_marker)
        os.environ[marker] = "before"
        result = self.call(
            path_or_source=(
                "import os, sys, threading, time\n"
                "os.environ['HARNESS_TEST_ENV_VALUE'] = 'changed'\n"
                "sys.argv = ['changed']\n"
                "os.chdir('/')\n"
                "threading.Thread(target=lambda: time.sleep(0.05), name='demo-child', daemon=True).start()\n"
                "raise RuntimeError('expected failure')\n"
            ),
            timeout_s=1.0,
        )

        self.assertTrue(result.is_error)
        self.assertIn("expected failure", result.content)
        self.assertIn("waited for them to exit", result.content)
        self.assertIs(sys.argv, original_argv)
        self.assertIs(sys.stdout, original_stdout)
        self.assertIs(sys.stderr, original_stderr)
        self.assertEqual(os.getcwd(), original_cwd)
        self.assertEqual(os.environ.get(marker), "before")

    def test_program_runs_with_the_workspace_as_cwd(self) -> None:
        self.call(path_or_source="import os\nprint('CWD', os.path.basename(os.getcwd()))\n")
        result = self.call(path_or_source="import os\nprint('CWD', os.path.basename(os.getcwd()))\n")
        self.assertIn("CWD {}".format(os.path.basename(os.path.realpath(self.workspace_dir))), result.content)

    def test_run_counter_and_cap(self) -> None:
        context = self.registry.context
        self.call(path_or_source="print(1)")
        self.assertEqual(context.runs, 1)


class TestFileTools(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = self.make_registry()

    def call(self, name: str, **args):
        return self.registry._tools[name].handler(**args)

    def test_write_read_round_trip(self) -> None:
        self.call("write_file", path="note.md", content="line one\nline two\n")
        result = self.call("read_file", path="note.md")
        self.assertIn("line one", result.content)
        self.assertIn("line two", result.content)

    def test_edit_replaces_exactly(self) -> None:
        self.call("write_file", path="n.txt", content="alpha beta gamma")
        result = self.call("edit_file", path="n.txt", old="beta", new="BETA")
        self.assertEqual(result.metadata["occurrences"], 1)
        self.assertIn("BETA", self.call("read_file", path="n.txt").content)

    def test_edit_missing_text_changes_nothing(self) -> None:
        self.call("write_file", path="n.txt", content="alpha")
        with self.assertRaises(ToolError):
            self.call("edit_file", path="n.txt", old="zzz", new="yyy")
        self.assertEqual(self.call("read_file", path="n.txt").content.split(":\n", 1)[1], "alpha")

    def test_edit_count_limits_replacements(self) -> None:
        self.call("write_file", path="n.txt", content="x x x")
        result = self.call("edit_file", path="n.txt", old="x", new="y", count=2)
        self.assertEqual(result.metadata["occurrences"], 3)
        self.assertIn("Replaced 2 of 3", result.content)

    def test_list_files_newest_first(self) -> None:
        self.call("write_file", path="old.txt", content="a")
        time.sleep(0.01)
        self.call("write_file", path="new.txt", content="b")
        listing = self.call("list_files", pattern="*.txt")
        self.assertLess(listing.content.index("new.txt"), listing.content.index("old.txt"))

    def test_list_files_respects_glob(self) -> None:
        self.call("write_file", path="a.py", content="1")
        self.call("write_file", path="b.md", content="2")
        listing = self.call("list_files", pattern="*.py")
        self.assertIn("a.py", listing.content)
        self.assertNotIn("b.md", listing.content)

    def test_read_refuses_a_directory(self) -> None:
        os.makedirs(os.path.join(self.workspace_dir, "sub"), exist_ok=True)
        with self.assertRaises(ToolError):
            self.call("read_file", path="sub")

    def test_binary_file_is_refused(self) -> None:
        with open(os.path.join(self.workspace_dir, "blob.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02binary")
        with self.assertRaises(ToolError):
            self.call("read_file", path="blob.bin")

    def test_write_size_budget_is_enforced(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.call("write_file", path="huge.txt", content="x" * (budget.MAX_WRITE_BYTES + 10))
        self.assertIn("single-write limit", str(caught.exception))

    def test_search_finds_and_caps(self) -> None:
        for index in range(5):
            self.call("write_file", path="f{}.txt".format(index), content="needle {}\n".format(index))
        found = self.call("search_files", query="needle", limit=3)
        self.assertIn("match", found.content)
        self.assertEqual(found.metadata["matches"], 3)
        self.assertIn("match cap", found.content)

    def test_search_regex_and_plain(self) -> None:
        self.call("write_file", path="f.txt", content="abc123\nxyz\n")
        self.assertIn("abc123", self.call("search_files", query=r"abc\d+", regex=True).content)
        self.assertEqual(self.call("search_files", query="nomatch").metadata["matches"], 0)

    def test_search_invalid_regex_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self.call("search_files", query="([", regex=True)


class TestMemoryStore(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = self.make_registry()

    def call(self, name: str, **args):
        return self.registry._tools[name].handler(**args)

    def test_empty_store(self) -> None:
        self.assertIn("No saved facts", self.call("memory_read").content)

    def test_write_then_read(self) -> None:
        self.call("memory_write", key="screenshots_folder", value="~/Screenshots")
        self.assertIn("~/Screenshots", self.call("memory_read").content)

    def test_update_is_reported(self) -> None:
        self.call("memory_write", key="city", value="Berlin")
        result = self.call("memory_write", key="city", value="Lisbon")
        self.assertTrue(result.metadata["replaced"])
        self.assertIn("Lisbon", self.call("memory_read").content)

    def test_persists_as_json(self) -> None:
        self.call("memory_write", key="unit", value="celsius")
        path = os.path.join(self.workspace_dir, "memory.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["facts"]["unit"], "celsius")
        self.assertEqual(payload["version"], 1)

    def test_corrupt_store_is_survivable(self) -> None:
        path = os.path.join(self.workspace_dir, "memory.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIn("No saved facts", self.call("memory_read").content)
        self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_key_pattern_is_enforced(self) -> None:
        from harness.schema import assert_valid
        from harness.errors import ValidationError

        schema = self.registry._tools["memory_write"].parameters
        with self.assertRaises(ValidationError):
            assert_valid({"key": "has spaces", "value": "x"}, schema)


class TestDeviceToolsDegrade(TempDirTestCase):
    """Every device tool must fail politely off-device, never crash the turn."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = self.make_registry()

    def _result(self, tool_name: str, **args):
        return self.registry._tools[tool_name].handler(**args)

    def test_clipboard_get_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self._result("clipboard_get")
        self.assertIn("pasteboard", str(caught.exception))

    def test_clipboard_set_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self._result("clipboard_set", text="hi")

    def test_notify_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self._result("notify", title="done")

    def test_speak_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self._result("speak", text="hello")

    def test_share_text_falls_back_to_a_file(self) -> None:
        # With no share bridge the adapter writes a file and reports the path.
        result = self._result("share_text", text="share me", title="t")
        self.assertIn("file-fallback", result.content)

    def test_photo_requires_a_real_image(self) -> None:
        with self.assertRaises(ToolError):
            self._result("save_photo", path="missing.png")

    def test_photo_rejects_a_non_image(self) -> None:
        self.registry._tools["write_file"].handler(path="notes.txt", content="hi")
        with self.assertRaises(ToolError) as caught:
            self._result("save_photo", path="notes.txt")
        self.assertIn("image extension", str(caught.exception))

    def test_calendar_add_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self._result("calendar_add_event", title="Standup", start_iso="2024-05-01T09:00")

    def test_calendar_bad_iso_is_reported(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self._result("calendar_add_event", title="X", start_iso="tomorrow")
        self.assertIn("ISO", str(caught.exception))

    def test_keepalive_start_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            self._result("keepalive_start", label="job")

    def test_keepalive_stop_is_idempotent(self) -> None:
        self.assertIn("no background task", self._result("keepalive_stop").content)

    def test_memory_status_always_answers(self) -> None:
        self.assertIn("memory_status", self._result("memory_status").content)

    def test_device_capabilities_lists_probes(self) -> None:
        result = self._result("device_capabilities")
        for name in ("pasteboard", "share", "photos", "calendar_events", "background"):
            self.assertIn(name, result.content)

    def test_shortcut_run_needs_a_name(self) -> None:
        with self.assertRaises(ToolError):
            self._result("shortcut_run", name="")


class TestFinishTool(TempDirTestCase):
    def test_finish_reports_itself_through_metadata(self) -> None:
        registry = self.make_registry()
        result = registry._tools["finish"].handler(message="All done, run demo.py.")
        self.assertTrue(result.metadata["finished"])
        self.assertEqual(result.metadata["message"], "All done, run demo.py.")
        self.assertTrue(registry.context.finished)
        self.assertEqual(registry.context.finish_message, "All done, run demo.py.")


if __name__ == "__main__":
    unittest.main()

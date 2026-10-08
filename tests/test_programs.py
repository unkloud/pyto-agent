"""Persistent program-library records and explicit direct-run workflow."""

from __future__ import annotations

import os
import json
import unittest

from harness import programs
from harness.session import SessionLog, assistant_message_event, user_message_event
from harness.tools_ios import Workspace, build_registry, default_context

from .support import TempDirTestCase


class TestProgramLibrary(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Workspace(self.workspace_dir)

    def write_program(self, relative: str, source: str = "print('saved run')\n") -> None:
        path = self.workspace.resolve(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)

    def register(self, **overrides):
        values = {
            "title": "Daily note",
            "purpose": "Summarize today's notes.",
            "entry_file": "notes/daily note.py",
            "mode": "batch",
            "required_capabilities": ["pasteboard", "network"],
        }
        values.update(overrides)
        return programs.register(self.workspace, **values)

    def test_records_survive_workspace_reopen_and_keep_unicode_and_spaces(self) -> None:
        self.write_program("notes/daily note.py")
        record = self.register(title="Café note 📝")

        reopened = Workspace(self.workspace_dir)
        loaded = programs.find_program(reopened, record["id"])
        self.assertEqual(loaded["title"], "Café note 📝")
        self.assertEqual(loaded["entry_file"], "notes/daily note.py")
        self.assertEqual(loaded["mode"], "batch")
        self.assertTrue(loaded["entry_exists"])
        self.assertEqual(loaded["last_verification_result"]["status"], "not_run")

    def test_capability_dependency_ids_persist_and_existing_labels_keep_their_meaning(self) -> None:
        self.write_program("needs_clipboard.py")
        record = self.register(
            entry_file="needs_clipboard.py",
            required_capabilities=["pasteboard", "calendar"],
            capability_dependencies=["clipboard.read"],
        )
        loaded = programs.find_program(self.workspace, record["id"])
        self.assertEqual(loaded["required_capabilities"], ["pasteboard", "calendar"])
        self.assertEqual(loaded["capability_dependencies"], ["clipboard.read"])
        updated = self.register(
            title=record["title"],
            entry_file="needs_clipboard.py",
            program_id=record["id"],
        )
        self.assertEqual(updated["capability_dependencies"], ["clipboard.read"])
        with self.assertRaises(programs.ProgramLibraryError):
            self.register(
                title="Unknown capability",
                entry_file="needs_clipboard.py",
                capability_dependencies=["clipboard.read.everything"],
            )

    def test_schema_three_persists_input_contract_and_versioned_project_brief(self) -> None:
        self.write_program("input_demo.py", "def main(inputs):\n    print(inputs['count'])\n")
        record = self.register(
            entry_file="input_demo.py",
            input_schema=[
                {"name": "count", "label": "Count", "type": "number", "integer": True, "minimum": 1},
                {"name": "mode", "label": "Mode", "type": "choice", "choices": ["brief", "full"], "default": "brief"},
            ],
        )
        self.assertNotIn("default", record["input_schema"][0])
        self.assertEqual(record["input_schema"][1]["default"], "brief")
        index_path = programs.metadata_path(self.workspace)
        with open(index_path, "r", encoding="utf-8") as handle:
            saved_index = json.load(handle)
        self.assertEqual(saved_index["schema_version"], 3)
        self.assertNotIn("submitted_values", saved_index["programs"][0])
        self.assertEqual(record["project_brief"]["schema_version"], 1)
        result = programs.execute_saved(
            build_registry(default_context(self.workspace_dir, self.path("spill"))),
            programs.find_program(self.workspace, record["id"]),
            input_values={"count": "7", "mode": "full"},
        )
        self.assertFalse(result.is_error, result.content)
        self.assertIn("7", result.content)
        with open(index_path, "r", encoding="utf-8") as handle:
            self.assertNotIn('"count": 7', handle.read())

    def test_legacy_schema_one_index_migrates_on_the_next_write(self) -> None:
        self.write_program("legacy.py")
        path = self.workspace.resolve(programs.METADATA_FILE)
        legacy = {
            "schema_version": 1,
            "programs": [
                {
                    "id": "legacy123",
                    "title": "Legacy",
                    "purpose": "Old record",
                    "entry_file": "legacy.py",
                    "mode": "batch",
                    "required_capabilities": [],
                    "last_verification_result": {"status": "not_run", "summary": "Not run yet.", "checked_at": None},
                    "registered_at": 1,
                    "updated_at": 1,
                }
            ],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle)
        found = programs.find_program(self.workspace, "legacy123")
        self.assertEqual(found["input_schema"], [])
        programs.register(
            self.workspace,
            title="Legacy",
            purpose="Updated old record",
            entry_file="legacy.py",
            mode="batch",
            program_id="legacy123",
        )
        with open(path, "r", encoding="utf-8") as handle:
            upgraded = json.load(handle)
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertEqual(upgraded["programs"][0]["input_schema"], [])
        self.assertEqual(upgraded["programs"][0]["project_brief"]["schema_version"], 1)

    def test_schema_two_index_migrates_without_losing_input_schema(self) -> None:
        self.write_program("schema_two.py")
        record = self.register(
            entry_file="schema_two.py",
            input_schema=[{"name": "kind", "label": "Kind", "type": "choice", "choices": ["a", "b"]}],
        )
        path = programs.metadata_path(self.workspace)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["schema_version"] = 2
        payload["programs"][0].pop("project_brief")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        loaded = programs.find_program(self.workspace, record["id"])
        self.assertEqual(loaded["input_schema"][0]["name"], "kind")
        self.assertEqual(loaded["project_brief"]["requirements"], [])
        programs.update_verification(self.workspace, record["id"], status="passed", summary="It ran.")
        with open(path, "r", encoding="utf-8") as handle:
            upgraded = json.load(handle)
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertEqual(upgraded["programs"][0]["input_schema"][0]["name"], "kind")

    def test_project_briefs_are_isolated_and_show_live_files_and_verification(self) -> None:
        self.write_program("first.py")
        self.write_program("second.py")
        self.write_program("helpers/shared.py")
        first = self.register(title="First project", entry_file="first.py")
        second = self.register(title="Second project", entry_file="second.py")
        programs.update_project_brief(
            self.workspace,
            first["id"],
            requirements=["Keep the notebook local and searchable."],
            decisions=["Use Markdown files."],
            related_files=["helpers/shared.py"],
            unresolved=["Decide how to export notes."],
        )
        programs.update_project_brief(
            self.workspace,
            second["id"],
            requirements=["Count only completed workouts."],
            decisions=["Use a weekly summary."],
            unresolved=[],
        )
        programs.update_verification(self.workspace, first["id"], status="passed", summary="The notebook ran.")

        first_view = programs.project_brief_view(self.workspace, first["id"])
        second_view = programs.project_brief_view(self.workspace, second["id"])
        self.assertEqual(first_view["project_brief"]["requirements"], ["Keep the notebook local and searchable."])
        self.assertEqual(second_view["project_brief"]["requirements"], ["Count only completed workouts."])
        self.assertEqual(first_view["files"], [{"path": "first.py", "status": "present"}, {"path": "helpers/shared.py", "status": "present"}])
        self.assertEqual(first_view["program"]["last_verification_result"]["status"], "passed")

        os.unlink(self.workspace.resolve("helpers/shared.py"))
        stale = programs.project_brief_view(self.workspace, first["id"])
        self.assertEqual(stale["files"][1]["status"], "missing or outside workspace")

    def test_project_brief_rejects_oversized_or_escaping_updates_without_mutation(self) -> None:
        self.write_program("brief.py")
        record = self.register(entry_file="brief.py")
        index_path = programs.metadata_path(self.workspace)
        with open(index_path, "rb") as handle:
            original = handle.read()
        with self.assertRaisesRegex(programs.ProgramLibraryError, "characters or fewer"):
            programs.update_project_brief(self.workspace, record["id"], requirements=["x" * 501])
        with self.assertRaisesRegex(programs.ProgramLibraryError, "stay inside the workspace"):
            programs.update_project_brief(self.workspace, record["id"], related_files=["../outside.py"])
        with open(index_path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_duplicate_title_is_case_insensitive_and_does_not_overwrite_index(self) -> None:
        self.write_program("one.py")
        self.write_program("two.py")
        first = self.register(entry_file="one.py")
        with open(programs.metadata_path(self.workspace), "rb") as handle:
            original = handle.read()
        with self.assertRaisesRegex(programs.ProgramLibraryError, "already uses the title"):
            self.register(title="DAILY NOTE", entry_file="two.py")
        with open(programs.metadata_path(self.workspace), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(programs.find_program(self.workspace, first["id"])["entry_file"], "one.py")

    def test_update_by_id_can_recover_a_renamed_entry_and_preserves_verification(self) -> None:
        self.write_program("old.py")
        record = self.register(entry_file="old.py")
        programs.update_verification(self.workspace, record["id"], status="failed", summary="A test failed.")
        os.unlink(self.workspace.resolve("old.py"))
        self.assertFalse(programs.find_program(self.workspace, record["id"])["entry_exists"])

        self.write_program("renamed.py")
        updated = self.register(
            title=record["title"],
            purpose="Updated purpose.",
            entry_file="renamed.py",
            mode="batch",
            required_capabilities=["files"],
            program_id=record["id"],
        )
        self.assertEqual(updated["entry_file"], "renamed.py")
        self.assertEqual(updated["last_verification_result"]["status"], "failed")
        self.assertTrue(programs.find_program(self.workspace, record["id"])["entry_exists"])

    def test_entry_files_must_exist_be_python_and_stay_in_workspace(self) -> None:
        self.write_program("ok.py")
        with self.assertRaisesRegex(programs.ProgramLibraryError, "workspace-relative"):
            self.register(entry_file="../outside.py")
        with self.assertRaisesRegex(programs.ProgramLibraryError, "no such file"):
            self.register(entry_file="missing.py")
        with open(self.workspace.resolve("notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("text")
        with self.assertRaisesRegex(programs.ProgramLibraryError, "existing workspace Python file"):
            self.register(entry_file="notes.txt")

    def test_malformed_or_wrong_version_index_is_preserved_and_reported(self) -> None:
        path = self.workspace.resolve(programs.METADATA_FILE)
        payload = b'{"schema_version": 99, "programs": []}\n'
        with open(path, "wb") as handle:
            handle.write(payload)
        with self.assertRaisesRegex(programs.ProgramLibraryError, "unsupported or malformed schema"):
            programs.list_programs(self.workspace)
        with self.assertRaises(programs.ProgramLibraryError):
            self.register()
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_malformed_json_is_not_replaced_during_listing(self) -> None:
        path = self.workspace.resolve(programs.METADATA_FILE)
        payload = b"not json\n"
        with open(path, "wb") as handle:
            handle.write(payload)
        with self.assertRaisesRegex(programs.ProgramLibraryError, "left untouched"):
            programs.render_listing(self.workspace)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_failed_verification_is_persisted_and_shown(self) -> None:
        self.write_program("check.py")
        record = self.register(entry_file="check.py")
        programs.update_verification(self.workspace, record["id"], status="failed", summary="The run returned an error.")
        listing = programs.render_listing(self.workspace)
        self.assertIn("last check: failed", listing)
        self.assertIn("The run returned an error.", programs.find_program(self.workspace, record["id"])["last_verification_result"]["summary"])

    def test_edit_prompt_carries_selected_program_context_and_change(self) -> None:
        self.write_program("notes/daily note.py")
        record = self.register()
        programs.update_project_brief(
            self.workspace,
            record["id"],
            requirements=["Keep exports local; do not upload notes."],
            decisions=["Use ISO dates."],
            unresolved=["Confirm archive encryption."],
        )
        record = programs.find_program(self.workspace, record["id"])
        prompt = programs.build_edit_prompt(record, "Add a date heading.", workspace=self.workspace)
        self.assertIn(record["id"], prompt)
        self.assertIn("notes/daily note.py", prompt)
        self.assertIn("Summarize today's notes.", prompt)
        self.assertIn("Keep exports local; do not upload notes.", prompt)
        self.assertIn("Confirm archive encryption.", prompt)
        self.assertIn('"status": "present"', prompt)
        self.assertIn("not policy", prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("Add a date heading.", prompt)

    def test_project_requirement_survives_compaction_and_a_new_edit_session(self) -> None:
        self.write_program("tracker.py", "print('tracker')\n")
        record = self.register(title="Workout tracker", purpose="Track completed workouts.", entry_file="tracker.py")
        programs.update_project_brief(
            self.workspace,
            record["id"],
            requirements=["Count only completed workouts, not planned sessions."],
        )

        path = self.path("before-compaction.jsonl")
        session = SessionLog.create(path, workspace=self.workspace_dir)
        for index in range(4):
            session.append("message.user", user_message_event("Old chat turn {}".format(index)))
            session.append("message.assistant", assistant_message_event({"role": "assistant", "content": "Old response."}))
        session.append("message.user", user_message_event("Continue the workout tracker in this chat."))
        compacted = session.compact(keep_recent=2, force=True)
        self.assertIsNotNone(compacted)
        session.close()

        restarted = SessionLog.resume(path)
        self.assertEqual(restarted.project(), [{"role": "user", "content": "Continue the workout tracker in this chat."}])
        restarted.close()
        persisted = programs.find_program(Workspace(self.workspace_dir), record["id"])
        edit_prompt = programs.build_edit_prompt(persisted, "Add a monthly total.", workspace=Workspace(self.workspace_dir))
        self.assertIn("Count only completed workouts, not planned sessions.", edit_prompt)
        self.assertIn("Add a monthly total.", edit_prompt)

    def test_command_parser_keeps_builtin_commands_separate_from_user_prompts(self) -> None:
        self.assertEqual(programs.parse_command("/programs"), {"action": "list"})
        self.assertEqual(programs.parse_command("/run abc123"), {"action": "run", "identifier": "abc123"})
        self.assertEqual(
            programs.parse_command("/edit abc123 Add a title"),
            {"action": "edit", "identifier": "abc123", "request": "Add a title"},
        )
        self.assertIsNone(programs.parse_command("/runner should be explained"))
        self.assertIsNone(programs.parse_command("please edit the program"))

    def test_explicit_saved_run_uses_registered_runner_without_model_client(self) -> None:
        self.write_program("batch.py", "print('ran without a model')\n")
        record = self.register(entry_file="batch.py")
        context = default_context(self.workspace_dir, self.path("spill"))
        registry = build_registry(context)
        result = programs.execute_saved(registry, record)
        self.assertFalse(result.is_error, result.content)
        self.assertIn("ran without a model", result.content)
        self.assertEqual(programs.find_program(self.workspace, record["id"])["last_verification_result"]["status"], "passed")

    def test_failed_saved_run_updates_the_record(self) -> None:
        self.write_program("broken.py", "raise ValueError('expected failure')\n")
        record = self.register(entry_file="broken.py")
        context = default_context(self.workspace_dir, self.path("spill"))
        result = programs.execute_saved(build_registry(context), record)
        self.assertTrue(result.is_error)
        stored = programs.find_program(self.workspace, record["id"])
        self.assertEqual(stored["last_verification_result"]["status"], "failed")

    def test_missing_entry_run_fails_with_recovery_action(self) -> None:
        self.write_program("gone.py")
        record = self.register(entry_file="gone.py")
        os.unlink(self.workspace.resolve("gone.py"))
        context = default_context(self.workspace_dir, self.path("spill"))
        registry = build_registry(context)
        with self.assertRaisesRegex(programs.ProgramLibraryError, "entry file.*is missing"):
            programs.execute_saved(registry, programs.find_program(self.workspace, record["id"]))


if __name__ == "__main__":
    unittest.main()

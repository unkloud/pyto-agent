"""Typed input contracts for saved programs and their terminal collector."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import program_inputs


class TestProgramInputSchema(unittest.TestCase):
    def test_normalizes_supported_field_types_and_defaults(self) -> None:
        schema = program_inputs.normalize_schema(
            [
                {"name": "note", "label": "Note", "type": "text", "required": False},
                {"name": "limit", "label": "Maximum files", "type": "number", "integer": True, "minimum": 1, "maximum": 50},
                {"name": "group_by", "label": "Group by", "type": "choice", "choices": ["extension", "first letter"], "default": "extension"},
                {"name": "source", "label": "Source folder", "type": "folder"},
                {"name": "index", "label": "Index file", "type": "file", "extensions": [".JSON"]},
            ]
        )
        self.assertEqual(schema[0]["required"], False)
        self.assertNotIn("default", schema[1])
        self.assertEqual(schema[1]["minimum"], 1)
        self.assertEqual(schema[2]["choices"], ["extension", "first letter"])
        self.assertEqual(schema[4]["extensions"], [".json"])

    def test_rejects_duplicate_names_unknown_keys_and_invalid_choices(self) -> None:
        base = {"name": "format", "label": "Format", "type": "choice", "choices": ["csv", "json"]}
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "duplicate"):
            program_inputs.normalize_schema([base, dict(base)])
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "unsupported key"):
            program_inputs.normalize_schema([{**base, "callback": "run shell"}])
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "unique"):
            program_inputs.normalize_schema([{**base, "choices": ["csv", "CSV"]}])
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "2 to"):
            program_inputs.normalize_schema([{**base, "choices": ["csv"]}])

    def test_rejects_path_defaults_and_bad_numeric_bounds(self) -> None:
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "may only store a static choice default"):
            program_inputs.normalize_schema([{"name": "folder", "label": "Folder", "type": "folder", "default": "/tmp"}])
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "may only store a static choice default"):
            program_inputs.normalize_schema([{"name": "token", "label": "Token", "type": "text", "default": "private"}])
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "minimum cannot exceed"):
            program_inputs.normalize_schema([{"name": "count", "label": "Count", "type": "number", "minimum": 5, "maximum": 2}])

    def test_values_are_typed_validated_and_not_saved_anywhere(self) -> None:
        schema = [
            {"name": "title", "label": "Title", "type": "text", "required": False},
            {"name": "limit", "label": "Limit", "type": "number", "integer": True, "minimum": 1, "maximum": 50},
            {"name": "mode", "label": "Mode", "type": "choice", "choices": ["preview", "apply"]},
        ]
        values = program_inputs.validate_values(schema, {"title": "Résumé", "limit": "3", "mode": "preview"})
        self.assertEqual(values, {"title": "Résumé", "limit": 3, "mode": "preview"})
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "whole number"):
            program_inputs.validate_values(schema, {"limit": "2.5", "mode": "preview"})
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "at most"):
            program_inputs.validate_values(schema, {"limit": "51", "mode": "preview"})
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "one of"):
            program_inputs.validate_values(schema, {"limit": "2", "mode": "other"})
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "unknown"):
            program_inputs.validate_values(schema, {"limit": "2", "mode": "preview", "secret": "x"})
        with self.assertRaisesRegex(program_inputs.ProgramInputError, "Title is required"):
            program_inputs.validate_values(
                [{"name": "title", "label": "Title", "type": "text"}], {"title": "   "}
            )
        self.assertEqual(program_inputs.validate_values(schema, {"limit": "2", "mode": "preview"})["title"], None)

    def test_file_folder_paths_check_access_type_extensions_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Café folder")
            os.mkdir(folder)
            document = os.path.join(folder, "résumé.json")
            with open(document, "w", encoding="utf-8") as handle:
                handle.write("{}")
            wrong_extension = os.path.join(folder, "résumé.txt")
            with open(wrong_extension, "w", encoding="utf-8") as handle:
                handle.write("text")
            schema = [
                {"name": "folder", "label": "Folder", "type": "folder"},
                {"name": "file", "label": "JSON file", "type": "file", "extensions": [".json"]},
            ]
            values = program_inputs.validate_values(schema, {"folder": folder, "file": document})
            self.assertEqual(values["folder"], os.path.realpath(folder))
            self.assertEqual(values["file"], os.path.realpath(document))
            with self.assertRaisesRegex(program_inputs.ProgramInputError, "accessible folder"):
                program_inputs.validate_values(schema, {"folder": document, "file": document})
            with self.assertRaisesRegex(program_inputs.ProgramInputError, "extensions"):
                program_inputs.validate_values(schema, {"folder": folder, "file": wrong_extension})
            with mock.patch("harness.program_inputs.os.access", return_value=False):
                with self.assertRaisesRegex(program_inputs.ProgramInputError, "cannot be read"):
                    program_inputs.validate_values(schema, {"folder": folder, "file": document})

    def test_terminal_collector_uses_picker_and_reports_cancel(self) -> None:
        class FileSystem:
            class FilePickerCancellation(Exception):
                pass

            @staticmethod
            def pick_directory():
                return folder

            @staticmethod
            def import_file(**_kwargs):
                raise FileSystem.FilePickerCancellation()

        with tempfile.TemporaryDirectory() as folder:
            schema = [
                {"name": "root", "label": "Folder", "type": "folder"},
                {"name": "file", "label": "File", "type": "file"},
            ]
            with self.assertRaises(program_inputs.InputsCancelled):
                program_inputs.collect_terminal_values(schema, file_system=FileSystem)

    def test_terminal_prompt_and_cli_values_parse_numbers_choices_and_path_picker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "資料 folder")
            os.mkdir(folder)
            schema = [
                {"name": "label", "label": "Label", "type": "text"},
                {"name": "limit", "label": "Limit", "type": "number", "integer": True},
                {"name": "mode", "label": "Mode", "type": "choice", "choices": ["preview", "apply"]},
                {"name": "folder", "label": "Folder", "type": "folder"},
            ]
            answers = iter(("Résumé", "2", "2"))
            values = program_inputs.collect_terminal_values(
                schema,
                input_fn=lambda _prompt: next(answers),
                file_system=SimpleNamespace(pick_directory=lambda: folder),
            )
            self.assertEqual(values, {"label": "Résumé", "limit": 2, "mode": "apply", "folder": os.path.realpath(folder)})

            cli = program_inputs.parse_cli_values(
                schema,
                ["label=one=two", "limit=5", "mode=preview", "folder=@pick"],
                file_system=SimpleNamespace(pick_directory=lambda: folder),
            )
            self.assertEqual(cli["label"], "one=two")
            self.assertEqual(cli["limit"], 5)
            self.assertEqual(cli["folder"], os.path.realpath(folder))
            with self.assertRaisesRegex(program_inputs.ProgramInputError, "cannot open Pyto's file/folder picker"):
                program_inputs.parse_cli_values(
                    schema,
                    ["label=Crème brûlée & tea", "limit=5", "mode=preview", "folder=@pick"],
                    file_system=SimpleNamespace(pick_directory=lambda: self.fail("picker must not open")),
                    allow_picker=False,
                )
            self.assertEqual(
                program_inputs.parse_cli_values(
                    schema[:1], ["label=Crème brûlée & tea; x=1"], allow_picker=False
                ),
                {"label": "Crème brûlée & tea; x=1"},
            )
            with self.assertRaisesRegex(program_inputs.ProgramInputError, "NAME=VALUE"):
                program_inputs.parse_cli_values(schema, ["broken"])

    def test_schema_summary_does_not_include_values(self) -> None:
        schema = [{"name": "style", "label": "Style", "type": "choice", "choices": ["draft", "final"], "default": "draft"}]
        summary = program_inputs.schema_summary(schema)
        self.assertIn("Style (choice)", summary)
        self.assertNotIn("draft", summary)


if __name__ == "__main__":
    unittest.main()

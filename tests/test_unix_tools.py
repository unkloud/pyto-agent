"""Tests for the bounded command bridge exposed to the Pyto agent."""

from __future__ import annotations

import os
import shutil
import unittest

from harness.errors import ToolError
from harness.tools_ios import Workspace
from harness.unix_tools import parse_help_output, run_command, validate_arguments

from .support import TempDirTestCase


class TestCommandHelpParsing(unittest.TestCase):
    def test_only_recognizes_command_table_rows(self) -> None:
        output = """Use grep to search files and head to view a prefix.
Available commands:
cat
cut
find
grep
help
"""
        self.assertEqual(parse_help_output(output), ["cat", "cut", "find", "grep"])

    def test_accepts_a_space_separated_command_list(self) -> None:
        self.assertEqual(parse_help_output("cat cut find grep"), ["cat", "cut", "find", "grep"])


class TestCommandArguments(unittest.TestCase):
    def test_find_does_not_accept_expression_or_exec_options(self) -> None:
        with self.assertRaises(ToolError):
            validate_arguments("find", ["-exec", "rm", "{}", ";"])

    def test_grep_requires_exactly_one_pattern(self) -> None:
        with self.assertRaises(ToolError):
            validate_arguments("grep", ["first", "second"])

    def test_commands_outside_the_allowlist_are_refused(self) -> None:
        with self.assertRaises(ToolError):
            validate_arguments("sh", ["-c", "echo unsafe"])


class TestCommandExecution(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Workspace(self.workspace_dir)
        with open(os.path.join(self.workspace_dir, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("first line\nneedle found\nlast line\n")

    def run_tool(self, command, args=(), *, path="", input_text=None):
        return run_command(
            command,
            args,
            path=path,
            input_text=input_text,
            workspace=self.workspace,
            available=[command],
        )

    def test_grep_uses_a_workspace_path_and_argv(self) -> None:
        if not shutil.which("grep"):
            self.skipTest("grep is not installed")
        outcome = self.run_tool("grep", ["-n", "needle"], path="notes.txt")
        self.assertEqual(outcome["returncode"], 0)
        self.assertIn("needle found", outcome["stdout"])
        self.assertEqual(outcome["mode"], "desktop subprocess")

    def test_grep_no_matches_is_a_normal_result(self) -> None:
        if not shutil.which("grep"):
            self.skipTest("grep is not installed")
        outcome = self.run_tool("grep", ["missing"], path="notes.txt")
        self.assertEqual(outcome["returncode"], 1)
        self.assertTrue(outcome["no_matches"])

    def test_path_escape_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            self.run_tool("cat", path="../outside.txt")

    def test_only_confirmed_commands_can_run(self) -> None:
        with self.assertRaises(ToolError):
            run_command(
                "grep", ["needle"], path="notes.txt", input_text=None,
                workspace=self.workspace, available=["cat"],
            )

    def test_file_size_is_bounded_before_execution(self) -> None:
        with open(os.path.join(self.workspace_dir, "large.txt"), "w", encoding="utf-8") as handle:
            handle.write("x" * (1024 * 1024 + 1))
        with self.assertRaises(ToolError):
            self.run_tool("cat", path="large.txt")

    def test_folder_listing_is_bounded(self) -> None:
        for index in range(501):
            with open(os.path.join(self.workspace_dir, "item-{}.txt".format(index)), "w", encoding="utf-8") as handle:
                handle.write("x")
        with self.assertRaises(ToolError):
            self.run_tool("find", ["-name", "*.txt"])

    def test_text_commands_accept_bounded_stdin(self) -> None:
        if not shutil.which("wc"):
            self.skipTest("wc is not installed")
        outcome = self.run_tool("wc", ["-l"], input_text="one\ntwo\n")
        self.assertEqual(outcome["returncode"], 0)
        self.assertIn("2", outcome["stdout"])

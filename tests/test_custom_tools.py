"""Tests for persistent, schema-described user tools."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest import mock

from harness import capability_inventory, custom_tools
from harness.errors import ToolError
from harness.tools import ToolRegistry, ToolResult
from harness.tools_ios import Workspace, build_registry, default_context

from .support import TempDirTestCase


class TestCustomTools(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Workspace(self.workspace_dir)
        self.registry = ToolRegistry()
        self.calls = []

        def run_program(path, args):
            self.calls.append((path, args))
            return ToolResult.ok("custom program ran")

        self.run_program = run_program

    @staticmethod
    def schema():
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        }

    @staticmethod
    def source():
        return "def run(inputs):\n    return {'length': len(inputs['text'])}\n"

    def create_and_register(self, capability_dependencies=None):
        manifest = custom_tools.create_custom_tool(
            self.workspace,
            name="summarize_text",
            purpose="Summarize a short text.",
            parameters=self.schema(),
            source=self.source(),
            required_modules=["re"],
            capability_dependencies=capability_dependencies,
        )
        custom_tools.register_custom_tool(self.workspace, self.registry, self.run_program, manifest)
        return manifest

    def test_creation_persists_schema_and_source_hash(self) -> None:
        manifest = self.create_and_register()
        self.assertIn("custom_summarize_text", self.registry)
        with open(os.path.join(self.workspace_dir, manifest["source"]), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("def run(inputs):", source)
        self.assertEqual(hashlib.sha256(source.encode("utf-8")).hexdigest(), manifest["source_sha256"])
        listed = custom_tools.list_custom_tools(self.workspace)
        self.assertEqual(listed["enabled"][0]["tool"], "custom_summarize_text")

    def test_custom_tool_persists_capability_dependency_ids(self) -> None:
        manifest = self.create_and_register(["clipboard.read"])
        self.assertEqual(manifest["capability_dependencies"], ["clipboard.read"])
        listed = custom_tools.list_custom_tools(self.workspace)
        self.assertEqual(listed["enabled"][0]["capability_dependencies"], ["clipboard.read"])
        self.assertNotIn("alternatives", manifest)

    def test_unavailable_point_does_not_block_tool_using_another_documented_point(self) -> None:
        runtime = {
            "device_model": "iPhone17,3",
            "ios_version": "26.1",
            "pyto_version": "19.1",
            "pyto_build": "500",
        }
        context = default_context(self.workspace_dir, self.path("spill"))
        context.state_dir = self.path("state")
        registry = build_registry(context)
        with mock.patch.object(capability_inventory.ios, "is_pyto", return_value=True):
            recorded = capability_inventory.record_unavailable(
                context.state_dir,
                "location.read",
                failed_step="import location",
                error_type="ImportError",
                detail="location module unavailable",
                test_id="minimal-location.py",
                runtime=runtime,
            )
        self.assertTrue(recorded)
        with mock.patch.object(capability_inventory, "runtime_fingerprint", return_value=runtime):
            with self.assertRaisesRegex(ToolError, "currently unavailable"):
                registry.get("custom_tool_create").handler(
                    name="blocked_location_reader",
                    purpose="Read the device location directly.",
                    parameters={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    source="def run(inputs):\n    return None\n",
                    capability_dependencies=["location.read"],
                )
            manifest = registry.get("custom_tool_create").handler(
                name="shortcut_location_reader",
                purpose="Read a location result supplied by a named Shortcut.",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "input_text": {"type": "string"},
                    },
                    "required": ["name", "input_text"],
                    "additionalProperties": False,
                },
                source=(
                    "from urllib.parse import quote\nimport xcallback\n\n"
                    "def run(inputs):\n"
                    "    url = 'shortcuts://x-callback-url/run-shortcut?name={}&input=text&text={}'.format("
                    "quote(inputs['name']), quote(inputs['input_text']))\n"
                    "    return xcallback.open_url(url)\n"
                ),
                capability_dependencies=["shortcut.run"],
            )
        self.assertFalse(manifest.is_error, manifest.content)
        listed = custom_tools.list_custom_tools(self.workspace)["enabled"][0]
        self.assertEqual(listed["capability_dependencies"], ["shortcut.run"])
        self.assertNotIn("alternatives", listed)

    def test_custom_tool_runs_through_the_program_runner_with_json_inputs(self) -> None:
        self.create_and_register()
        result = self.registry.get("custom_summarize_text").handler(text="hello")
        self.assertEqual(result.content, "custom program ran")
        self.assertEqual(self.calls[0][0], "custom-tools/summarize_text.py")
        self.assertEqual(json.loads(self.calls[0][1][0]), {"text": "hello"})

    def test_source_change_after_registration_is_refused(self) -> None:
        manifest = self.create_and_register()
        path = os.path.join(self.workspace_dir, manifest["source"])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("# changed\n")
        with self.assertRaises(ToolError):
            self.registry.get("custom_summarize_text").handler(text="hello")
        self.assertEqual(self.calls, [])

    def test_tool_can_be_disabled_and_reenabled(self) -> None:
        self.create_and_register()
        disabled = custom_tools.disable_custom_tool(self.workspace, "summarize_text", self.registry)
        self.assertIn("source was kept", disabled)
        self.assertNotIn("custom_summarize_text", self.registry)
        self.assertEqual(len(custom_tools.list_custom_tools(self.workspace)["disabled"]), 1)
        enabled = custom_tools.enable_custom_tool(self.workspace, "custom_summarize_text", self.registry, self.run_program)
        self.assertIn("approval each time", enabled)
        self.assertIn("custom_summarize_text", self.registry)

    def test_invalid_schema_and_source_are_rejected_before_persistence(self) -> None:
        with self.assertRaises(ToolError):
            custom_tools.create_custom_tool(
                self.workspace,
                name="bad_schema",
                purpose="Bad schema.",
                parameters={"type": "object", "properties": {"n": {"type": "integer", "minimum": "many"}}},
                source=self.source(),
            )
        with self.assertRaises(ToolError):
            custom_tools.create_custom_tool(
                self.workspace,
                name="bad_source",
                purpose="Bad source.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                source="def run():\n    return None\n",
            )
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "custom-tools")))

    def test_workspace_file_tools_cannot_edit_the_managed_store(self) -> None:
        registry = build_registry(default_context(self.workspace_dir))
        with self.assertRaises(ToolError):
            registry.get("write_file").handler(path="custom-tools/unsafe.json", content="{}")
        with self.assertRaises(ToolError):
            registry.get("write_program").handler(path="custom-tools/unsafe.py", source="print(1)")

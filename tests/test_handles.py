"""Persistent typed-handle behavior across store/session restarts."""

from __future__ import annotations

import hashlib
import json
import os

from harness.handles import LocalHandleStore
from harness.tools_ios import Workspace

from .support import TempDirTestCase


class TestPersistentHandles(TempDirTestCase):
    def test_handle_ids_and_payloads_survive_reopening_store(self) -> None:
        workspace = Workspace(self.workspace_dir)
        source = workspace.resolve("source.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("one\ntwo\n")

        first = LocalHandleStore(workspace, session_id="first")
        ingested = first.ingest_file("source.txt")
        self.assertEqual(ingested.state, "ok")
        self.assertIsNotNone(ingested.handle)
        source_id = ingested.handle.handle_id
        first.close()
        self.assertEqual(first.metadata(source_id), None)

        second = LocalHandleStore(Workspace(self.workspace_dir), session_id="second")
        metadata = second.metadata(source_id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.handle_id, source_id)
        self.assertEqual(metadata.scope, "workspace")
        self.assertEqual(metadata.lifetime, "until-explicit-delete")
        transformed = second.transform_text(source_id, lambda text: text.upper())
        self.assertEqual(transformed.state, "ok")
        self.assertIsNotNone(transformed.handle)
        transformed_id = transformed.handle.handle_id
        second.close()

        third = LocalHandleStore(Workspace(self.workspace_dir), session_id="third")
        loaded = third.metadata(transformed_id)
        self.assertIsNotNone(loaded)
        written = third.write_file(transformed_id, "result.txt")
        self.assertEqual(written.state, "ok")
        with open(workspace.resolve("result.txt", must_exist=True), "rb") as handle:
            self.assertEqual(handle.read(), b"ONE\nTWO\n")

        manifest_path = os.path.join(self.workspace_dir, ".pyto-handles", "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["version"], 1)
        self.assertNotIn("state", manifest["handles"][0])
        self.assertIn("shape", manifest["handles"][0])
        self.assertIn("preview_policy", manifest["handles"][0])
        self.assertIn("size_bytes", manifest["handles"][0])
        self.assertIn("artifact_file", manifest["handles"][0])

        deleted = third.delete(transformed_id)
        self.assertEqual(deleted.state, "ok")
        third.close()
        fourth = LocalHandleStore(Workspace(self.workspace_dir))
        self.assertIsNone(fourth.metadata(transformed_id))

    def test_operation_result_state_is_separate_from_handle_metadata(self) -> None:
        workspace = Workspace(self.workspace_dir)
        with open(workspace.resolve("data.txt"), "w", encoding="utf-8") as handle:
            handle.write("private text")
        result = LocalHandleStore(workspace).ingest_file("data.txt")
        self.assertEqual(result.to_dict()["state"], "ok")
        self.assertNotIn("state", result.handle.to_dict())

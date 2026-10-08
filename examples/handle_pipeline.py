"""Demonstrate a local file -> transform -> file pipeline using opaque handles.

Run from the repository root with ``python3 examples/handle_pipeline.py``. The example
prints handle metadata and operation states; it never prints file contents or contacts an
LLM. The same stdlib-only module is compatible with Pyto's Python 3.10 runtime.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import hashlib

# Make the repository package importable when this example is launched by path from the
# repository root (Python otherwise puts only the examples/ directory on sys.path).
REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)

from harness.handles import LocalHandleStore
from harness.tools_ios import Workspace


def summarize(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "{} non-empty lines\n".format(len(lines)) + "\n".join("- " + line for line in lines)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pyto-handle-demo-") as root:
        workspace = Workspace(root)
        source_path = workspace.resolve("input.txt")
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write("Buy oat milk\nBook dentist\nBuy oat milk\n")

        store = LocalHandleStore(workspace, session_id="example")
        source = store.ingest_file("input.txt")
        if source.state != "ok" or source.handle is None:
            raise RuntimeError("input ingest failed: {}".format(source.error))
        source_id = source.handle.handle_id
        store.close()

        # Reopening simulates a later harness session: the stable ID resolves from the
        # workspace manifest and the payload remains private in the handle directory.
        store = LocalHandleStore(workspace, session_id="next-session")
        source_metadata = store.metadata(source_id)
        if source_metadata is None:
            raise RuntimeError("source handle did not survive store restart")
        transformed = store.transform_text(source_id, summarize)
        if transformed.state != "ok" or transformed.handle is None:
            raise RuntimeError("local transform failed: {}".format(transformed.error))
        transformed_id = transformed.handle.handle_id
        store.close()

        store = LocalHandleStore(workspace, session_id="final-session")
        transformed_metadata = store.metadata(transformed_id)
        if transformed_metadata is None:
            raise RuntimeError("transformed handle did not survive store restart")
        written = store.write_file(transformed_id, "summary.txt")
        if written.state != "ok":
            raise RuntimeError("output write failed: {}".format(written.error))
        output_path = workspace.resolve("summary.txt", must_exist=True)
        with open(output_path, "rb") as output_file:
            output_digest = hashlib.sha256(output_file.read()).hexdigest()
        expected_digest = hashlib.sha256(summarize("Buy oat milk\nBook dentist\nBuy oat milk\n").encode("utf-8")).hexdigest()

        print(json.dumps({
            "source_handle": source_metadata.to_dict(),
            "transform_handle": transformed_metadata.to_dict(),
            "write_operation": written.to_dict(),
            "output_exists": os.path.isfile(output_path),
            "output_matches_local_transform": output_digest == expected_digest,
            "handles_survived_store_restarts": True,
            "contents_printed": False,
            "llm_calls": 0,
        }, ensure_ascii=False, sort_keys=True))
        store.close()


if __name__ == "__main__":
    main()

"""Provider request context is bounded without corrupting current intent or tool calls."""

from __future__ import annotations

import copy
import unittest

from harness.request_context import (
    RequestContextError,
    bound_provider_request,
    estimate_request_bytes,
)


class TestBoundProviderRequest(unittest.TestCase):
    def test_drops_whole_old_turns_and_keeps_the_latest_request(self) -> None:
        messages = [
            {"role": "system", "content": "Follow the system rules."},
            {"role": "user", "content": "old requirement " + "x" * 5000},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "Keep this new requirement exactly."},
        ]
        original = copy.deepcopy(messages)
        bounded = bound_provider_request(messages, max_bytes=1200)

        self.assertEqual(bounded.omitted_turns, 1)
        self.assertLessEqual(bounded.payload_bytes, 1200)
        self.assertEqual(bounded.messages[-1], messages[-1])
        self.assertIn("Earlier complete chat turns are omitted", bounded.messages[0]["content"])
        self.assertEqual(messages, original, "building a request must not mutate the durable projection")

    def test_trims_large_tool_result_content_without_changing_tool_pair(self) -> None:
        tool_call = {
            "id": "call_read",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'},
        }
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Summarize this file for my current project."},
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call_read", "name": "read_file", "content": "start\n" + "result " * 3000 + "\nend"},
        ]
        original_call = copy.deepcopy(tool_call)
        bounded = bound_provider_request(messages, max_bytes=1800)

        self.assertEqual(bounded.trimmed_messages, 1)
        self.assertLessEqual(bounded.payload_bytes, 1800)
        self.assertEqual(bounded.messages[1]["content"], messages[1]["content"])
        self.assertEqual(bounded.messages[2]["tool_calls"], [original_call])
        self.assertEqual(bounded.messages[3]["tool_call_id"], "call_read")
        self.assertIn("older content shortened", bounded.messages[3]["content"])
        self.assertIn("end", bounded.messages[3]["content"])

    def test_provider_byte_estimate_includes_tool_schemas(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "tool", "description": "schema " * 20}}]
        self.assertGreater(estimate_request_bytes(messages, tools), estimate_request_bytes(messages, []))

    def test_refuses_to_truncate_the_current_user_request(self) -> None:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do not drop " * 100}]
        with self.assertRaisesRegex(RequestContextError, "current user request"):
            bound_provider_request(messages, max_bytes=100)

    def test_refuses_an_orphaned_tool_result(self) -> None:
        messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call1"}]},
        ]
        with self.assertRaisesRegex(RequestContextError, "without its result"):
            bound_provider_request(messages, max_bytes=5000)


if __name__ == "__main__":
    unittest.main()

"""Agent-loop tests: turn structure, approval, truncation, resume, and tool round trips.

These drive :func:`harness.loop.run_turn` against the mock provider, so they are the
closest thing here to a real conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from harness.errors import MalformedResponseError
from harness.loop import (
    APPROVAL_REQUIRED_TOOLS,
    AUTO_APPROVED_TOOLS,
    ApprovalRequest,
    Event,
    auto_prompter,
    build_system_prompt,
    make_policy,
    run_turn,
)
from harness.session import SessionLog

from .mock_provider import MockProvider, error_response, text_response, tool_response
from .support import TempDirTestCase, make_client, make_options


def drive(options, prompt: str):
    """Run a turn to completion, returning (events, done_payload)."""
    async def go():
        collected = []
        done = {}
        async for event in run_turn(options, prompt):
            collected.append(event)
            if event.kind == "done":
                done = event.data
        return collected, done

    return asyncio.run(go())


def kinds(events):
    return [event.kind for event in events]


class TestSystemPrompt(TempDirTestCase):
    def test_prompt_describes_the_ios_environment(self) -> None:
        config = self.make_config()
        prompt = build_system_prompt(config, self.workspace_dir)
        for needle in ("Pyto", "iPhone", "suspends", "standard library", "no PTY", "finish"):
            self.assertIn(needle, prompt)

    def test_prompt_names_the_workspace(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir)
        self.assertIn(self.workspace_dir, prompt)

    def test_prompt_lists_missing_capabilities(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir)
        self.assertIn("NOT available", prompt)

    def test_extra_text_is_appended(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir, extra="Remember: be brief.")
        self.assertIn("be brief", prompt)


class TestApprovalPolicy(unittest.TestCase):
    def test_workspace_and_read_tools_are_auto_approved(self) -> None:
        # `interactive=True` is what the CLI passes when a human can answer (a TTY, or
        # --ui); run_program stays AUTO there.  With nothing attached it is denied and
        # needs the explicit opt-in — see TestUnattendedPrograms in test_hardening.py.
        policy = make_policy(interactive=True)
        for name in ("write_program", "run_program", "read_file", "list_files", "memory_read", "finish"):
            self.assertTrue(policy(name, {}).allowed, name)

    def test_sharing_tools_need_approval(self) -> None:
        denials = [make_policy()(name, {}).allowed for name in ("share_text", "open_url", "shortcut_run")]
        self.assertEqual(denials, [False, False, False])

    def test_yolo_allows_everything(self) -> None:
        policy = make_policy(yolo=True)
        for name in APPROVAL_REQUIRED_TOOLS:
            self.assertTrue(policy(name, {}).allowed, name)

    def test_deny_list_beats_yolo(self) -> None:
        policy = make_policy(yolo=True, deny=["share_text"])
        self.assertFalse(policy("share_text", {}).allowed)

    def test_prompter_decides(self) -> None:
        allowed = make_policy(prompter=auto_prompter(True))
        denied = make_policy(prompter=auto_prompter(False))
        self.assertTrue(allowed("share_text", {}).allowed)
        self.assertFalse(denied("share_text", {}).allowed)
        self.assertIn("declined", denied("share_text", {}).reason)

    def test_prompter_receives_a_described_request(self) -> None:
        seen = []

        def prompter(request: ApprovalRequest) -> bool:
            seen.append(request)
            return True

        make_policy(prompter=prompter)("shortcut_run", {"name": "Pay Rent"})
        self.assertEqual(seen[0].tool, "shortcut_run")
        self.assertIn("Pay Rent", seen[0].describe())
        self.assertIn("Shortcuts", seen[0].reason)

    def test_custom_tool_approval_shows_the_saved_python_source(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            os.makedirs(os.path.join(workspace, "custom-tools"), exist_ok=True)
            source = "def run(inputs):\n    return 'review this code'\n"
            with open(os.path.join(workspace, "custom-tools", "example.py"), "w", encoding="utf-8") as handle:
                handle.write(source)
            request = ApprovalRequest(
                tool="custom_example", arguments={"text": "hello"},
                reason="runs saved Python", workspace=workspace,
            )
            self.assertIn(source.rstrip(), request.describe())
            enable_request = ApprovalRequest(
                tool="custom_tool_enable", arguments={"name": "example"},
                reason="re-enables Python", workspace=workspace,
            )
            self.assertIn(source.rstrip(), enable_request.describe())

    def test_custom_tool_creation_approval_shows_full_source(self) -> None:
        source = "#" + (" review" * 400) + "\ndef run(inputs):\n    return None\n"
        description = ApprovalRequest(
            tool="custom_tool_create", arguments={"name": "long", "source": source},
            reason="stores Python",
        ).describe()
        self.assertIn(source.rstrip(), description)
        self.assertNotIn("more characters", description)

    def test_unknown_tool_is_denied(self) -> None:
        self.assertFalse(make_policy()("mystery_tool", {}).allowed)

    def test_saved_custom_tools_need_approval_for_each_invocation(self) -> None:
        self.assertFalse(make_policy()("custom_saved", {}).allowed)
        seen = []

        def prompter(request: ApprovalRequest) -> bool:
            seen.append(request)
            return True

        self.assertTrue(make_policy(prompter=prompter)("custom_saved", {"text": "hello"}).allowed)
        self.assertIn("saved Python", seen[0].reason)

    def test_calendar_write_needs_approval_but_read_does_not(self) -> None:
        policy = make_policy()
        self.assertFalse(policy("calendar_add_event", {}).allowed)
        self.assertTrue(policy("calendar_list_events", {}).allowed)

    def test_every_registered_tool_is_classified(self) -> None:
        registry = self.make_registry() if hasattr(self, "make_registry") else None
        from harness.tools_ios import build_registry, default_context
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            names = set(build_registry(default_context(tmp)).names())
        classified = AUTO_APPROVED_TOOLS | APPROVAL_REQUIRED_TOOLS
        self.assertEqual(names - classified, set(), "every tool needs a policy classification")


class TestSimpleTurn(TempDirTestCase):
    def test_text_answer_ends_the_turn(self) -> None:
        with MockProvider([text_response("Here is your answer.")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session, system_prompt="sys")
            events, done = drive(options, "hello")
            client.close()
        self.assertEqual(done["stop"], "stop")
        self.assertTrue(done["finished"])
        self.assertEqual(done["turns"], 1)
        self.assertIn("delta", kinds(events))
        self.assertIn("message.completed", kinds(events))

    def test_assistant_message_is_logged_and_projected(self) -> None:
        with MockProvider([text_response("logged")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            drive(options, "hello")
            client.close()
            messages = session.project()
            session.close()
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["content"], "logged")

    def test_system_prompt_is_sent_first(self) -> None:
        with MockProvider([text_response("ok")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session, system_prompt="MY SYSTEM PROMPT")
            drive(options, "hello")
            client.close()
            sent = provider.messages_sent(0)
        self.assertEqual(sent[0], {"role": "system", "content": "MY SYSTEM PROMPT"})
        self.assertEqual(sent[1]["content"], "hello")

    def test_tools_are_offered(self) -> None:
        with MockProvider([text_response("ok")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            drive(options, "hello")
            client.close()
            names = provider.tool_names_sent(0)
        self.assertIn("write_program", names)
        self.assertIn("finish", names)

    def test_provider_error_is_reported_as_an_event(self) -> None:
        with MockProvider([error_response(400, "no such model")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            events, done = drive(options, "hello")
            client.close()
        self.assertEqual(done["stop"], "error")
        self.assertTrue(any(event.kind == "error" for event in events))
        self.assertTrue(done["errors"])


class TestToolRoundTrip(TempDirTestCase):
    def test_write_then_run_in_one_conversation(self) -> None:
        source = "print('automation ran')\n"
        script = [
            tool_response(("write_program", {"path": "demo.py", "source": source, "purpose": "demo"})),
            tool_response(("run_program", {"path_or_source": "demo.py"}), ("finish", {"message": "Done: demo.py"})),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            options = make_options(client, registry, session)
            events, done = drive(options, "write and run a demo")
            client.close()
            sent_second = provider.messages_sent(1)
            session.close()

        self.assertTrue(os.path.exists(os.path.join(self.workspace_dir, "demo.py")))
        self.assertEqual(done["stop"], "finish_tool")
        self.assertEqual(done["message"], "Done: demo.py")
        roles = [m["role"] for m in sent_second]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(sent_second[3]["name"], "write_program")

    def test_new_custom_tool_is_offered_in_the_next_turn(self) -> None:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
        source = "def run(inputs):\n    print('custom:', inputs['text'])\n"
        script = [
            tool_response(("custom_tool_create", {
                "name": "echo_text",
                "purpose": "Print supplied text.",
                "parameters": schema,
                "source": source,
                "required_modules": ["sys"],
            })),
            tool_response(("custom_echo_text", {"text": "hello"})),
            text_response("The saved tool ran."),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            events, done = drive(options, "create and run an echo tool")
            client.close()
        self.assertEqual(done["stop"], "stop")
        self.assertIn("custom_echo_text", provider.tool_names_sent(1))
        tool_results = [event.data.get("content", "") for event in events if event.kind == "tool.completed"]
        self.assertTrue(any("custom: hello" in content for content in tool_results))

    def test_tool_results_are_logged_in_model_order(self) -> None:
        script = [
            tool_response(("list_files", {"pattern": "*.py"}), ("memory_read", {})),
            text_response("all done"),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            drive(options, "list things")
            client.close()
            tool_events = [e for e in session.events if e.type == "message.tool"]
            session.close()
        self.assertEqual([e.data["message"]["name"] for e in tool_events], ["list_files", "memory_read"])

    def test_finish_metadata_wins_over_text(self) -> None:
        with MockProvider([tool_response(("finish", {"message": "Summary for the user."}))]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            _, done = drive(options, "do it")
            client.close()
            session.close()
        self.assertEqual(done["message"], "Summary for the user.")
        self.assertTrue(done["finished"])

    def test_malformed_tool_arguments_become_a_tool_error(self) -> None:
        script = [{"sse": [{"tool": ("write_program", {"path": "x.py"})}]}, text_response("recovered")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            events, done = drive(options, "go")
            client.close()
            session.close()
        self.assertIn("tool.completed", kinds(events))
        completed = [e for e in events if e.kind == "tool.completed"][0]
        self.assertTrue(completed.data["is_error"])
        self.assertEqual(done["stop"], "stop")

    def test_unknown_tool_is_reported_to_the_model(self) -> None:
        script = [tool_response(("not_a_tool", {})), text_response("sorry")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            events, _ = drive(options, "go")
            client.close()
            session.close()
        completed = [e for e in events if e.kind == "tool.completed"][0]
        self.assertIn("unknown tool", completed.data["content"])


class TestDeniedTool(TempDirTestCase):
    def test_denied_call_never_runs_and_the_model_is_told(self) -> None:
        script = [
            tool_response(("share_text", {"text": "secret"})),
            text_response("I did not share it."),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            registry.policy = make_policy(prompter=auto_prompter(False))
            options = make_options(client, registry, session)
            events, done = drive(options, "share this")
            client.close()
            session.close()
        denied = [e for e in events if e.kind == "tool.denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].data["name"], "share_text")
        completed = [e for e in events if e.kind == "tool.completed"][0]
        self.assertTrue(completed.data["is_error"])
        self.assertIn("denied", completed.data["content"])
        self.assertEqual(done["stop"], "stop")

    def test_approval_is_recorded_on_the_registry(self) -> None:
        with MockProvider([tool_response(("share_text", {"text": "x"})), text_response("ok")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            registry.policy = make_policy(prompter=auto_prompter(True))
            options = make_options(client, registry, session)
            drive(options, "share")
            client.close()
            session.close()
        self.assertEqual(registry.approvals, [("share_text", True, "")])


class TestTruncation(TempDirTestCase):
    def test_a_huge_tool_result_is_clamped_and_spilled(self) -> None:
        # `run_program` is what really produces a huge result: a chatty program's stdout.
        script = [
            tool_response(("run_program", {"path_or_source": "print('y' * 40000)"})),
            text_response("done"),
        ]
        spill = self.path("workspace", "tool-output")
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(
                client, self.make_registry(), session, max_tool_result_chars=2000, spill_dir=spill
            )
            events, _ = drive(options, "write a lot")
            client.close()
            session.close()
        completed = [e for e in events if e.kind == "tool.completed"][0]
        self.assertTrue(completed.data["truncated"])
        self.assertLess(len(completed.data["content"]), 4000)
        self.assertIn("truncated", completed.data["content"])
        spill_path = completed.data["metadata"].get("spill_path")
        self.assertIsNotNone(spill_path)
        self.assertTrue(os.path.exists(spill_path))

    def test_small_results_are_untouched(self) -> None:
        script = [tool_response(("list_files", {})), text_response("done")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session, max_tool_result_chars=5000)
            events, _ = drive(options, "list")
            client.close()
            session.close()
        completed = [e for e in events if e.kind == "tool.completed"][0]
        self.assertFalse(completed.data["truncated"])


class TestTurnLimits(TempDirTestCase):
    def test_max_turns_stops_a_loop(self) -> None:
        # The model keeps calling a tool forever.
        script = [tool_response(("list_files", {}))]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session, max_turns=3)
            events, done = drive(options, "loop forever")
            client.close()
            session.close()
        self.assertEqual(done["stop"], "turn_limit")
        self.assertEqual(done["turns"], 3)
        self.assertIn("turn.limit", kinds(events))

    def test_zero_max_turns_still_runs_once(self) -> None:
        with MockProvider([text_response("once")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session, max_turns=0)
            _, done = drive(options, "hi")
            client.close()
            session.close()
        self.assertEqual(done["turns"], 1)


class TestResume(TempDirTestCase):
    def test_a_resumed_session_continues_the_conversation(self) -> None:
        first = [
            tool_response(("write_program", {"path": "keep.py", "source": "print('kept')\n", "purpose": "keep"})),
            text_response("Wrote keep.py."),
        ]
        with MockProvider(first) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            drive(options, "write keep.py")
            client.close()
            session.close()

        # Simulate the app being killed and relaunched.
        resumed = SessionLog.resume(self.path("session.jsonl"))
        with MockProvider([text_response("It is still there.")]) as provider:
            client = make_client(provider)
            options = make_options(client, self.make_registry(), resumed)
            drive(options, "is it still there?")
            client.close()
            sent = provider.messages_sent(0)
            resumed.close()

        # The interrupted conversation is replayed verbatim, then the new user message.
        self.assertEqual(
            [m["role"] for m in sent], ["system", "user", "assistant", "tool", "assistant", "user"]
        )
        self.assertEqual(sent[3]["name"], "write_program")
        self.assertEqual(sent[5]["content"], "is it still there?")

    def test_compaction_runs_at_the_turn_boundary(self) -> None:
        session = self.make_session()
        session.append("message.user", {"message": {"role": "user", "content": "old"}, "projectionShape": "chat-completions.v1"})
        with MockProvider([text_response("ok")]) as provider:
            client = make_client(provider)
            options = make_options(client, self.make_registry(), session)
            events, _ = drive(options, "hello")
            client.close()
            session.close()
        # No status event below the budget: compaction is a no-op until it is needed.
        self.assertNotIn("status", kinds(events))


class TestCancellation(TempDirTestCase):
    def test_a_set_stop_event_ends_the_turn(self) -> None:
        import threading

        with MockProvider([text_response("never sent")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            stop = threading.Event()
            stop.set()
            options = make_options(client, self.make_registry(), session, stop=stop)
            events, done = drive(options, "go")
            client.close()
            session.close()
        self.assertEqual(done["stop"], "cancelled")
        self.assertIn("error", kinds(events))
        self.assertEqual(len(provider.requests), 0)


if __name__ == "__main__":
    unittest.main()

"""Agent-loop tests: turn structure, approval, truncation, resume, and tool round trips.

These drive :func:`harness.loop.run_turn` against the mock provider, so they are the
closest thing here to a real conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from harness.errors import BadRequestError, MalformedResponseError
from harness.loop import (
    APPROVAL_REQUIRED_TOOLS,
    AUTO_APPROVED_TOOLS,
    ApprovalRequest,
    Event,
    auto_prompter,
    build_system_prompt,
    make_policy,
    _dispatch,
    run_turn,
)
from harness.session import SessionLog, assistant_message_event, user_message_event
from harness.tools import ToolResult

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
    def test_prompt_describes_desktop_without_claiming_pyto_runtime(self) -> None:
        with mock.patch("harness.loop.ios.is_pyto", return_value=False), mock.patch(
            "harness.loop.ios.available_capabilities", return_value={"pyto_ui": False, "speech": False}
        ):
            prompt = " ".join(build_system_prompt(self.make_config(), self.workspace_dir).split())
        self.assertIn("Desktop Python.", prompt)
        self.assertIn("do not claim that desktop execution verifies iPhone or iPad behavior", prompt)
        self.assertNotIn("running inside Pyto on the user's iPhone", prompt)
        self.assertNotIn("there is no background daemon, and iOS may suspend", prompt)
        self.assertIn("finish", prompt)

    def test_prompt_describes_pyto_runtime_conditionally(self) -> None:
        with mock.patch("harness.loop.ios.is_pyto", return_value=True), mock.patch(
            "harness.loop.ios.available_capabilities", return_value={"pyto_ui": True, "speech": True}
        ):
            prompt = " ".join(build_system_prompt(self.make_config(), self.workspace_dir).split())
        self.assertIn("Runtime: Pyto on iOS.", prompt)
        self.assertIn("there is no background daemon", prompt)
        self.assertIn("Detected runtime features: pyto_ui, speech", prompt)

    def test_prompt_selects_one_time_batch_and_interactive_workflows(self) -> None:
        prompt = " ".join(build_system_prompt(self.make_config(), self.workspace_dir).split())
        for phrase in (
            "one-time action",
            "do not create a saved program",
            "reusable batch program",
            "preview_program",
            "An app remaining open for interaction is not a batch timeout",
            "interactive app",
            "register_program before the final run",
        ):
            self.assertIn(phrase, prompt)
        self.assertNotIn("Keep programs short, bounded and non-interactive", prompt)

    def test_prompt_includes_scoped_objective_c_grounding_and_permissions(self) -> None:
        prompt = " ".join(build_system_prompt(self.make_config(), self.workspace_dir).split())
        for phrase in (
            "examples/objc_framework_recipes.py",
            "do not guess",
            "pyto_api does not inventory arbitrary Objective-C classes or selectors",
            "never use a direct bridge to bypass its approval flow",
        ):
            self.assertIn(phrase, prompt)

    def test_prompt_names_the_workspace(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir)
        self.assertIn(self.workspace_dir, prompt)

    def test_prompt_lists_missing_capabilities(self) -> None:
        with mock.patch("harness.loop.ios.available_capabilities", return_value={"speech": False}):
            prompt = " ".join(build_system_prompt(self.make_config(), self.workspace_dir).split())
        self.assertIn("runtime features were not detected here: speech", prompt)

    def test_extra_text_is_appended(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir, extra="Remember: be brief.")
        self.assertIn("be brief", prompt)

    def test_prompt_describes_saved_program_registration_and_no_model_run(self) -> None:
        prompt = build_system_prompt(self.make_config(), self.workspace_dir)
        for phrase in ("register_program", "/programs", "/run ID", "/edit ID", "--run-saved ID", "read_file"):
            self.assertIn(phrase, prompt)


class TestApprovalPolicy(unittest.TestCase):
    def test_workspace_and_read_tools_are_auto_approved(self) -> None:
        # `interactive=True` is what the CLI passes when a human can answer (a TTY, or
        # --web); program runs and previews stay AUTO there. With nobody attached, they are
        # denied without the explicit opt-in — see TestUnattendedPrograms in test_hardening.py.
        policy = make_policy(interactive=True)
        for name in ("write_program", "register_program", "list_saved_programs", "run_program", "preview_program", "read_file", "list_files", "memory_read", "finish"):
            self.assertTrue(policy(name, {}).allowed, name)

    def test_interactive_preview_is_denied_without_a_human_attached(self) -> None:
        self.assertFalse(make_policy(interactive=False)("preview_program", {}).allowed)

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

    def test_context_limit_refuses_before_provider_and_keeps_user_request(self) -> None:
        with MockProvider([text_response("must not be requested")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(
                client,
                self.make_registry(),
                session,
                max_provider_request_bytes=1,
            )
            events, done = drive(options, "Keep this full user request in the session.")
            client.close()
            projected = session.project()
            session.close()

        self.assertEqual(done["stop"], "context_limit")
        self.assertEqual(len(provider.requests), 0)
        self.assertIn("CONTEXT_LIMIT", [event.data.get("code") for event in events if event.kind == "error"])
        self.assertEqual(projected, [{"role": "user", "content": "Keep this full user request in the session."}])

    def test_provider_receives_bounded_history_while_session_keeps_full_history(self) -> None:
        with MockProvider([text_response("I kept the current request.")]) as provider:
            client = make_client(provider)
            session = self.make_session()
            old_request = "Old project conversation " + "x" * 200000
            session.append("message.user", user_message_event(old_request))
            session.append("message.assistant", assistant_message_event({"role": "assistant", "content": "Old answer."}))
            options = make_options(
                client,
                self.make_registry(),
                session,
                max_provider_request_bytes=100000,
                compact=False,
            )
            events, done = drive(options, "Keep the new requirement: use monthly totals.")
            sent = provider.messages_sent(0)
            client.close()
            durable = session.project()
            session.close()

        self.assertEqual(done["stop"], "stop")
        self.assertNotIn(old_request, [message.get("content") for message in sent])
        self.assertEqual(sent[-1], {"role": "user", "content": "Keep the new requirement: use monthly totals."})
        self.assertEqual(durable[0], {"role": "user", "content": old_request})
        self.assertTrue(any(event.kind == "status" and event.data.get("omitted_turns") == 1 for event in events))

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
    def test_one_time_action_uses_a_capability_without_creating_a_program(self) -> None:
        pasteboard = self.install_bridge("pasteboard")
        pasteboard.get = lambda: "one-time clipboard value"
        script = [tool_response(("clipboard_get", {})), text_response("The clipboard contains one-time clipboard value.")]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            events, done = drive(options, "Read my clipboard once and tell me what it contains.")
            client.close()

        called = [event.data["name"] for event in events if event.kind == "tool.completed"]
        self.assertEqual(called, ["clipboard_get"])
        self.assertFalse(os.path.exists(os.path.join(self.workspace_dir, "one_time.py")))
        self.assertEqual(done["stop"], "stop")

    def test_reusable_batch_is_written_registered_and_verified(self) -> None:
        source = "print('batch recipe verified')\n"
        def summary_after_registration(body):
            registered = next(
                message for message in body["messages"] if message.get("name") == "register_program"
            )
            program_id = registered["content"].split(" as ", 1)[1].split(" at ", 1)[0]
            return text_response(
                "Saved Daily summary (id {}) at daily.py; last run printed batch recipe verified.".format(
                    program_id
                )
            )

        script = [
            tool_response(("write_program", {"path": "daily.py", "source": source, "purpose": "Daily summary"})),
            tool_response(("register_program", {
                "title": "Daily summary",
                "purpose": "Print the daily summary.",
                "entry_file": "daily.py",
                "mode": "batch",
                "required_capabilities": ["files"],
            })),
            tool_response(("run_program", {"path_or_source": "daily.py"})),
            summary_after_registration,
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            options = make_options(client, registry, session)
            events, done = drive(options, "Create a reusable daily summary program and verify it.")
            client.close()

        called = [event.data["name"] for event in events if event.kind == "tool.completed"]
        self.assertEqual(called, ["write_program", "register_program", "run_program"])
        self.assertTrue(os.path.isfile(os.path.join(self.workspace_dir, "daily.py")))
        self.assertTrue(
            any("batch recipe verified" in event.data["content"] for event in events if event.kind == "tool.completed")
        )
        with open(os.path.join(self.workspace_dir, "pyto-programs.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        program = saved["programs"][0]
        self.assertEqual(program["last_verification_result"]["status"], "passed")
        self.assertIn(program["id"], done["message"])
        self.assertIn("daily.py", done["message"])
        self.assertEqual(done["stop"], "stop")

    def test_interactive_app_uses_preview_and_reports_interaction_as_unverified(self) -> None:
        source = "import pyto_ui\n# interactive app\n"
        preview_result = {
            "validation": {"checked": True, "passed": True, "syntax_passed": True, "imports_passed": True},
            "monitor": {
                "presentation_requested": True,
                "preview_opened": True,
                "preview_closed": True,
                "interaction_verified": False,
                "callback_successes": 0,
                "callback_errors": [],
            },
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "duration_s": 0.2,
            "is_error": False,
            "cleanup_pending": False,
            "surviving_threads": [],
        }
        def summary_after_preview(body):
            registered = next(
                message for message in body["messages"] if message.get("name") == "register_program"
            )
            program_id = registered["content"].split(" as ", 1)[1].split(" at ", 1)[0]
            return text_response(
                "Counter app id {} at counter_app.py opened; user interaction is not verified.".format(
                    program_id
                )
            )

        script = [
            tool_response(("write_program", {"path": "counter_app.py", "source": source, "purpose": "Counter app"})),
            tool_response(("register_program", {
                "title": "Counter app",
                "purpose": "Interactive counter.",
                "entry_file": "counter_app.py",
                "mode": "app",
                "required_capabilities": ["pyto_ui"],
            })),
            tool_response(("preview_program", {"path": "counter_app.py"})),
            summary_after_preview,
        ]
        with mock.patch("harness.tools_ios.previews.run_preview", return_value=preview_result):
            with MockProvider(script) as provider:
                client = make_client(provider)
                session = self.make_session()
                options = make_options(client, self.make_registry(), session)
                events, done = drive(options, "Build a persistent counter app that I can tap and reuse.")
                client.close()

        called = [event.data["name"] for event in events if event.kind == "tool.completed"]
        self.assertEqual(called, ["write_program", "register_program", "preview_program"])
        preview = next(
            event.data["content"]
            for event in events
            if event.kind == "tool.completed" and event.data.get("name") == "preview_program"
        )
        self.assertIn("Preview opened: yes", preview)
        self.assertIn("User interaction verified: no", preview)
        self.assertNotIn("run_program", called)
        with open(os.path.join(self.workspace_dir, "pyto-programs.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        program = saved["programs"][0]
        self.assertEqual(program["last_verification_result"]["status"], "preview_opened")
        self.assertIn(program["id"], done["message"])
        self.assertIn("not verified", done["message"])
        self.assertEqual(done["stop"], "stop")

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

    def test_write_then_run_in_the_same_batch_is_ordered(self) -> None:
        source = "print('same-batch ran')\n"
        script = [
            tool_response(
                ("write_program", {"path": "same_batch.py", "source": source, "purpose": "batch test"}),
                ("run_program", {"path_or_source": "same_batch.py"}),
            ),
            text_response("Both operations completed."),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            write_tool = registry.get("write_program")
            original_write = write_tool.handler

            def delayed_write(**arguments):
                time.sleep(0.1)
                return original_write(**arguments)

            write_tool.handler = delayed_write
            options = make_options(client, registry, session)
            _, done = drive(options, "write and run in one batch")
            sent_second = provider.messages_sent(1)
            client.close()
            session.close()

        run_result = next(message for message in sent_second if message.get("name") == "run_program")
        self.assertIn("same-batch ran", run_result["content"])
        self.assertEqual(done["message"], "Both operations completed.")

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
            tool_results = [message for message in session.project() if message.get("role") == "tool"]
            session.close()
        self.assertEqual([message["name"] for message in tool_results], ["list_files", "memory_read"])

    def test_each_result_is_durable_before_a_slower_batch_member_finishes(self) -> None:
        registry = self.make_registry()
        effects = []
        slow_started = asyncio.Event()
        never_release = asyncio.Event()

        @registry.tool("fast_action", "Fast test action.", inline=True)
        def fast_action():
            effects.append("fast")
            return ToolResult.ok("fast result")

        @registry.tool("slow_action", "Waits until the dispatch is cancelled.")
        async def slow_action():
            slow_started.set()
            await never_release.wait()
            effects.append("slow")
            return ToolResult.ok("slow result")

        session = self.make_session()
        session.append("message.user", user_message_event("run two actions"))
        session.append(
            "message.assistant",
            assistant_message_event(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "fast", "type": "function", "function": {"name": "fast_action", "arguments": "{}"}},
                        {"id": "slow", "type": "function", "function": {"name": "slow_action", "arguments": "{}"}},
                    ],
                }
            ),
        )
        options = make_options(None, registry, session, max_parallel_tools=2)

        async def interrupt_after_fast_result():
            async def consume_dispatch():
                async for _item in _dispatch(
                    options,
                    [("fast", "fast_action", {}), ("slow", "slow_action", {})],
                    turn=1,
                ):
                    pass

            task = asyncio.create_task(consume_dispatch())
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            for _ in range(200):
                if any(
                    event.type == "tool.completed" and event.data.get("id") == "fast"
                    for event in session.events
                ):
                    break
                await asyncio.sleep(0.005)
            else:
                self.fail("fast result waited for the slow tool before being journaled")

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(interrupt_after_fast_result())
        recovered = session.reconcile_tool_calls()
        projected = [message for message in session.project() if message.get("role") == "tool"]
        self.assertEqual(effects, ["fast"])
        self.assertEqual([(item["tool"], item["state"]) for item in recovered], [("slow_action", "unknown")])
        self.assertEqual([item["tool_call_id"] for item in projected], ["fast", "slow"])
        self.assertEqual(projected[0]["content"], "fast result")
        self.assertIn("Outcome unknown", projected[1]["content"])

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
    def test_strict_mock_provider_rejects_a_missing_tool_result(self) -> None:
        messages = [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "unfinished",
                        "type": "function",
                        "function": {"name": "write_program", "arguments": "{}"},
                    }
                ],
            },
        ]
        with MockProvider([text_response("ok")], strict_tool_protocol=True) as provider:
            client = make_client(provider)
            try:
                with self.assertRaisesRegex(BadRequestError, "invalid tool-call sequence"):
                    client.stream_sync(messages)
            finally:
                client.close()

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

    def test_unknown_side_effect_is_not_replayed_and_history_is_provider_valid(self) -> None:
        effects = ["already applied before the app stopped"]
        session = self.make_session()
        session.append("message.user", user_message_event("make the external change"))
        session.append(
            "message.assistant",
            assistant_message_event(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_side_effect",
                            "type": "function",
                            "function": {"name": "side_effect_probe", "arguments": "{}"},
                        }
                    ],
                }
            ),
        )
        session.append("tool.intent", {"id": "call_side_effect", "name": "side_effect_probe"})
        session.append("tool.started", {"id": "call_side_effect", "name": "side_effect_probe"})
        session.close()

        resumed = SessionLog.resume(session.path)
        registry = self.make_registry()

        @registry.tool("side_effect_probe", "Records a side effect for recovery coverage.", inline=True)
        def side_effect_probe():
            effects.append("replayed")
            return "changed"

        with MockProvider([text_response("I preserved the interrupted operation state.")], strict_tool_protocol=True) as provider:
            client = make_client(provider)
            options = make_options(client, registry, resumed)
            events, done = drive(options, "continue safely")
            sent = provider.messages_sent(0)
            client.close()
            resumed.close()

        self.assertTrue(done["finished"])
        self.assertEqual(effects, ["already applied before the app stopped"])
        roles = [message["role"] for message in sent]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
        recovered_result = sent[3]
        self.assertEqual(recovered_result["tool_call_id"], "call_side_effect")
        self.assertIn("Outcome unknown", recovered_result["content"])
        self.assertTrue(any("outcome" in event.data.get("message", "").lower() for event in events))


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

"""User-facing output tests for the shared browser and terminal renderer."""

from __future__ import annotations

import io
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import program_inputs, programs
from harness.loop import ApprovalRequest, Event
from harness.ui import Printer, UIApprover, format_history_page, handle_program_command, run_turn_sync, terminal_repl
from harness.tools_ios import Workspace, build_registry, default_context

from .mock_provider import MockProvider, text_response, tool_response
from .support import TempDirTestCase, make_client, make_options


class TestPrinter(unittest.TestCase):
    def make_printer(self, *, verbose: bool = False):
        stream = io.StringIO()
        return Printer(verbose=verbose, stream=stream), stream

    def test_large_pyto_reference_is_only_a_status_by_default(self) -> None:
        printer, stream = self.make_printer()
        body = "Pyto API reference\n" + "reference detail\n" * 100
        completed = Event("tool.completed", {"id": "ref", "name": "pyto_api", "content": body})
        printer.handle(Event("tool.started", {"id": "ref", "name": "pyto_api", "arguments": {"module": "photos"}}))
        printer.handle(completed)
        output = stream.getvalue()
        self.assertEqual(output.count("Checking the Pyto API reference…"), 1)
        self.assertNotIn("reference detail", output)
        self.assertEqual(completed.data["content"], body, "presentation must not alter the tool result")

    def test_verbose_shows_complete_arguments_body_and_timing(self) -> None:
        printer, stream = self.make_printer(verbose=True)
        argument = "x" * 400
        body = "diagnostic line\nsecond line"
        printer.handle(
            Event("tool.started", {"name": "read_file", "arguments": {"path": "long.txt", "query": argument}})
        )
        printer.handle(
            Event(
                "tool.completed",
                {
                    "name": "read_file",
                    "content": body,
                    "duration_ms": 13,
                    "is_error": False,
                    "truncated": True,
                    "metadata": {"spill_path": "tool-output/read_file.txt"},
                },
            )
        )
        output = stream.getvalue()
        self.assertIn(argument, output)
        self.assertIn("diagnostic line", output)
        self.assertIn("second line", output)
        self.assertIn("13 ms", output)
        self.assertIn("full output saved to tool-output/read_file.txt", output)

    def test_verbose_details_are_still_secret_scrubbed(self) -> None:
        printer, stream = self.make_printer(verbose=True)
        secret = "sk-12345678901234567890"
        printer.handle(Event("tool.started", {"name": "read_file", "arguments": {"token": secret}}))
        printer.handle(Event("tool.completed", {"name": "read_file", "content": secret}))
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn("<redacted>", stream.getvalue())

    def test_streamed_credentials_split_across_chunks_are_scrubbed(self) -> None:
        secret = "sk-12345678901234567890"
        printer, stream = self.make_printer()
        printer.handle(Event("delta", {"text": secret[:12]}))
        printer.handle(Event("delta", {"text": secret[12:]}))
        self.assertNotIn(secret[:12], stream.getvalue())
        printer.handle(Event("message.completed", {"content": secret}))
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn("<redacted>", stream.getvalue())

        verbose_printer, verbose_stream = self.make_printer(verbose=True)
        verbose_printer.handle(Event("reasoning.delta", {"text": secret[:12]}))
        verbose_printer.handle(Event("reasoning.delta", {"text": secret[12:]}))
        verbose_printer.handle(Event("message.completed", {"content": "safe", "reasoning": secret}))
        self.assertNotIn(secret, verbose_stream.getvalue())
        self.assertIn("<redacted>", verbose_stream.getvalue())

    def test_default_shows_saved_program_path_without_full_result(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("tool.started", {"name": "write_program", "arguments": {"source": "secret source"}}))
        printer.handle(
            Event(
                "tool.completed",
                {
                    "name": "write_program",
                    "content": "Wrote demo.py (40 lines). Full model-facing text",
                    "metadata": {"path": "demo.py"},
                    "is_error": False,
                },
            )
        )
        output = stream.getvalue()
        self.assertIn("Saving program…", output)
        self.assertIn("Saved program: demo.py", output)
        self.assertNotIn("secret source", output)
        self.assertNotIn("Full model-facing text", output)

    def test_error_shows_a_short_actionable_diagnostic(self) -> None:
        printer, stream = self.make_printer()
        body = "FAILED demo.py in 0.1s\n--- stderr ---\nNameError: name 'missing' is not defined\n" + "trace\n" * 30
        printer.handle(Event("tool.completed", {"name": "run_program", "is_error": True, "content": body}))
        output = stream.getvalue()
        self.assertIn("NameError: name 'missing' is not defined", output)
        self.assertNotIn("trace\ntrace", output)

    def test_denial_reason_is_shown_without_duplicate_tool_body(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("tool.denied", {"id": "share-1", "name": "share_text", "reason": "user declined"}))
        printer.handle(
            Event(
                "tool.completed",
                {"id": "share-1", "name": "share_text", "is_error": True, "content": "private shared text"},
            )
        )
        output = stream.getvalue()
        self.assertIn("user declined", output)
        self.assertNotIn("private shared text", output)
        self.assertEqual(output.count("Not run:"), 1)

    def test_interruption_is_actionable(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("error", {"code": "CANCELLED", "message": "cancelled"}))
        self.assertEqual(stream.getvalue(), "Interrupted.\n")

    def test_provider_error_is_concise_by_default_and_detailed_when_verbose(self) -> None:
        message = "Provider rejected the request: " + "x" * 500
        printer, stream = self.make_printer()
        printer.handle(Event("error", {"message": message}))
        self.assertLessEqual(len(stream.getvalue()), 248)
        self.assertNotIn("x" * 241, stream.getvalue())

        verbose_printer, verbose_stream = self.make_printer(verbose=True)
        verbose_printer.handle(Event("error", {"message": message}))
        self.assertIn("x" * 500, verbose_stream.getvalue())

    def test_terminal_interrupt_uses_the_shared_renderer_and_cancels_client(self) -> None:
        client = mock.Mock()
        session = SimpleNamespace(path="session.jsonl")
        answers = iter(("start", "/quit"))
        printer, stream = self.make_printer()
        with mock.patch("harness.ui.run_turn_sync", side_effect=KeyboardInterrupt):
            code = terminal_repl(
                options_factory=lambda _session: SimpleNamespace(client=client),
                session=session,
                printer=printer,
                input_fn=lambda _prompt: next(answers),
            )
        self.assertEqual(code, 0)
        self.assertIn("Interrupted.", stream.getvalue())
        client.cancel.assert_called_once_with()

    def test_finish_message_identical_to_streamed_text_appears_once(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("delta", {"text": "Ready."}))
        printer.handle(Event("message.completed", {"content": "Ready."}))
        printer.handle(Event("finished", {"message": "Ready."}))
        self.assertEqual(stream.getvalue(), "Ready.\n")

    def test_non_stream_delta_free_message_is_visible(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("message.completed", {"content": "non-streamed answer"}))
        self.assertEqual(stream.getvalue(), "non-streamed answer\n")


class TestPrinterWithProvider(TempDirTestCase):
    def test_streamed_and_non_streamed_answers_are_visible_once(self) -> None:
        specs = (
            (True, text_response("one complete answer")),
            (
                False,
                {
                    "json": {
                        "choices": [
                            {"message": {"role": "assistant", "content": "one complete answer"}}
                        ]
                    }
                },
            ),
        )
        for streaming, response in specs:
            with self.subTest(streaming=streaming):
                with MockProvider([response]) as provider:
                    client = make_client(provider)
                    session = self.make_session("session-{}.jsonl".format(streaming))
                    options = make_options(
                        client,
                        self.make_registry(),
                        session,
                        stream=streaming,
                    )
                    printer_stream = io.StringIO()
                    done = run_turn_sync(options, "answer", Printer(stream=printer_stream))
                    client.close()
                self.assertTrue(done["finished"])
                self.assertEqual(printer_stream.getvalue().count("one complete answer"), 1)

    def test_finish_tool_result_is_printed_once(self) -> None:
        message = "Saved demo.py and checked it successfully."
        with MockProvider([tool_response(("finish", {"message": message}))]) as provider:
            client = make_client(provider)
            session = self.make_session()
            options = make_options(client, self.make_registry(), session)
            printer_stream = io.StringIO()
            done = run_turn_sync(options, "finish the task", Printer(stream=printer_stream))
            client.close()
        self.assertTrue(done["finished"])
        self.assertEqual(printer_stream.getvalue().count(message), 1)

    def test_tool_result_still_reaches_the_model_unchanged(self) -> None:
        script = [
            tool_response(("list_files", {"pattern": "*.missing"})),
            text_response("No matching files were found."),
        ]
        with MockProvider(script) as provider:
            client = make_client(provider)
            session = self.make_session()
            registry = self.make_registry()
            options = make_options(client, registry, session)
            printer_stream = io.StringIO()
            run_turn_sync(options, "find missing files", Printer(stream=printer_stream))
            messages = provider.messages_sent(1)
            client.close()
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("No files matching", tool_messages[0]["content"])
        self.assertNotIn("No files matching", printer_stream.getvalue())


class TestHistoryPage(unittest.TestCase):
    def test_pages_are_bounded_and_expose_older_session_messages(self) -> None:
        messages = [{"role": "user", "content": "turn {}".format(index)} for index in range(30)]
        recent, has_older = format_history_page(messages, page_size=12, session_path="sessions/chat.jsonl")
        older, has_even_older = format_history_page(messages, offset=12, page_size=12)
        self.assertIn("turn 18", recent)
        self.assertIn("turn 29", recent)
        self.assertIn("sessions/chat.jsonl", recent)
        self.assertTrue(has_older)
        self.assertIn("turn 6", older)
        self.assertIn("turn 17", older)
        self.assertTrue(has_even_older)

    def test_long_message_is_shortened_in_the_view_without_mutating_history(self) -> None:
        message = {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": "a" * 4000 + "\nsk-12345678901234567890"}
        original = dict(message)
        rendered, _ = format_history_page([message])
        self.assertIn("characters omitted from this page", rendered)
        self.assertIn("<redacted>", rendered)
        self.assertEqual(message, original)


class TestUIApprover(unittest.TestCase):
    @staticmethod
    def request(tool: str) -> ApprovalRequest:
        return ApprovalRequest(tool=tool, arguments={"url": "https://example.test"}, reason="test", workspace="")

    def test_requests_queue_and_stale_or_repeated_answers_are_ignored(self) -> None:
        approver = UIApprover()
        presented = []
        approver.set_presenter(lambda request, token: presented.append((request.tool, token)))
        answers = {}

        first = threading.Thread(target=lambda: answers.setdefault("first", approver(self.request("open_url"))))
        second = threading.Thread(target=lambda: answers.setdefault("second", approver(self.request("share_text"))))
        first.start()
        self.wait_for(lambda: len(approver._queue) == 1)
        self.wait_for(lambda: len(presented) == 1)
        second.start()
        self.wait_for(lambda: len(approver._queue) == 2)

        first_token = presented[0][1]
        self.assertTrue(approver.answer(first_token, True))
        self.wait_for(lambda: len(presented) == 2)
        second_token = presented[1][1]
        self.assertFalse(approver.answer(first_token, False), "a repeated tap cannot answer the next request")
        self.assertTrue(approver.answer(second_token, False))
        first.join(1)
        second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(answers, {"first": True, "second": False})
        self.assertEqual([item[0] for item in presented], ["open_url", "share_text"])

    def test_cancel_and_close_deny_waiters_and_close_rejects_future_prompts(self) -> None:
        for operation in ("cancel", "close"):
            with self.subTest(operation=operation):
                approver = UIApprover()
                visible = threading.Event()
                result = []
                approver.set_presenter(lambda _request, _token: visible.set())
                worker = threading.Thread(target=lambda: result.append(approver(self.request("share_text"))))
                worker.start()
                self.assertTrue(visible.wait(1))
                getattr(approver, operation)()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [False])
                if operation == "close":
                    self.assertFalse(approver(self.request("open_url")))

    @staticmethod
    def wait_for(condition, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        raise AssertionError("condition did not become true before timeout")


class TestProgramChatCommands(TempDirTestCase):
    """Terminal saved-program commands use the shared registry and runner."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = Workspace(self.workspace_dir)
        source_path = self.workspace.resolve("notes/daily.py")
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write("print('saved program ran without a model')\n")
        self.record = programs.register(
            self.workspace,
            title="Daily notes",
            purpose="Print a note summary.",
            entry_file="notes/daily.py",
            mode="batch",
        )
        self.context = default_context(self.workspace_dir, self.path("spill"))
        self.registry = build_registry(self.context)

    def make_options_factory(self, client):
        def factory(_session):
            return SimpleNamespace(client=client, stop=threading.Event(), registry=self.registry)

        factory.context = self.context
        factory.registry = self.registry
        return factory

    def test_terminal_run_command_executes_without_model_turn(self) -> None:
        client = SimpleNamespace()
        session = SimpleNamespace(path="session.jsonl")
        output = io.StringIO()
        printer = Printer(stream=output)
        answers = iter(("/run {}".format(self.record["id"]), "/quit"))
        with mock.patch("harness.ui.run_turn_sync", side_effect=AssertionError("model turn was used")):
            code = terminal_repl(
                options_factory=self.make_options_factory(client),
                session=session,
                printer=printer,
                input_fn=lambda _prompt: next(answers),
            )
        self.assertEqual(code, 0)
        self.assertIn("saved program ran without a model", output.getvalue())
        self.assertEqual(programs.find_program(self.workspace, self.record["id"])["last_verification_result"]["status"], "passed")

    def test_edit_command_passes_the_selected_record_into_the_model_turn(self) -> None:
        client = SimpleNamespace()
        session = SimpleNamespace(path="session.jsonl")
        options = self.make_options_factory(client)(session)
        captured = []
        with mock.patch("harness.ui.run_turn_sync", side_effect=lambda _options, prompt, _printer: captured.append(prompt) or {}):
            handled = handle_program_command(
                "/edit {} Add a date heading".format(self.record["id"]),
                options_factory=self.make_options_factory(client),
                session=session,
                printer=Printer(stream=io.StringIO()),
                options=options,
            )
        self.assertTrue(handled)
        self.assertEqual(len(captured), 1)
        self.assertIn(self.record["id"], captured[0])
        self.assertIn("notes/daily.py", captured[0])
        self.assertIn("Add a date heading", captured[0])

    def test_cancelled_program_inputs_never_start_the_saved_runner(self) -> None:
        record = programs.register(
            self.workspace,
            title=self.record["title"],
            purpose=self.record["purpose"],
            entry_file=self.record["entry_file"],
            mode="batch",
            program_id=self.record["id"],
            input_schema=[{"name": "folder", "label": "Folder", "type": "folder"}],
        )
        client = SimpleNamespace()
        session = SimpleNamespace(path="session.jsonl")
        output = io.StringIO()

        def cancel(_record):
            raise program_inputs.InputsCancelled("cancelled")

        with mock.patch("harness.programs.execute_saved", side_effect=AssertionError("runner started")):
            handled = handle_program_command(
                "/run {}".format(record["id"]),
                options_factory=self.make_options_factory(client),
                session=session,
                printer=Printer(stream=output),
                input_collector=cancel,
            )
        self.assertTrue(handled)
        self.assertIn("Input cancelled. The program was not run.", output.getvalue())

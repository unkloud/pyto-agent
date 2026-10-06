"""User-facing output tests for the shared terminal and GUI printer."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import program_inputs, programs
from harness.loop import ApprovalRequest, Event, make_policy
from harness.ui import Printer, Transcript, UIApprover, _UIStream, collect_program_inputs_ui, format_history_page, handle_program_command, run_turn_sync, run_ui, terminal_repl
from harness.tools_ios import Workspace, build_registry, default_context

from .mock_provider import MockProvider, text_response, tool_response
from .support import TempDirTestCase, make_client, make_options


class TestTranscript(unittest.TestCase):
    def test_completed_streamed_message_is_committed_once(self) -> None:
        transcript = Transcript()
        transcript.append_delta("hello ")
        transcript.append_delta("world")
        transcript.complete_message("hello world")
        self.assertEqual(transcript.render(), "hello world")

    def test_final_message_replaces_a_partial_mismatch(self) -> None:
        transcript = Transcript()
        transcript.append_delta("half answer")
        transcript.complete_message("corrected answer")
        self.assertEqual(transcript.render(), "corrected answer")


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
        self.assertEqual(printer.transcript.render(), "Ready.")

    def test_non_stream_delta_free_message_is_visible(self) -> None:
        printer, stream = self.make_printer()
        printer.handle(Event("message.completed", {"content": "non-streamed answer"}))
        self.assertEqual(stream.getvalue(), "non-streamed answer\n")
        self.assertEqual(printer.transcript.render(), "non-streamed answer")


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


class TestUIStream(unittest.TestCase):
    class TextView:
        def __init__(self) -> None:
            self._text = ""
            self.assignment_count = 0

        @property
        def text(self) -> str:
            return self._text

        @text.setter
        def text(self, value: str) -> None:
            self.assignment_count += 1
            self._text = value

    def test_display_history_is_bounded(self) -> None:
        view = self.TextView()
        output = _UIStream(view)
        output.write("a" * 210_000)
        self.assertLessEqual(len(view.text), 80_000)
        self.assertTrue(view.text.endswith("a" * 100))

    def test_small_writes_are_coalesced_until_interval_or_forced_flush(self) -> None:
        view = self.TextView()
        with mock.patch("harness.ui.time.monotonic", return_value=100.0):
            output = _UIStream(view)
            for piece in ("token ",) * 100:
                output.write(piece)
                output.flush()
            self.assertEqual(view.assignment_count, 0)
            output.flush(force=True)
        self.assertEqual(view.assignment_count, 1)
        self.assertEqual(view.text, "token " * 100)

    def test_history_display_does_not_replace_the_chat_buffer(self) -> None:
        view = self.TextView()
        output = _UIStream(view)
        output.write("chat transcript")
        output.flush(force=True)
        output.replace_display("older history page")
        self.assertEqual(view.text, "older history page")
        output.restore_display()
        self.assertEqual(view.text, "chat transcript")

    def test_close_prevents_all_later_view_writes(self) -> None:
        view = self.TextView()
        output = _UIStream(view)
        output.write("before")
        output.flush(force=True)
        output.close()
        assignments = view.assignment_count
        output.write("after")
        output.flush()
        self.assertEqual(view.assignment_count, assignments)
        self.assertEqual(view.text, "before")

    def test_view_assignment_errors_are_not_suppressed(self) -> None:
        class BrokenView:
            @property
            def text(self):
                return ""

            @text.setter
            def text(self, _value):
                raise RuntimeError("view update failed")

        output = _UIStream(BrokenView())
        output.write("chat text")
        with self.assertRaisesRegex(RuntimeError, "view update failed"):
            output.flush(force=True)


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


class TestPytoChatLifecycle(unittest.TestCase):
    """Use only PytoUI members documented by Pyto; omit invented dispatch APIs."""

    class View:
        def __init__(self) -> None:
            self.title = ""
            self.background_color = None
            self.width = 390
            self.height = 844
            self.frame = None
            self.subviews = []
            self.closed = False

        def add_subview(self, view) -> None:
            self.subviews.append(view)

        def close(self) -> None:
            self.closed = True

    class TextView(TestUIStream.TextView):
        def __init__(self) -> None:
            super().__init__()
            self.editable = True
            self.font = None
            self.background_color = None
            self.text_color = None
            self.frame = None

    class TextField:
        def __init__(self) -> None:
            self.placeholder = ""
            self.background_color = None
            self.text_color = None
            self.frame = None
            self.action = None
            self.text = ""
            self.enabled = True

    class Button:
        def __init__(self) -> None:
            self.title = ""
            self.background_color = None
            self.text_color = None
            self.frame = None
            self.action = None
            self.enabled = True

    class Client:
        def __init__(self) -> None:
            self.reset_count = 0
            self.cancel_count = 0
            self.cancelled = threading.Event()

        def reset_cancel(self) -> None:
            self.reset_count += 1
            self.cancelled.clear()

        def cancel(self) -> None:
            self.cancel_count += 1
            self.cancelled.set()

    class Session:
        path = "session.jsonl"

        def __init__(self) -> None:
            self.closed = False
            self.events = []
            self.messages = []

        def project(self):
            return list(self.messages)

        def append(self, event: str) -> None:
            if self.closed:
                raise AssertionError("worker wrote to a closed session")
            self.events.append(event)

        def close(self) -> None:
            self.closed = True

    class StrictPytoUI:
        COLOR_SYSTEM_BACKGROUND = object()
        COLOR_SECONDARY_SYSTEM_BACKGROUND = object()
        COLOR_TERTIARY_SYSTEM_BACKGROUND = object()
        COLOR_LABEL = object()
        COLOR_SYSTEM_BLUE = object()
        COLOR_WHITE = object()
        FLEXIBLE_LEFT_MARGIN = 1
        FLEXIBLE_WIDTH = 2
        FLEXIBLE_RIGHT_MARGIN = 4
        FLEXIBLE_TOP_MARGIN = 8
        FLEXIBLE_HEIGHT = 16
        FLEXIBLE_BOTTOM_MARGIN = 32

        def __init__(self, show_view) -> None:
            self.View = TestPytoChatLifecycle.View
            self.TextView = TestPytoChatLifecycle.TextView
            self.TextField = TestPytoChatLifecycle.TextField
            self.Button = TestPytoChatLifecycle.Button
            self.Font = lambda name, size: (name, size)
            self.presented = []
            self._show_view = show_view

        def show_view(self, view) -> None:
            self.presented.append(view)
            self._show_view(view)

    @staticmethod
    def wait_for(condition, message: str) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        raise AssertionError(message)

    def test_callbacks_are_ready_before_presentation_and_close_drains_two_turns(self) -> None:
        client = self.Client()
        session = self.Session()
        second_started = threading.Event()
        third_started = threading.Event()
        prompts = []

        def run_turn(options, prompt, printer):
            prompts.append(prompt)
            if prompt in ("second request", "third request"):
                (second_started if prompt == "second request" else third_started).set()
                self.assertTrue(client.cancelled.wait(2), "Stop or dismissal should cancel the active request")
                session.append(prompt + " finished")
                # A stopped turn may produce a final event. The stream stays live for a
                # Stop action and is closed before cancellation on window dismissal.
                printer.write("late completion")
            else:
                session.append("first turn finished")
            return {"finished": True}

        def show_view(view):
            self.assertTrue(view.subviews)
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            entry = next(item for item in view.subviews if isinstance(item, self.TextField))
            transcript = next(item for item in view.subviews if isinstance(item, self.TextView))
            send = buttons["Send"]
            stop = buttons["Stop"]
            close = buttons["Close"]
            self.assertTrue(callable(send.action))
            self.assertTrue(callable(entry.action))
            self.assertTrue(callable(stop.action))
            self.assertTrue(callable(close.action))

            entry.text = "first request"
            send.action(send)
            self.wait_for(lambda: len(prompts) == 1 and send.enabled, "first turn did not complete")

            entry.text = "second request"
            entry.action(entry)
            self.assertTrue(second_started.wait(2), "second turn did not start")
            self.assertTrue(stop.enabled)

            stop.action(stop)
            self.wait_for(lambda: len(prompts) == 2 and send.enabled, "Stop did not finish the second turn")
            self.assertEqual(stop.title, "Stop")

            entry.text = "third request"
            send.action(send)
            self.assertTrue(third_started.wait(2), "third turn did not start")
            close.action(close)  # same supported close method used by a PytoUI view
            self.assertTrue(view.closed)
            self.dismissed_assignments = transcript.assignment_count

        ui = self.StrictPytoUI(show_view)
        # These names caused the original defect and are intentionally absent from the
        # strict contract fake: pyto_ui.View.present and pyto_ui.main_thread.
        self.assertFalse(hasattr(ui, "main_thread"))
        self.assertFalse(hasattr(self.View(), "present"))

        def run_and_close():
            try:
                run_ui(
                    options_factory=lambda _session: SimpleNamespace(client=client, stop=threading.Event()),
                    session=session,
                )
            finally:
                session.close()

        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            with mock.patch("harness.ui.run_turn_sync", side_effect=run_turn):
                run_and_close()

        self.assertEqual(len(ui.presented), 1)
        self.assertEqual(prompts, ["first request", "second request", "third request"])
        self.assertEqual(client.reset_count, 3)
        self.assertEqual(client.cancel_count, 2)
        self.assertEqual(
            session.events,
            ["first turn finished", "second request finished", "third request finished"],
        )
        self.assertTrue(session.closed)
        self.assertEqual(ui.presented[0].subviews[0].assignment_count, self.dismissed_assignments)

    def test_history_button_pages_the_session_and_returns_to_chat(self) -> None:
        session = self.Session()
        session.messages = [{"role": "user", "content": "saved turn {}".format(index)} for index in range(13)]
        client = self.Client()

        def show_view(view):
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            transcript = next(item for item in view.subviews if isinstance(item, self.TextView))
            history = buttons["History"]
            history.action(history)
            self.assertIn("Session history", transcript.text)
            self.assertIn("saved turn 1", transcript.text)
            self.assertNotIn("saved turn 0", transcript.text)
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            older = buttons["Older"]
            older.action(older)
            self.assertIn("saved turn 0", transcript.text)
            chat = next(item for item in view.subviews if isinstance(item, self.Button) and item.title == "Chat")
            chat.action(chat)
            self.assertIn("saved turn 1", transcript.text)
            self.assertNotIn("saved turn 0", transcript.text)
            self.assertEqual(transcript.flex, [self.StrictPytoUI.FLEXIBLE_WIDTH, self.StrictPytoUI.FLEXIBLE_HEIGHT])
            view.close()

        ui = self.StrictPytoUI(show_view)
        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            run_ui(
                options_factory=lambda _session: SimpleNamespace(client=client, stop=threading.Event()),
                session=session,
            )

    def test_approvals_are_answered_in_chat_and_actions_run_only_once_when_allowed(self) -> None:
        client = self.Client()
        session = self.Session()
        approver = UIApprover()
        executed = []
        requests = (
            ApprovalRequest("share_text", {"text": "weekly summary"}, "shares data", "/workspace"),
            ApprovalRequest("open_url", {"url": "https://example.test"}, "opens a link", "/workspace"),
        )
        policy = make_policy(prompter=approver, workspace="/workspace")

        def run_turn(_options, _prompt, _printer):
            for request in requests:
                if policy(request.tool, request.arguments).allowed:
                    executed.append(request.tool)
            return {"finished": True}

        def show_view(view):
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            transcript = next(item for item in view.subviews if isinstance(item, self.TextView))
            entry = next(item for item in view.subviews if isinstance(item, self.TextField))
            allow = buttons["Allow"]
            deny = buttons["Deny"]
            send = buttons["Send"]
            entry.text = "share a summary and open a link"
            send.action(send)

            self.wait_for(
                lambda: "share_text(text='weekly summary')" in transcript.text and allow.enabled,
                "share approval was not shown in the chat",
            )
            old_allow = allow.action
            old_allow(allow)
            old_allow(allow)  # repeated callback retains the first request token

            self.wait_for(
                lambda: "open_url(url='https://example.test')" in transcript.text and deny.enabled,
                "queued URL approval was not shown after the first response",
            )
            old_allow(allow)  # a stale approval cannot answer the current request
            self.assertTrue(deny.enabled)
            deny.action(deny)
            deny.action(deny)
            self.wait_for(lambda: send.enabled, "turn did not finish after the denial")

        ui = self.StrictPytoUI(show_view)
        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            with mock.patch("harness.ui.run_turn_sync", side_effect=run_turn):
                run_ui(
                    options_factory=lambda _session: SimpleNamespace(client=client, stop=threading.Event()),
                    session=session,
                    approver=approver,
                )

        self.assertEqual(executed, ["share_text"])
        self.assertIn("Approval allowed.", ui.presented[0].subviews[0].text)
        self.assertIn("Approval denied.", ui.presented[0].subviews[0].text)

    def test_stop_denies_the_visible_approval(self) -> None:
        client = self.Client()
        session = self.Session()
        approver = UIApprover()
        executed = []
        policy = make_policy(prompter=approver)

        def run_turn(_options, _prompt, _printer):
            if policy("share_text", {"text": "private"}).allowed:
                executed.append("share_text")
            return {"finished": True}

        def show_view(view):
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            entry = next(item for item in view.subviews if isinstance(item, self.TextField))
            entry.text = "share something"
            buttons["Send"].action(buttons["Send"])
            self.wait_for(lambda: buttons["Allow"].enabled, "approval did not appear")
            buttons["Stop"].action(buttons["Stop"])
            self.wait_for(lambda: buttons["Send"].enabled, "stopped turn did not finish")

        ui = self.StrictPytoUI(show_view)
        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            with mock.patch("harness.ui.run_turn_sync", side_effect=run_turn):
                run_ui(
                    options_factory=lambda _session: SimpleNamespace(client=client, stop=threading.Event()),
                    session=session,
                    approver=approver,
                )
        self.assertEqual(executed, [])
        self.assertEqual(client.cancel_count, 1)

    def test_closing_with_an_approval_pending_denies_it_and_drains_worker(self) -> None:
        client = self.Client()
        session = self.Session()
        approver = UIApprover()
        executed = []
        policy = make_policy(prompter=approver)

        def run_turn(_options, _prompt, _printer):
            if policy("open_url", {"url": "https://example.test"}).allowed:
                executed.append("open_url")
            return {"finished": True}

        def show_view(view):
            buttons = {item.title: item for item in view.subviews if isinstance(item, self.Button)}
            entry = next(item for item in view.subviews if isinstance(item, self.TextField))
            entry.text = "open a link"
            buttons["Send"].action(buttons["Send"])
            self.wait_for(lambda: buttons["Allow"].enabled, "approval did not appear")
            buttons["Close"].action(buttons["Close"])
            self.assertTrue(view.closed)

        ui = self.StrictPytoUI(show_view)
        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            with mock.patch("harness.ui.run_turn_sync", side_effect=run_turn):
                run_ui(
                    options_factory=lambda _session: SimpleNamespace(client=client, stop=threading.Event()),
                    session=session,
                    approver=approver,
                )
        self.assertEqual(executed, [])
        self.assertEqual(client.cancel_count, 1)


class TestProgramChatCommands(TempDirTestCase):
    """Saved-program Run/Edit controls use the same registry in chat and terminal."""

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
        client = TestPytoChatLifecycle.Client()
        session = TestPytoChatLifecycle.Session()
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

    def test_gui_programs_button_lists_and_run_command_does_not_call_model(self) -> None:
        client = TestPytoChatLifecycle.Client()
        session = TestPytoChatLifecycle.Session()
        dismissed = {}

        def show_view(view):
            buttons = {item.title: item for item in view.subviews if isinstance(item, TestPytoChatLifecycle.Button)}
            transcript = next(item for item in view.subviews if isinstance(item, TestPytoChatLifecycle.TextView))
            entry = next(item for item in view.subviews if isinstance(item, TestPytoChatLifecycle.TextField))
            buttons["Programs"].action(buttons["Programs"])
            self.assertIn(self.record["id"], transcript.text)
            entry.text = "/run {}".format(self.record["id"])
            buttons["Send"].action(buttons["Send"])
            TestPytoChatLifecycle.wait_for(
                lambda: buttons["Send"].enabled and "saved program ran without a model" in transcript.text,
                "the saved Run action did not finish in chat",
            )
            buttons["Close"].action(buttons["Close"])
            dismissed["closed"] = view.closed

        ui = TestPytoChatLifecycle.StrictPytoUI(show_view)
        with mock.patch("harness.ui.pyto_ui_module", return_value=ui):
            with mock.patch("harness.ui.run_turn_sync", side_effect=AssertionError("model turn was used")):
                run_ui(options_factory=self.make_options_factory(client), session=session)
        self.assertTrue(dismissed["closed"])

    def test_edit_command_passes_the_selected_record_into_the_model_turn(self) -> None:
        client = TestPytoChatLifecycle.Client()
        session = TestPytoChatLifecycle.Session()
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
        client = TestPytoChatLifecycle.Client()
        session = TestPytoChatLifecycle.Session()
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


class TestProgramInputForm(unittest.TestCase):
    class Element:
        def __init__(self) -> None:
            self.subviews = []
            self.frame = None

        def add_subview(self, child) -> None:
            self.subviews.append(child)

    class View(Element):
        def __init__(self) -> None:
            super().__init__()
            self.width = 390
            self.height = 844
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class ScrollView(Element):
        def __init__(self) -> None:
            super().__init__()
            self.content_view = TestProgramInputForm.Element()

    class Label(Element):
        def __init__(self, text="") -> None:
            super().__init__()
            self.text = text
            self.number_of_lines = 1
            self.text_color = None

    class TextField(Element):
        def __init__(self, text="", placeholder="") -> None:
            super().__init__()
            self.text = text
            self.placeholder = placeholder
            self.keyboard_type = None

    class Button(Element):
        def __init__(self, title="") -> None:
            super().__init__()
            self.title = title
            self.action = None
            self.enabled = True

    class UI:
        COLOR_SYSTEM_BACKGROUND = object()
        COLOR_SYSTEM_RED = object()
        COLOR_LABEL = object()
        KeyboardType = SimpleNamespace(DECIMAL_PAD="decimal-pad")

        def __init__(self, on_show) -> None:
            self.View = TestProgramInputForm.View
            self.ScrollView = TestProgramInputForm.ScrollView
            self.Label = TestProgramInputForm.Label
            self.TextField = TestProgramInputForm.TextField
            self.Button = TestProgramInputForm.Button
            self._on_show = on_show

        def show_view(self, view) -> None:
            self._on_show(view)

    @staticmethod
    def _walk(view):
        pending = [view]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(getattr(item, "subviews", []))
            content = getattr(item, "content_view", None)
            if content is not None:
                pending.append(content)

    def test_form_validates_then_returns_typed_values_from_controls_and_picker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            captured = {}

            def show(view):
                captured["view"] = view
                fields = [item for item in self._walk(view) if isinstance(item, self.TextField)]
                self.assertEqual(len(fields), 1)
                fields[0].text = "not a number"
                buttons = [item for item in self._walk(view) if isinstance(item, self.Button)]
                next(item for item in buttons if item.title == "Pick folder").action(None)
                run = next(item for item in buttons if item.title == "Run")
                run.action(None)
                self.assertFalse(view.closed, "invalid values should leave the form open")
                self.assertTrue(any("must be a number" in item.text for item in self._walk(view) if isinstance(item, self.Label)))
                fields[0].text = "4"
                next(item for item in buttons if item.title.startswith("Group by:")).action(None)
                run.action(None)

            ui = self.UI(show)
            values = collect_program_inputs_ui(
                ui,
                {
                    "title": "Organizer",
                    "input_schema": [
                        {"name": "max_files", "label": "Maximum files", "type": "number", "integer": True, "minimum": 1},
                        {"name": "group_by", "label": "Group by", "type": "choice", "choices": ["extension", "first letter"]},
                        {"name": "folder", "label": "Folder", "type": "folder"},
                    ],
                },
                file_system=SimpleNamespace(pick_directory=lambda: folder),
            )
            self.assertEqual(values["max_files"], 4)
            self.assertEqual(values["group_by"], "first letter")
            self.assertEqual(values["folder"], os.path.realpath(folder))
            self.assertTrue(captured["view"].closed)

    def test_cancel_form_reports_cancellation_without_values(self) -> None:
        from harness.ui import collect_program_inputs_ui

        def show(view):
            cancel = next(
                item for item in self._walk(view)
                if isinstance(item, self.Button) and item.title == "Cancel"
            )
            cancel.action(None)

        ui = self.UI(show)
        with self.assertRaises(program_inputs.InputsCancelled):
            collect_program_inputs_ui(
                ui,
                {"title": "Organizer", "input_schema": [{"name": "name", "label": "Name", "type": "text"}]},
            )

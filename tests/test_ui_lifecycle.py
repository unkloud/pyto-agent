"""Strict Pyto UI contract and lifecycle regression tests."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.ui import _UIStream, run_ui


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
        self.assertLessEqual(len(view.text), 200_000)
        self.assertTrue(view.text.endswith("a" * 100))

    def test_close_prevents_all_later_view_writes(self) -> None:
        view = self.TextView()
        output = _UIStream(view)
        output.write("before")
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

        with self.assertRaisesRegex(RuntimeError, "view update failed"):
            _UIStream(BrokenView()).write("chat text")


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

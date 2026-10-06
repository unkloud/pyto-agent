"""iOS adapter tests: URL construction, and graceful degradation off-device.

The adapters are tested on Linux *because* degrading gracefully is the contract.  A
handful of tests inject fake bridge modules into ``sys.modules`` to exercise the native
path without an iPhone.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
import urllib.parse
from unittest import mock

from harness import ios
from harness.errors import ConfigError

from .support import TempDirTestCase


class TestPlatformReporting(unittest.TestCase):
    def test_platform_label_mentions_desktop_here(self) -> None:
        self.assertIn("desktop", ios.platform_label())

    def test_not_ios_on_linux(self) -> None:
        self.assertFalse(ios.is_ios())

    def test_capability_report_lists_native_and_missing(self) -> None:
        report = ios.capability_report()
        self.assertIn("native:", report)
        self.assertIn("unavailable (will degrade):", report)

    def test_available_capabilities_shape(self) -> None:
        caps = ios.available_capabilities()
        for name in ("pasteboard", "share", "notifications", "speech", "photos", "calendar_events", "background"):
            self.assertIn(name, caps)
            self.assertIsInstance(caps[name], bool)


class TestClipboard(TempDirTestCase):
    def test_get_degrades_without_the_bridge(self) -> None:
        result = ios.clipboard_get()
        self.assertFalse(result.supported)
        self.assertFalse(result.ok)
        self.assertIn("pasteboard", result.detail)

    def test_set_degrades_without_the_bridge(self) -> None:
        result = ios.clipboard_set("hello")
        self.assertFalse(result.ok)
        self.assertIn("hello", result.detail)

    def test_get_with_a_bridge(self) -> None:
        module = self.install_bridge("pasteboard")
        module.get = lambda: "copied text"
        result = ios.clipboard_get()
        self.assertTrue(result.ok)
        self.assertTrue(result.supported)
        self.assertEqual(result.data["text"], "copied text")
        self.assertEqual(result.method, "pasteboard.get")

    def test_set_with_a_bridge(self) -> None:
        module = self.install_bridge("pasteboard")
        module.set = module.record
        result = ios.clipboard_set("write me")
        self.assertTrue(result.ok)
        self.assertEqual(module.calls[0][0], ("write me",))

    def test_a_raising_bridge_is_reported_not_propagated(self) -> None:
        module = self.install_bridge("pasteboard")

        def explode():
            raise RuntimeError("device locked")

        module.get = explode
        result = ios.clipboard_get()
        self.assertFalse(result.ok)
        self.assertTrue(result.supported)
        self.assertIn("device locked", result.detail)


class TestShare(TempDirTestCase):
    def test_share_text_falls_back_to_a_file(self) -> None:
        result = ios.share_text("some text to share", title="note")
        self.assertTrue(result.ok)
        self.assertFalse(result.supported)
        self.assertEqual(result.method, "file-fallback")
        path = result.data["path"]
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "some text to share")

    def test_share_text_uses_the_bridge_when_present(self) -> None:
        module = self.install_bridge("share")
        module.open = module.record
        result = ios.share_text("hi")
        self.assertTrue(result.supported)
        self.assertEqual(result.method, "share.open")
        self.assertEqual(module.calls[0][0], ("hi",))

    def test_share_file_missing_path(self) -> None:
        result = ios.share_file(self.path("nope.txt"))
        self.assertFalse(result.ok)
        self.assertIn("no such file", result.detail)

    def test_share_file_falls_back_to_reporting_the_path(self) -> None:
        target = self.path("thing.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        result = ios.share_file(target)
        self.assertTrue(result.ok)
        self.assertFalse(result.supported)
        self.assertEqual(result.data["path"], target)


class TestOpenUrl(TempDirTestCase):
    def test_open_url_is_suppressed_off_device(self) -> None:
        """The suite sets PYTO_HARNESS_NO_BROWSER so no browser can be spawned."""
        result = ios.open_url("https://example.com")
        self.assertFalse(result.ok)
        self.assertEqual(result.method, "suppressed")
        self.assertIn("https://example.com", result.detail)

    def test_empty_url(self) -> None:
        result = ios.open_url("")
        self.assertFalse(result.ok)
        self.assertIn("empty url", result.detail)

    def test_pyto_open_url_is_preferred(self) -> None:
        module = self.install_bridge("pyto")
        module.open_url = module.record
        result = ios.open_url("shortcuts://run-shortcut?name=Test")
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "pyto.open_url")
        self.assertEqual(module.calls[0][0], ("shortcuts://run-shortcut?name=Test",))


class TestShortcutUrls(unittest.TestCase):
    def test_run_shortcut_url(self) -> None:
        url = ios.shortcut_url("Morning Routine")
        self.assertEqual(url, "shortcuts://run-shortcut?name=Morning+Routine")

    def test_input_is_passed_as_text(self) -> None:
        url = ios.shortcut_url("Summarise", "hello world")
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["name"], ["Summarise"])
        self.assertEqual(query["input"], ["text"])
        self.assertEqual(query["text"], ["hello world"])

    def test_values_are_url_encoded(self) -> None:
        url = ios.shortcut_url("A&B", "x=1&y=2")
        self.assertIn("A%26B", url)
        self.assertIn("x%3D1%26y%3D2", url)

    def test_x_callback_url_carries_the_callback(self) -> None:
        url = ios.shortcut_wait_url("Do Thing", "input", callback="pyto://")
        self.assertTrue(url.startswith("shortcuts://x-callback-url/run-shortcut?"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["x-success"], ["pyto://"])

    def test_pyto_run_url(self) -> None:
        url = ios.pyto_run_url("/tmp/run.py", {"task": "hi"})
        self.assertTrue(url.startswith("pyto://python/"))
        self.assertIn("task=hi", url)

    def test_shortcut_run_needs_a_name(self) -> None:
        result = ios.shortcut_run("   ")
        self.assertFalse(result.ok)
        self.assertIn("name is required", result.detail)

    def test_shortcut_run_reports_the_url_it_used(self) -> None:
        result = ios.shortcut_run("My Shortcut", "payload")
        self.assertIn("url", result.data)
        self.assertIn("My+Shortcut", result.data["url"])
        self.assertFalse(result.ok, "URL opening is suppressed in the test environment")

    def test_shortcut_run_wait_reports_the_callback(self) -> None:
        result = ios.shortcut_run_wait("My Shortcut", "payload")
        self.assertEqual(result.data["callback"], "pyto://")
        self.assertIn("x-callback-url", result.data["url"])


class TestNotificationsAndSpeech(TempDirTestCase):
    def test_notify_degrades(self) -> None:
        result = ios.notify("Done", "the job finished")
        self.assertFalse(result.supported)
        self.assertIn("Done", result.detail)

    def test_notify_with_a_bridge(self) -> None:
        module = self.install_bridge("notifications")
        module.send = module.record
        result = ios.notify("Title", "Body")
        self.assertTrue(result.ok)
        self.assertTrue(result.supported)

    def test_speak_degrades(self) -> None:
        result = ios.speak("read this aloud")
        self.assertFalse(result.supported)
        self.assertIn("read this aloud", result.detail)

    def test_speak_with_a_bridge(self) -> None:
        module = self.install_bridge("speech")
        module.say = module.record
        result = ios.speak("hello")
        self.assertTrue(result.ok)
        self.assertEqual(module.calls[0][0], ("hello",))

    def test_speak_uses_documented_avfoundation_bridge_when_pyto_wrapper_is_missing(self) -> None:
        calls = []

        class Utterance:
            @classmethod
            def speechUtteranceWithString_(cls, text):
                calls.append(("create", text))
                return cls()

            def setRate_(self, rate):
                calls.append(("rate", rate))

            def setVoice_(self, voice):
                calls.append(("voice", voice))

        class Synthesizer:
            @classmethod
            def new(cls):
                return cls()

            def speakUtterance_(self, utterance):
                calls.append(("speak", utterance))

        class Voice:
            @staticmethod
            def voiceWithLanguage_(language):
                calls.append(("language", language))
                return "voice:{}".format(language)

        framework = types.ModuleType("AVFoundation")
        framework.AVSpeechSynthesizer = Synthesizer
        framework.AVSpeechUtterance = Utterance
        framework.AVSpeechSynthesisVoice = Voice
        with mock.patch.dict(sys.modules, {"speech": None, "AVFoundation": framework}):
            result = ios.speak("hello", language="en-AU", rate=0.4)

        self.assertTrue(result.ok)
        self.assertTrue(result.supported)
        self.assertEqual(result.method, "AVSpeechSynthesizer")
        self.assertEqual([item[0] for item in calls], ["create", "rate", "language", "voice", "speak"])


class TestPhotos(TempDirTestCase):
    def test_missing_file(self) -> None:
        result = ios.save_photo(self.path("nope.png"))
        self.assertFalse(result.ok)
        self.assertIn("no such image", result.detail)

    def test_non_image_extension(self) -> None:
        target = self.path("note.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        result = ios.save_photo(target)
        self.assertFalse(result.ok)
        self.assertIn("image extension", result.detail)

    def test_valid_image_degrades_without_the_bridge(self) -> None:
        target = self.path("shot.png")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        result = ios.save_photo(target)
        self.assertFalse(result.supported)
        self.assertIn(target, result.detail)

    def test_valid_image_with_a_bridge(self) -> None:
        target = self.path("shot.png")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        module = self.install_bridge("photos")
        module.save_image = module.record
        result = ios.save_photo(target)
        self.assertTrue(result.ok)
        self.assertTrue(result.supported)
        self.assertEqual(module.calls[0][0], (target,))


class TestUnexpandableTilde(TempDirTestCase):
    """Pyto cannot expand ``~``; a user path must fail cleanly, never become a '~' dir."""

    def broken_expanduser(self):
        real = os.path.expanduser

        def broken(path):
            return path if str(path).startswith("~") else real(path)

        return mock.patch.object(ios.os.path, "expanduser", side_effect=broken)

    def test_save_photo_reports_it_instead_of_crashing(self) -> None:
        with self.broken_expanduser():
            result = ios.save_photo("~/Pictures/shot.png")
        self.assertFalse(result.ok)
        self.assertFalse(result.supported)
        self.assertIn("~", result.detail)
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "~")))

    def test_open_in_files_reports_it_instead_of_crashing(self) -> None:
        with self.broken_expanduser():
            result = ios.open_in_files("~/Documents")
        self.assertFalse(result.ok)
        self.assertIn("~", result.detail)

    def test_pyto_run_url_refuses_an_unexpandable_path(self) -> None:
        with self.broken_expanduser():
            with self.assertRaises(ConfigError):
                ios.pyto_run_url("~/run.py")

    def test_an_absolute_path_is_untouched(self) -> None:
        with self.broken_expanduser():
            result = ios.save_photo(self.path("nope.png"))
        self.assertIn(self.path("nope.png"), result.detail)


class TestCalendar(TempDirTestCase):
    def test_add_event_degrades(self) -> None:
        result = ios.calendar_add_event("Standup", "2024-05-01T09:00")
        self.assertFalse(result.supported)
        self.assertIn("calendar_events", result.detail)
        self.assertEqual(result.data["title"], "Standup")

    def test_bad_timestamp_is_rejected(self) -> None:
        result = ios.calendar_add_event("X", "next tuesday")
        self.assertFalse(result.ok)
        self.assertIn("ISO", result.detail)

    def test_iso_parsing_variants(self) -> None:
        self.assertIsNotNone(ios._iso_to_epoch("2024-05-01T09:00"))
        self.assertIsNotNone(ios._iso_to_epoch("2024-05-01 09:00"))
        self.assertIsNotNone(ios._iso_to_epoch("2024-05-01"))
        self.assertIsNotNone(ios._iso_to_epoch("2024-05-01T09:00:00Z"))
        self.assertIsNone(ios._iso_to_epoch("tomorrow"))
        self.assertIsNone(ios._iso_to_epoch(""))

    def test_add_event_with_a_bridge(self) -> None:
        module = self.install_bridge("calendar_events")

        def save_event(**kwargs):
            module.calls.append((tuple(), kwargs))

        module.save_event = save_event
        result = ios.calendar_add_event("Dentist", "2024-05-01T09:00", "2024-05-01T10:00", notes="bring card")
        self.assertTrue(result.ok)
        self.assertTrue(result.supported)
        self.assertEqual(module.calls[0][1]["title"], "Dentist")
        self.assertEqual(module.calls[0][1]["notes"], "bring card")

    def test_add_event_tolerates_a_positional_signature(self) -> None:
        module = self.install_bridge("calendar_events")

        def save_event(title, start, end, notes=None, calendar=None):
            module.calls.append((title, start, end))

        module.save_event = save_event
        result = ios.calendar_add_event("Lunch", "2024-05-01T12:00")
        self.assertTrue(result.ok)
        self.assertEqual(module.calls[0][0], "Lunch")

    def test_list_events_without_a_reader(self) -> None:
        self.install_bridge("calendar_events")
        result = ios.calendar_list_events(7)
        self.assertFalse(result.ok)
        self.assertIn("no reader", result.detail)

    def test_list_events_with_a_reader(self) -> None:
        module = self.install_bridge("calendar_events")
        module.get_events = lambda days=7: [{"title": "Sync", "start": 1, "end": 2}]
        result = ios.calendar_list_events(3)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["events"][0]["title"], "Sync")


class TestKeepalive(TempDirTestCase):
    def test_start_degrades(self) -> None:
        result = ios.keepalive_start("job")
        self.assertFalse(result.supported)
        self.assertIn("background", result.detail)

    def test_start_and_stop_with_a_bridge(self) -> None:
        module = self.install_bridge("background")
        task = types.SimpleNamespace()
        task.started = False
        task.stopped = False
        task.start = lambda: setattr(task, "started", True)
        task.stop = lambda: setattr(task, "stopped", True)
        module.BackgroundTask = lambda **kwargs: task
        started = ios.keepalive_start("long job")
        self.assertTrue(started.ok)
        self.assertTrue(task.started)
        self.assertTrue(ios._KEEPALIVE.get("label") == "long job")
        stopped = ios.keepalive_stop()
        self.assertTrue(stopped.ok)
        self.assertTrue(task.stopped)

    def test_stop_without_a_task(self) -> None:
        ios._KEEPALIVE.clear()
        result = ios.keepalive_stop()
        self.assertTrue(result.ok)
        self.assertIn("no background task", result.detail)


class TestMemory(unittest.TestCase):
    def test_memory_status_always_returns_a_result(self) -> None:
        result = ios.memory_status()
        self.assertIn("memory_status", result.render())
        self.assertEqual(result.action, "memory_status")

    def test_available_memory_reads_the_os_hook(self) -> None:
        original = getattr(sys.modules["os"], "os_proc_available_memory", None)
        setattr(sys.modules["os"], "os_proc_available_memory", lambda: 900 * 1024 * 1024)
        try:
            result = ios.memory_status()
        finally:
            if original is None:
                delattr(sys.modules["os"], "os_proc_available_memory")
            else:
                setattr(sys.modules["os"], "os_proc_available_memory", original)
        self.assertTrue(result.supported)
        self.assertIn("MB free", result.detail)

    def test_low_memory_is_flagged(self) -> None:
        original = getattr(sys.modules["os"], "os_proc_available_memory", None)
        setattr(sys.modules["os"], "os_proc_available_memory", lambda: 400 * 1024 * 1024)
        try:
            result = ios.memory_status()
        finally:
            if original is None:
                delattr(sys.modules["os"], "os_proc_available_memory")
            else:
                setattr(sys.modules["os"], "os_proc_available_memory", original)
        self.assertIn("critical", result.detail)


class TestRecord(TempDirTestCase):
    def test_every_attempt_is_recorded(self) -> None:
        ios.clear_record()
        ios.clipboard_get()
        ios.clipboard_set("x")
        ios.notify("t")
        self.assertGreaterEqual(len(ios.RECORD), 3)
        self.assertTrue(all(entry.action for entry in ios.RECORD))

    def test_record_is_bounded(self) -> None:
        ios.clear_record()
        for index in range(260):
            ios.record(ios.CapabilityResult(action="noise", detail=str(index)))
        self.assertLessEqual(len(ios.RECORD), 200)

    def test_render_is_one_line(self) -> None:
        result = ios.CapabilityResult(action="x", ok=True, supported=True, method="m", detail="d")
        self.assertNotIn("\n", result.render())


if __name__ == "__main__":
    unittest.main()

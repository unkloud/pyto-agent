"""Tests for Pyto library grounding: the catalogue, introspection, the tool, hints, the doctor.

Everything here is offline: the Pyto modules are fakes injected into ``sys.modules``, which is
exactly the seam the harness needs (device truth = whatever ``dir()``/``inspect`` see).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.machinery
import json
import os
import sys
import types
import unittest
from typing import Any, Dict, List
from unittest import mock

from harness import doctor, ios, pyto_api
from harness.loop import AUTO_APPROVED_TOOLS, APPROVAL_REQUIRED_TOOLS, build_system_prompt
from harness.tools_ios import MAX_PYTO_REFERENCE_CHARS

from .mock_provider import MockProvider, tool_response
from .support import ROOT, TempDirTestCase, make_client, make_options


def fake_module(name: str, **members: Any) -> types.ModuleType:
    """A stand-in for an importable Pyto module, with a real spec like an import would set."""
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    for key, value in members.items():
        setattr(module, key, value)
    return module


class PytoFakeTestCase(TempDirTestCase):
    """A temp-dir test that can install fake Pyto modules and clean the probe memo."""

    def setUp(self) -> None:
        super().setUp()
        pyto_api.clear_memo()
        self.addCleanup(pyto_api.clear_memo)

    def install(self, name: str, **members: Any) -> types.ModuleType:
        """Install a fake Pyto module.

        A member defined in a real Pyto module carries that module's ``__module__``; the
        probe uses that to ignore imported helpers (``from Foundation import NSURL``), so
        the fakes are stamped the same way.
        """
        module = fake_module(name, **members)
        for key, value in members.items():
            if callable(value) and getattr(value, "__module__", None) not in (None, name):
                try:
                    value.__module__ = name
                except (AttributeError, TypeError):  # pragma: no cover - builtins
                    pass
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        pyto_api.clear_memo()
        return module


# --------------------------------------------------------------------------------------
# 1. Catalogue sanity
# --------------------------------------------------------------------------------------


class TestCatalogue(unittest.TestCase):
    REQUIRED = {
        "pyto_ui",
        "pyto",
        "pasteboard",
        "sharing",
        "share",
        "notifications",
        "usernotification",
        "photos",
        "speech",
        "sound",
        "music",
        "location",
        "motion",
        "background",
        "xcallback",
        "apps",
        "calendar_events",
        "widgets",
        "watch",
        "multipeer",
        "console",
        "file_system",
        "userkeys",
        "_extensionsimporter",
    }

    def test_every_module_has_members_and_every_member_a_description(self) -> None:
        for name in pyto_api.module_names():
            entry = pyto_api.CURATED[name]
            self.assertTrue(entry["members"], "{} has no members".format(name))
            self.assertTrue(entry.get("purpose"), "{} has no purpose".format(name))
            self.assertTrue(entry.get("snippet"), "{} has no snippet".format(name))
            self.assertTrue(entry.get("sources"), "{} cites no source".format(name))
            for member in entry["members"]:
                self.assertTrue(member.get("name"), name)
                self.assertTrue(member.get("signature"), member.get("name"))
                self.assertTrue(
                    member.get("description"),
                    "{}.{} has no description".format(name, member.get("name")),
                )
                self.assertIsInstance(member.get("verified"), bool, member.get("name"))

    def test_snippets_parse_as_python_310(self) -> None:
        for name in pyto_api.module_names():
            entry = pyto_api.CURATED[name]
            ast.parse(entry["snippet"], feature_version=(3, 10))
            for member in entry["members"]:
                if member.get("snippet"):
                    ast.parse(member["snippet"], feature_version=(3, 10))

    def test_no_curated_member_name_is_duplicated(self) -> None:
        for name in pyto_api.module_names():
            seen = set()
            for member in pyto_api.CURATED[name]["members"]:
                self.assertNotIn(
                    member["name"],
                    seen,
                    "{}: {} appears twice".format(name, member["name"]),
                )
                seen.add(member["name"])

    def test_module_order_has_no_duplicates_and_covers_the_catalogue(self) -> None:
        self.assertEqual(len(pyto_api._MODULE_ORDER), len(set(pyto_api._MODULE_ORDER)))
        self.assertEqual(set(pyto_api._MODULE_ORDER), set(pyto_api.CURATED))

    def test_the_required_modules_are_covered(self) -> None:
        self.assertEqual(self.REQUIRED - set(pyto_api.module_names()), set())

    def test_not_available_list_is_present_and_explains_itself(self) -> None:
        self.assertGreaterEqual(len(pyto_api.NOT_AVAILABLE), 8)
        text = " ".join(item["name"] for item in pyto_api.NOT_AVAILABLE).lower()
        for wanted in (
            "reminders",
            "healthkit",
            "bluetooth",
            "speech recognition",
            "pip install",
            "daemon",
            "pty",
            "subprocess",
            "git",
            "ffmpeg",
            "wget",
            "make",
        ):
            self.assertIn(wanted, text, "NOT_AVAILABLE does not mention {}".format(wanted))
        for item in pyto_api.NOT_AVAILABLE:
            self.assertTrue(item["reason"], item)
            self.assertTrue(item["instead"], item)
        self.assertIn("Reminders", pyto_api.not_available_text())

    def test_userkeys_is_flagged_as_not_secure_storage(self) -> None:
        caveats = " ".join(pyto_api.CURATED["userkeys"]["caveats"]).lower()
        self.assertIn("not secure storage", caveats)

    def test_clipboard_is_flagged_as_foreground_only(self) -> None:
        caveats = " ".join(pyto_api.CURATED["pasteboard"]["caveats"]).lower()
        self.assertIn("foreground", caveats)
        background = " ".join(pyto_api.CURATED["background"]["caveats"]).lower()
        self.assertIn("grey area", background)


# --------------------------------------------------------------------------------------
# 2. Introspection with injected fake modules
# --------------------------------------------------------------------------------------


class TestIntrospection(PytoFakeTestCase):
    def test_device_truth_overrides_a_wrong_curated_signature(self) -> None:
        def set_string(text: str, style: str = None) -> None:  # noqa: ANN001 - fake API
            """Copy text, but with an extra parameter that only this device has."""

        self.install("pasteboard", set_string=set_string)
        info = pyto_api.introspect("pasteboard")
        self.assertTrue(info["available"])
        member = self._member(info, "set_string")
        self.assertIn("style", member["signature"], "the device signature must win")
        self.assertNotEqual(member["signature"], "set_string(text)")
        self.assertEqual(member["signature_source"], "device")
        self.assertEqual(member["catalogue_signature"], "set_string(text)")
        self.assertEqual(member["kind"], "function")
        self.assertTrue(member["on_device"])

    def test_unknown_device_members_are_reported(self) -> None:
        def set_string(text: str) -> None:
            """Copy the text."""

        def brand_new_api(flag: bool = False) -> str:
            """An API the catalogue has never heard of."""

        self.install("pasteboard", set_string=set_string, brand_new_api=brand_new_api)
        info = pyto_api.introspect("pasteboard")
        extra = self._member(info, "brand_new_api")
        self.assertFalse(extra["curated"])
        self.assertTrue(extra["on_device"])
        self.assertIn("API the catalogue has never heard of", extra["description"])
        self.assertIn("brand_new_api", pyto_api.render_reference("pasteboard"))

    def test_a_curated_member_missing_on_device_is_marked_missing(self) -> None:
        def set_string(text: str) -> None:
            """Copy the text."""

        self.install("pasteboard", set_string=set_string)
        info = pyto_api.introspect("pasteboard")
        self.assertFalse(self._member(info, "url")["on_device"])
        rendered = pyto_api.render_reference("pasteboard")
        self.assertIn("NOT on this device", rendered)
        self.assertIn("url", rendered)

    def test_a_missing_module_degrades_with_a_clear_message(self) -> None:
        info = pyto_api.introspect("photos")
        self.assertFalse(info["available"])
        self.assertTrue(info["known"])
        self.assertIn("ModuleNotFoundError", info["error"])
        self.assertTrue(info["members"], "the catalogue is still shown when the import fails")
        for member in info["members"]:
            self.assertIsNone(member["on_device"])
        rendered = pyto_api.render_reference("photos")
        self.assertIn("NOT importable on this device", rendered)
        self.assertIn("save_image", rendered)

    def test_an_unknown_module_says_so(self) -> None:
        info = pyto_api.introspect("definitely_not_pyto")
        self.assertFalse(info["available"])
        self.assertFalse(info["known"])
        rendered = pyto_api.render_reference("definitely_not_pyto")
        self.assertIn("unknown module", rendered)
        self.assertIn("Known Pyto modules", rendered)

    def test_module_member_names_prefers_the_device(self) -> None:
        def set_string(text: str) -> None:
            """Copy the text."""

        self.install("pasteboard", set_string=set_string)
        names = pyto_api.module_member_names("pasteboard")
        self.assertIn("set_string", names)
        self.assertNotIn("url", names, "a member the device lacks must not be suggested")

    def test_cache_extends_the_doctor_file_without_breaking_it(self) -> None:
        state = self.path("state")
        os.makedirs(state, exist_ok=True)
        path = pyto_api.capabilities_path(state)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "signatures": {"share.open": {"exists": True}}}, handle)

        def set_string(text: str) -> None:
            """Copy the text."""

        self.install("pasteboard", set_string=set_string)
        info = pyto_api.introspect("pasteboard", state_dir=state, persist=True)
        self.assertTrue(info["available"])
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["signatures"], {"share.open": {"exists": True}})
        self.assertIn("pasteboard", payload[pyto_api.CACHE_KEY]["modules"])
        cached = pyto_api.load_cache(state)
        self.assertTrue(cached["modules"]["pasteboard"]["present"])

    def test_a_stale_cache_never_claims_a_module_is_available(self) -> None:
        state = self.path("state")
        os.makedirs(state, exist_ok=True)
        path = pyto_api.capabilities_path(state)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    pyto_api.CACHE_KEY: {
                        "version": 1,
                        "created_at": 1,
                        "modules": {
                            "photos": {
                                "present": True,
                                "checked_at": 1,
                                "members": {"save_image": {"signature": "save_image(image)"}},
                            }
                        },
                    }
                },
                handle,
            )
        info = pyto_api.introspect("photos", state_dir=state)
        self.assertFalse(info["available"], "a cached probe must never fake availability")
        self.assertTrue(info["stale"])
        self.assertTrue(self._member(info, "save_image")["stale"])

    def _member(self, info: Dict[str, Any], name: str) -> Dict[str, Any]:
        for member in info["members"]:
            if member["name"] == name:
                return member
        self.fail("no member {!r} in {}".format(name, [m["name"] for m in info["members"]]))


# --------------------------------------------------------------------------------------
# 3. The pyto_api tool
# --------------------------------------------------------------------------------------


class TestPytoApiTool(PytoFakeTestCase):
    def call(self, **args: Any) -> Any:
        registry = self.make_registry()
        return registry._tools["pyto_api"].handler(**args)

    def test_the_tool_is_registered_read_only_and_auto_approved(self) -> None:
        registry = self.make_registry()
        self.assertIn("pyto_api", registry.names())
        self.assertIn("pyto_api", AUTO_APPROVED_TOOLS)
        self.assertNotIn("pyto_api", APPROVAL_REQUIRED_TOOLS)

    def test_index_mode_lists_modules_and_what_pyto_lacks(self) -> None:
        result = self.call()
        self.assertFalse(result.is_error)
        self.assertIn("Pyto library reference", result.content)
        self.assertIn("pyto_ui", result.content)
        self.assertIn("NOT importable here", result.content)
        self.assertIn("Reminders", result.content)
        self.assertIn("pip install of C extensions", result.content)

    def test_module_mode_shows_members_signatures_and_caveats(self) -> None:
        def set_string(text: str) -> None:
            """Copy the given text to the pasteboard."""

        self.install("pasteboard", set_string=set_string)
        result = self.call(module="pasteboard")
        self.assertIn("set_string", result.content)
        self.assertIn("foreground", result.content)
        self.assertIn("import pasteboard", result.content)
        self.assertEqual(result.metadata["module"], "pasteboard")
        self.assertFalse(result.metadata["capped"])

    def test_member_mode_narrows_to_one_member(self) -> None:
        def set_string(text: str) -> None:
            """Copy the given text to the pasteboard."""

        self.install("pasteboard", set_string=set_string)
        result = self.call(module="pasteboard", member="set_string")
        self.assertIn("set_string", result.content)
        self.assertNotIn("item_provider", result.content)

    def test_unknown_module_is_reported_not_crashed(self) -> None:
        result = self.call(module="nonsense_module")
        self.assertFalse(result.is_error)
        self.assertIn("unknown module", result.content)

    def test_required_pyto_module_that_is_absent_here_explains_itself(self) -> None:
        result = self.call(module="reminders")
        self.assertIn("Reminders", result.content)
        self.assertIn("Instead:", result.content)

    def test_the_answer_is_capped(self) -> None:
        for name in ("pasteboard", "photos", "pyto_ui"):
            result = self.call(module=name)
            self.assertLessEqual(len(result.content), MAX_PYTO_REFERENCE_CHARS, name)
        tiny = pyto_api.render_reference("pyto_ui", max_chars=600)
        self.assertLessEqual(len(tiny), 600)
        self.assertIn("[truncated:", tiny)

    def test_off_device_every_module_degrades_without_an_exception(self) -> None:
        for name in pyto_api.module_names():
            result = self.call(module=name)
            self.assertFalse(result.is_error, name)
            self.assertTrue(result.content.strip(), name)
        self.assertFalse(pyto_api.available_modules(), "no Pyto module should import in this test")

    def test_the_tool_policy_allows_it_without_prompting(self) -> None:
        from harness.loop import make_policy

        policy = make_policy()
        self.assertTrue(policy("pyto_api", {"module": "pasteboard"}).allowed)


# --------------------------------------------------------------------------------------
# 4. run_program error hints
# --------------------------------------------------------------------------------------


class TestHints(PytoFakeTestCase):
    def test_attribute_error_hint_names_the_closest_real_member(self) -> None:
        def set_string(text: str) -> None:
            """Copy the given text to the pasteboard."""

        self.install("pasteboard", set_string=set_string)
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "clip.py", line 2, in <module>\n'
            "    pasteboard.save_image(\"x\")\n"
            "AttributeError: module 'pasteboard' has no attribute 'save_image'"
        )
        hint = pyto_api.hint_for_stderr(stderr)
        self.assertIn("pasteboard.save_image", hint)
        self.assertIn("set_string", hint)
        self.assertIn('pyto_api(module="pasteboard")', hint)

    def test_cannot_import_name_hint(self) -> None:
        def save_image(image: Any) -> None:
            """Save a PIL image to the photo library."""

        self.install("photos", save_image=save_image)
        stderr = "ImportError: cannot import name 'save_photo' from 'photos' (/private/photos.py)"
        hint = pyto_api.hint_for_stderr(stderr)
        self.assertIn("photos.save_photo", hint)
        self.assertIn("save_image", hint)

    def test_no_module_named_hint_for_a_known_absent_module(self) -> None:
        hint = pyto_api.hint_for_stderr("ModuleNotFoundError: No module named 'reminders'")
        self.assertIn("Reminders", hint)
        self.assertIn("instead", hint)

    def test_no_module_named_hint_for_a_pyto_module_missing_here(self) -> None:
        hint = pyto_api.hint_for_stderr("ModuleNotFoundError: No module named 'pasteboard'")
        self.assertIn("not importable on this device", hint)
        self.assertIn('pyto_api(module="pasteboard")', hint)

    def test_an_unrelated_traceback_produces_no_hint(self) -> None:
        for stderr in (
            "ZeroDivisionError: division by zero",
            "AttributeError: module 'os' has no attribute 'save_file'",
            "ImportError: cannot import name 'missing' from 'json'",
            "ModuleNotFoundError: No module named 'nonsense_xyz'",
            "",
        ):
            self.assertEqual(pyto_api.hint_for_stderr(stderr), "", stderr)

    def test_parse_api_error_classifies_the_three_shapes(self) -> None:
        self.assertEqual(
            pyto_api.parse_api_error("AttributeError: module 'pasteboard' has no attribute 'set_clipboard'"),
            ("attribute", "pasteboard", "set_clipboard"),
        )
        self.assertEqual(
            pyto_api.parse_api_error("ImportError: cannot import name 'save_photo' from 'photos'"),
            ("import", "photos", "save_photo"),
        )
        self.assertEqual(
            pyto_api.parse_api_error("ModuleNotFoundError: No module named 'reminders'"),
            ("missing_module", "reminders", ""),
        )

    def test_run_program_appends_the_hint_without_rewriting_stderr(self) -> None:
        def set_string(text: str) -> None:
            """Copy the given text to the pasteboard."""

        self.install("pasteboard", set_string=set_string)
        registry = self.make_registry()
        program = "import pasteboard\npasteboard.set_clipboard('hi')\nprint('never')\n"
        with mock.patch.object(ios, "has_fake_subprocess", return_value=True):
            result = registry._tools["run_program"].handler(path_or_source=program)
        self.assertTrue(result.is_error)
        self.assertIn("AttributeError", result.content, "the original traceback is kept")
        self.assertIn("--- Pyto API hint ---", result.content)
        self.assertIn("set_string", result.content)
        self.assertTrue(result.metadata["pyto_hint"])

    def test_a_clean_run_has_no_hint(self) -> None:
        registry = self.make_registry()
        result = registry._tools["run_program"].handler(path_or_source="print('fine')\n")
        self.assertFalse(result.is_error)
        self.assertNotIn("Pyto API hint", result.content)
        self.assertFalse(result.metadata["pyto_hint"])


# --------------------------------------------------------------------------------------
# 5. Doctor: the libs_reference check and the libs.write_doc fix
# --------------------------------------------------------------------------------------


class TestDoctorLibsReference(PytoFakeTestCase):
    def make_ctx(self, **overrides: Any) -> doctor.DoctorContext:
        values: Dict[str, Any] = {
            "env": {},
            "config_path": self.path("config.json"),
            "root": ROOT,
            "state": self.path("state"),
            "workspace": self.path("workspace"),
            "sessions_dir": self.path("sessions"),
            "network": False,
            "persist": True,
        }
        values.update(overrides)
        return doctor.DoctorContext.for_config(self.make_config(), **values)

    def test_missing_doc_warns_and_is_fixable(self) -> None:
        item = doctor.run_one(self.make_ctx(), "libs_reference")
        self.assertEqual(item.status, "warn")
        self.assertTrue(item.fixable)
        self.assertEqual(item.fix_id, "libs.write_doc")

    def test_the_fix_writes_the_doc_and_the_check_then_passes(self) -> None:
        ctx = self.make_ctx()
        item = doctor.run_one(ctx, "libs_reference")
        outcome = doctor.apply_fix(ctx, "libs.write_doc", item)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(os.path.exists(ctx.libs_doc))
        with open(ctx.libs_doc, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Not available in Pyto (do not try)", text)
        self.assertIn("Reminders", text)
        self.assertIn("pip install of C extensions", text)
        self.assertIn("pasteboard", text)
        self.assertIn("pyto_api", text)
        again = doctor.run_one(ctx, "libs_reference")
        self.assertEqual(again.status, "ok")
        self.assertFalse(again.fixable)

    def test_a_hand_edited_doc_is_reported_stale(self) -> None:
        ctx = self.make_ctx()
        outcome = doctor.apply_fix(ctx, "libs.write_doc", doctor.run_one(ctx, "libs_reference"))
        self.assertTrue(outcome.ok, outcome.error)
        with open(ctx.libs_doc, "a", encoding="utf-8") as handle:
            handle.write("\n- a module that does not exist\n")
        item = doctor.run_one(ctx, "libs_reference")
        self.assertEqual(item.status, "warn")
        self.assertIn("stale", item.detail)

    def test_the_check_is_registered_with_a_title_and_a_fix(self) -> None:
        self.assertIn("libs_reference", doctor.CHECK_FUNCTIONS)
        self.assertIn("libs_reference", doctor.CHECK_TITLES)
        self.assertIn("libs.write_doc", doctor.FIXES)
        self.assertEqual(doctor.FIXES["libs.write_doc"].check_id, "libs_reference")

    def test_the_doctor_preserves_the_catalogue_cache_it_shares(self) -> None:
        ctx = self.make_ctx()
        path = ctx.capabilities_path
        os.makedirs(ctx.state, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({pyto_api.CACHE_KEY: {"version": 1, "modules": {"pasteboard": {"present": True}}}}, handle)
        doctor.apply_fix(ctx, "libs.write_doc", doctor.run_one(ctx, "libs_reference"))
        doctor.run_one(ctx, "ios_signatures")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIn("signatures", payload, "the doctor still writes its own discovery")
        self.assertIn(
            pyto_api.CACHE_KEY,
            payload,
            "the doctor must not erase the pyto_api cache it shares the file with",
        )


# --------------------------------------------------------------------------------------
# 6. The system prompt
# --------------------------------------------------------------------------------------


class TestSystemPrompt(TempDirTestCase):
    def render(self) -> str:
        return build_system_prompt(self.make_config(), self.workspace_dir)

    def test_the_prompt_names_the_pyto_modules(self) -> None:
        prompt = self.render()
        for name in ("pasteboard", "photos", "notifications", "pyto_ui", "calendar_events", "background"):
            self.assertIn(name, prompt, "the prompt does not mention {}".format(name))
        self.assertIn("Pyto modules reported by the local reference", prompt)
        self.assertIn("pip install", prompt)

    def test_the_prompt_carries_the_look_it_up_rule(self) -> None:
        prompt = self.render()
        self.assertIn("call `pyto_api`", prompt)
        self.assertIn("do not guess", prompt.lower())
        self.assertIn("PYTO_LIBS.md", prompt)
        self.assertIn("AttributeError", prompt)
        self.assertIn("ImportError", prompt)

    def test_the_old_false_sentence_is_gone(self) -> None:
        prompt = self.render()
        self.assertNotIn("Only the Python standard library is installed", prompt)
        self.assertNotIn("Do not import anything else", prompt)

    def test_unverified_legacy_names_are_not_promised_by_the_prompt(self) -> None:
        prompt = self.render()
        listed = [
            line
            for line in prompt.splitlines()
            if line.startswith("- Pyto modules reported by the local reference:")
        ]
        self.assertEqual(len(listed), 1, "the module list line is missing")
        self.assertNotIn("usernotification", listed[0])
        self.assertNotIn("share,", listed[0])
        for name in pyto_api.prompt_module_names():
            self.assertIn(name, listed[0], name)
        for internal in ("pyto,", "_extensionsimporter"):
            self.assertNotIn(internal, listed[0], "internal modules must not be promised")


# --------------------------------------------------------------------------------------
# 7. End to end: the model writes a program against a fake Pyto module
# --------------------------------------------------------------------------------------


class TestEndToEnd(PytoFakeTestCase):
    def install_pasteboard(self) -> None:
        calls: List[str] = []

        def set_string(text: str) -> None:
            """Copy the given text to the pasteboard."""
            calls.append(text)

        module = self.install("pasteboard", set_string=set_string)
        module.calls = calls  # type: ignore[attr-defined]

    def drive(self, script: List[Dict[str, Any]], task: str) -> Any:
        """Run one turn against the mock provider and return the tool results in order."""
        results: List[Any] = []
        with MockProvider(script) as provider:
            client = make_client(provider)
            registry = self.make_registry()
            session = self.make_session()
            options = make_options(client, registry, session, max_turns=4)

            async def go() -> None:
                from harness.loop import run_turn

                async for event in run_turn(options, task):
                    if event.kind == "tool.completed":
                        results.append(event.data)

            with mock.patch.object(ios, "has_fake_subprocess", return_value=True):
                asyncio.run(go())
            client.close()
        return results

    def test_a_documented_member_runs_clean(self) -> None:
        self.install_pasteboard()
        program = "import pasteboard\n\npasteboard.set_string(\"hi\")\nprint(\"copied 2 characters\")\n"
        script = [
            tool_response(
                ("pyto_api", {"module": "pasteboard"}),
                ("write_program", {"path": "clip.py", "source": program, "purpose": "copy to clipboard"}),
            ),
            tool_response(
                ("run_program", {"path_or_source": "clip.py"}),
                ("finish", {"message": "Copied to the clipboard."}),
            ),
        ]
        results = self.drive(script, "copy 'hi' to my clipboard")
        self.assertTrue(results, "the turn produced no tool results")
        self.assertEqual([r.get("name") for r in results], ["pyto_api", "write_program", "run_program", "finish"])
        lookup, written, ran = results[0], results[1], results[2]
        self.assertIn("set_string", lookup["content"])
        self.assertIn("clip.py", written["content"])
        self.assertEqual(ran["metadata"]["returncode"], 0, ran["content"])
        self.assertIn("copied 2 characters", ran["content"])
        print("\n--- e2e transcript 1: documented member ---")
        print(lookup["content"][:400])
        print(written["content"])
        print(ran["content"])

    def test_a_wrong_member_gets_the_hint_naming_the_right_one(self) -> None:
        self.install_pasteboard()
        program = "import pasteboard\n\npasteboard.set_clipboard(\"hi\")\nprint(\"never reached\")\n"
        script = [
            tool_response(
                ("write_program", {"path": "bad.py", "source": program, "purpose": "guessed API"}),
            ),
            tool_response(
                ("run_program", {"path_or_source": "bad.py"}),
                ("finish", {"message": "The name was wrong; pyto_api says set_string."}),
            ),
        ]
        results = self.drive(script, "copy 'hi' to my clipboard")
        self.assertEqual([r.get("name") for r in results], ["write_program", "run_program", "finish"])
        ran = results[1]
        self.assertEqual(ran["metadata"]["returncode"], 1)
        self.assertIn("AttributeError", ran["content"])
        self.assertIn("--- Pyto API hint ---", ran["content"])
        self.assertIn("set_string", ran["content"])
        self.assertNotIn("never reached", ran["content"].split("--- stdout ---")[1].split("--- stderr ---")[0])
        print("\n--- e2e transcript 2: wrong member ---")
        print(ran["content"])


if __name__ == "__main__":
    unittest.main()

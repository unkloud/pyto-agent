"""The home resolver: a writable, absolute ``~`` even where Pyto has none.

The bug these tests pin down was reported from a real iPhone: iOS refused to create the
harness's state directory because the path still carried a literal tilde and the folder
name began with a dot.  Pyto has no usable home directory, so
``os.path.expanduser("~")`` returns the string ``"~"`` and every derived path became a
*relative* path with a literal tilde.  The first write tried to create a directory
literally called ``~`` and iOS refused it.

The simulation used everywhere here is the honest one: ``os.path.expanduser`` is
monkeypatched to return its argument unchanged for a leading ``~`` — exactly what Pyto
effectively does — and the candidate directories are made genuinely unwritable with a
mode change, so the resolver's write probe really fails.  No device, no network.

:class:`TestLegacyStateMigration` is the only place in the tests that spells the old,
hidden folder name out in full: that is the spelling it exists to move.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from harness import home, ios
from harness.config import (
    Config,
    ConfigError,
    default_config_path,
    default_home,
    default_sessions_dir,
    default_spill_dir,
    default_state_dir,
    default_workspace,
    ensure_workspace,
)
from harness.session import SessionLog, new_session_path


def broken_expanduser(path, _real=os.path.expanduser):
    """Pyto's effective ``expanduser``: a leading ``~`` comes back unchanged."""
    if str(path).startswith("~"):
        return path
    return _real(path)


def no_tilde(path: str) -> bool:
    """True when no path segment starts with ``~`` (i.e. nothing was left unexpanded)."""
    return not any(part.startswith("~") for part in str(path).split(os.sep))


class HomeTestCase(unittest.TestCase):
    """A private temp tree, a reset resolver cache, and the original cwd restored."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="pyto-home-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home.reset_home_cache()
        self.addCleanup(home.reset_home_cache)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        # The checkout these tests run from must never become a candidate: the resolver's
        # new default is the folder *above* the install, and its pointer file is written
        # next to the entry point.  Pinning the entry points to nothing keeps every test
        # inside its own temp tree; the tests that exercise the install rules pin a fake
        # install with :meth:`pin_install` instead.
        self.entry_points = mock.patch.object(home, "entry_point_dirs", return_value=[])
        self.entry_points.start()
        self.addCleanup(self.entry_points.stop)

    # -- helpers -------------------------------------------------------------------

    def pin_install(self, *parts: str, make_run_py: bool = True) -> str:
        """Point the resolver at a fake install inside the temp tree.

        Writes ``run.py`` into it -- that is what makes a folder an install, and therefore
        what makes the folder *above* it the new default home.
        """
        install = self.path(*parts)
        os.makedirs(install, exist_ok=True)
        if make_run_py:
            with open(os.path.join(install, "run.py"), "w", encoding="utf-8") as handle:
                handle.write("# a fake entry point\n")
        patcher = mock.patch.object(
            home, "entry_point_dirs", return_value=[(install, "the folder that holds run.py")]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return install

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def make_dir(self, *parts: str, mode: int = 0o700) -> str:
        target = self.path(*parts)
        os.makedirs(target, exist_ok=True)
        os.chmod(target, mode)
        self.addCleanup(os.chmod, target, 0o700)
        return target

    def unwritable(self, *parts: str) -> str:
        """A directory that really refuses writes, checked before the test relies on it."""
        target = self.make_dir(*parts, mode=0o500)
        self.assertIsNotNone(
            home.writability_problem(target),
            "this test needs an unwritable directory; {} accepted a write".format(target),
        )
        return target

    def unset_env(self, **extra: str):
        """An override layer where the escape hatch and HOME are explicitly unset."""
        values = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        values.update(extra)
        return mock.patch.dict(os.environ, values)

    def patch_broken_expanduser(self):
        return mock.patch.object(home.os.path, "expanduser", side_effect=broken_expanduser)

    def assert_usable_home(self, resolved: str) -> None:
        self.assertTrue(os.path.isabs(resolved), "{} is not absolute".format(resolved))
        self.assertTrue(no_tilde(resolved), "{} still contains an unexpanded '~'".format(resolved))
        self.assertTrue(os.path.isdir(resolved), "{} does not exist".format(resolved))
        self.assertIsNone(home.writability_problem(resolved), "{} is not writable".format(resolved))
        probe = os.path.join(resolved, ".pyto-home-test-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)

    def install_fake_pyto(self):
        """A fake ``pyto`` module good enough for ``importlib.util.find_spec``."""
        module = types.ModuleType("pyto")
        module.__spec__ = importlib.machinery.ModuleSpec("pyto", loader=None)
        sys.modules["pyto"] = module
        self.addCleanup(sys.modules.pop, "pyto", None)
        return module


class TestBrokenExpanduser(HomeTestCase):
    def test_broken_expanduser_still_resolves_an_absolute_writable_path(self) -> None:
        opened = self.make_dir("Documents")
        os.chdir(opened)
        env = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        with self.unset_env(), self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assert_usable_home(resolved)
        self.assertEqual(resolved, os.path.abspath(opened))
        self.assertEqual(choice.source, "cwd")

    def test_the_state_directory_is_created_inside_the_fallback(self) -> None:
        opened = self.make_dir("Documents")
        os.chdir(opened)
        with self.unset_env(), self.patch_broken_expanduser():
            home.resolve_home()
        self.assertTrue(
            os.path.isdir(os.path.join(opened, home.STATE_DIR_NAME)),
            "the resolver must prove it can create {} where it will live".format(home.STATE_DIR_NAME),
        )

    def test_the_note_names_the_winner_and_the_reason(self) -> None:
        opened = self.make_dir("Documents")
        os.chdir(opened)
        with self.unset_env(), self.patch_broken_expanduser():
            choice = home.resolve_home_choice()
        self.assertEqual(choice.path, opened)
        self.assertTrue(choice.note.startswith("from cwd"), choice.note)
        self.assertIn("HOME was unset", choice.note)
        self.assertIn("the home directory was unresolved", choice.note)
        self.assertEqual(choice.describe(), "{} ({})".format(opened, choice.note))


class TestHomeFromEnvironment(HomeTestCase):
    def test_escape_hatch_wins_when_it_is_writable(self) -> None:
        hatch = self.path("hatch")  # does not exist yet: the escape hatch is created
        home_dir = self.make_dir("real-home")
        os.chdir(self.make_dir("Documents"))
        env = {"PYTO_HARNESS_HOME": hatch, "HOME": home_dir}
        with self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, hatch)
        self.assertEqual(choice.source, "PYTO_HARNESS_HOME")
        self.assertTrue(os.path.isdir(hatch))

    def test_escape_hatch_that_cannot_be_written_is_skipped(self) -> None:
        hatch = self.unwritable("readonly-hatch")
        home_dir = self.make_dir("real-home")
        env = {"PYTO_HARNESS_HOME": hatch, "HOME": home_dir}
        with self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, home_dir, "an unwritable PYTO_HARNESS_HOME must be skipped")
        self.assertEqual(choice.source, "HOME")
        self.assertIn("PYTO_HARNESS_HOME was not writable", choice.note)

    def test_escape_hatch_with_a_literal_tilde_is_skipped(self) -> None:
        home_dir = self.make_dir("real-home")
        env = {"PYTO_HARNESS_HOME": "~/agent", "HOME": home_dir}
        with self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, home_dir)
        self.assertTrue(no_tilde(resolved))
        self.assertIn("PYTO_HARNESS_HOME was unusable", choice.note)

    def test_absolute_writable_home_is_used(self) -> None:
        home_dir = self.make_dir("home")
        os.chdir(self.make_dir("Documents"))
        env = {"PYTO_HARNESS_HOME": "", "HOME": home_dir}
        with self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, home_dir)
        self.assertEqual(choice.source, "HOME")

    def test_unusable_home_values_fall_through_to_the_fallback(self) -> None:
        opened = self.make_dir("Documents")
        os.chdir(opened)
        cases = {
            "unset": "",
            "empty": "",
            "literal tilde": "~",
            "relative": "relative/home",
            "missing": self.path("does-not-exist"),
            "unwritable": self.unwritable("readonly-home"),
        }
        for label, value in cases.items():
            home.reset_home_cache()
            with self.subTest(home=label), self.patch_broken_expanduser():
                resolved = home.resolve_home(environ={"PYTO_HARNESS_HOME": "", "HOME": value})
            self.assertEqual(resolved, opened, label)
            self.assert_usable_home(resolved)
            self.assertTrue(no_tilde(resolved))

    def test_a_really_expanded_tilde_is_the_third_choice(self) -> None:
        passwd_home = self.make_dir("passwd-home")
        real = os.path.expanduser

        def fake(path):
            if str(path).startswith("~"):
                return passwd_home
            return real(path)

        env = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        with mock.patch.object(home.os.path, "expanduser", side_effect=fake):
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, passwd_home)
        self.assertEqual(choice.source, "expanduser")

    def test_cache_is_reused_until_the_environment_or_the_cache_changes(self) -> None:
        first = self.make_dir("first")
        second = self.make_dir("second")
        with self.patch_broken_expanduser():
            one = home.resolve_home_choice(environ={"PYTO_HARNESS_HOME": first, "HOME": ""})
            again = home.resolve_home_choice(environ={"PYTO_HARNESS_HOME": first, "HOME": ""})
            home.reset_home_cache()
            two = home.resolve_home_choice(environ={"PYTO_HARNESS_HOME": second, "HOME": ""})
        self.assertIs(one, again, "one probe, not one per call")
        self.assertEqual(one.path, first)
        self.assertEqual(two.path, second)


class TestPytoFallback(HomeTestCase):
    def test_fake_pyto_module_makes_is_pyto_true(self) -> None:
        self.install_fake_pyto()
        self.assertTrue(ios.is_pyto())

    def test_pyto_falls_back_inside_the_working_directory(self) -> None:
        self.install_fake_pyto()
        opened = self.make_dir("Documents")
        os.chdir(opened)
        env = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        with self.unset_env(), self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
        cwd = os.path.abspath(os.getcwd())
        self.assertTrue(
            resolved == cwd or resolved.startswith(cwd + os.sep),
            "{} is not inside the folder Pyto opened ({})".format(resolved, cwd),
        )
        self.assert_usable_home(resolved)
        self.assertTrue(no_tilde(resolved))
        self.assertTrue(
            os.path.isdir(os.path.join(cwd, home.STATE_DIR_NAME)),
            "{} must be created in the folder Pyto opened".format(home.STATE_DIR_NAME),
        )
        self.assertEqual(choice.source, "cwd")
        self.assertIn("Pyto", choice.note)

    def test_the_run_py_folder_is_used_when_the_working_directory_is_not(self) -> None:
        self.install_fake_pyto()
        run_folder = self.make_dir("install", "pyto-agent")
        opened = self.unwritable("Documents")  # Pyto opened a folder it cannot write
        os.chdir(opened)
        env = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        with self.unset_env(), self.patch_broken_expanduser():
            with mock.patch.object(home, "entry_point_dirs", return_value=[(run_folder, "the folder that holds run.py")]):
                resolved = home.resolve_home(environ=env)
                choice = home.resolve_home_choice(environ=env)
        self.assertEqual(resolved, run_folder)
        self.assertEqual(choice.source, "runpy")
        self.assertTrue(os.path.isdir(os.path.join(run_folder, home.STATE_DIR_NAME)))


class TestNothingWritable(HomeTestCase):
    def _all_unwritable(self, unwritable: str):
        """Make cwd, the run.py folder and the temp directory all refuse writes."""
        stack = contextlib.ExitStack()
        stack.enter_context(self.unset_env())
        stack.enter_context(self.patch_broken_expanduser())
        stack.enter_context(mock.patch.object(home, "entry_point_dirs", return_value=[]))
        stack.enter_context(mock.patch.object(home, "_temp_root", return_value=os.path.join(unwritable, "tmp")))
        return stack

    def test_raises_config_error_with_the_workaround(self) -> None:
        unwritable = self.unwritable("nowhere")
        os.chdir(unwritable)
        with self._all_unwritable(unwritable):
            with self.assertRaises(ConfigError) as caught:
                home.resolve_home()
        message = str(caught.exception)
        self.assertIn("PYTO_HARNESS_HOME", message)
        self.assertIn("~/{}".format(home.STATE_DIR_NAME), message)
        self.assertIn(os.getcwd(), message, "the message must name the folder Pyto opened")
        self.assertIn("os.getcwd()", message)
        self.assertEqual(caught.exception.code, "CONFIG_ERROR")

    def test_the_failure_is_reported_but_does_not_raise_through_the_choice_api(self) -> None:
        unwritable = self.unwritable("nowhere")
        os.chdir(unwritable)
        with self._all_unwritable(unwritable):
            choice = home.resolve_home_choice()
        self.assertFalse(choice.ok)
        self.assertEqual(choice.path, "")
        self.assertIn("PYTO_HARNESS_HOME", choice.error)
        self.assertEqual(choice.describe(), "<unresolved>")
        self.assertEqual(os.listdir(unwritable), [], "nothing may be created in an unwritable folder")

    def test_the_default_path_helpers_raise_the_same_message(self) -> None:
        unwritable = self.unwritable("nowhere")
        os.chdir(unwritable)
        with self._all_unwritable(unwritable):
            for helper in (default_home, default_workspace, default_sessions_dir):
                with self.assertRaises(ConfigError, msg=helper.__name__):
                    helper()

    def test_the_temporary_directory_is_the_last_resort_and_warns(self) -> None:
        temp_root = self.make_dir("temp")
        unwritable = self.unwritable("nowhere")
        stderr = io.StringIO()
        with self.unset_env(), self.patch_broken_expanduser():
            with mock.patch.object(home, "_cwd", return_value=unwritable), mock.patch.object(
                home, "entry_point_dirs", return_value=[]
            ), mock.patch.object(home, "_temp_root", return_value=temp_root), contextlib.redirect_stderr(stderr):
                resolved = home.resolve_home()
                choice = home.home_choice()
                warning_text = home.temporary_home_warning()
        self.assertEqual(resolved, os.path.join(temp_root, home.TEMP_HOME_DIR_NAME))
        self.assert_usable_home(resolved)
        self.assertTrue(choice.temporary)
        warning = stderr.getvalue()
        self.assertIn("temporary", warning.lower())
        self.assertIn("PYTO_HARNESS_HOME", warning)
        self.assertIn(resolved, warning_text or "")


class TestDefaultsNeverContainATilde(HomeTestCase):
    def scenarios(self):
        opened = self.make_dir("Documents")
        os.chdir(opened)
        yield "broken expanduser, no HOME", self.unset_env(), self.patch_broken_expanduser()
        yield (
            "unwritable HOME",
            self.unset_env(HOME=self.unwritable("readonly-home")),
            self.patch_broken_expanduser(),
        )
        yield (
            "escape hatch set",
            mock.patch.dict(os.environ, {"PYTO_HARNESS_HOME": self.make_dir("hatch"), "HOME": ""}),
            contextlib.nullcontext(),
        )

    def test_config_state_workspace_and_session_paths(self) -> None:
        for label, env, expanduser in self.scenarios():
            home.reset_home_cache()
            with self.subTest(scenario=label), env, expanduser:
                values = [
                    default_home(),
                    default_workspace(),
                    default_sessions_dir(),
                    default_state_dir(),
                    default_config_path(),
                    default_spill_dir(),
                ]
                for value in values:
                    self.assertTrue(os.path.isabs(value), "{}: {}".format(label, value))
                    self.assertTrue(no_tilde(value), "{}: {}".format(label, value))
                    self.assertNotIn("~", value, "{}: {}".format(label, value))
                self.assertTrue(os.path.isdir(default_home()))

    def test_an_unexpandable_config_or_state_override_is_refused(self) -> None:
        self.make_dir("Documents")
        for name, helper in (
            ("PYTO_HARNESS_CONFIG", default_config_path),
            ("PYTO_HARNESS_STATE_DIR", default_state_dir),
        ):
            home.reset_home_cache()
            with self.subTest(variable=name), self.unset_env(**{name: "~/somewhere"}), self.patch_broken_expanduser():
                with self.assertRaises(ConfigError) as caught:
                    helper()
                self.assertIn(name, str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "~")))


class TestUserSuppliedPaths(HomeTestCase):
    def test_workspace_with_a_literal_tilde_is_rejected_not_created(self) -> None:
        cwd = self.make_dir("Documents")
        os.chdir(cwd)
        with self.patch_broken_expanduser():
            with self.assertRaises(ConfigError) as caught:
                ensure_workspace(Config(workspace="~/pyto_harness_workspace"))
        self.assertIn("~", str(caught.exception))
        self.assertIn("absolute path", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(cwd, "~")), "a directory named '~' was created")
        self.assertFalse(os.path.exists(os.path.join(cwd, home.STATE_DIR_NAME)))

    def test_session_paths_with_a_literal_tilde_are_rejected(self) -> None:
        os.chdir(self.make_dir("Documents"))
        with self.patch_broken_expanduser():
            with self.assertRaises(ConfigError):
                new_session_path("~/sessions")

    def test_an_unwritable_workspace_reports_the_workaround(self) -> None:
        workspace = os.path.join(self.unwritable("readonly-workspace"), "workspace")
        with self.assertRaises(ConfigError) as caught:
            ensure_workspace(Config(workspace=workspace))
        message = str(caught.exception)
        self.assertIn("PYTO_HARNESS_HOME", message)
        self.assertIn("could not be created", message)

    def test_an_unwritable_sessions_directory_reports_the_workaround(self) -> None:
        sessions = os.path.join(self.unwritable("readonly-sessions"), "sessions")
        with self.assertRaises(ConfigError) as caught:
            SessionLog.create(os.path.join(sessions, "session.jsonl"), workspace=self.tmp)
        message = str(caught.exception)
        self.assertIn("PYTO_HARNESS_HOME", message)
        self.assertIn("sessions directory", message)

    def test_the_config_path_is_built_from_the_resolved_home(self) -> None:
        hatch = self.make_dir("hatch")
        with self.unset_env(PYTO_HARNESS_HOME=hatch, PYTO_HARNESS_CONFIG="", PYTO_HARNESS_STATE_DIR=""):
            home.reset_home_cache()
            expanded = default_home()
            self.assertEqual(expanded, hatch)
            self.assertEqual(default_config_path(), os.path.join(expanded, home.STATE_DIR_NAME, "config.json"))


class TestVisibleStateDirectory(HomeTestCase):
    """The state folder is ``pyto_harness`` — no leading dot, so the Files app shows it."""

    def test_a_fresh_home_resolves_to_the_visible_state_directory(self) -> None:
        home_dir = self.make_dir("home")
        with self.unset_env(PYTO_HARNESS_HOME=home_dir, PYTO_HARNESS_CONFIG="", PYTO_HARNESS_STATE_DIR=""):
            home.reset_home_cache()
            resolved = {
                "home": default_home(),
                "state": default_state_dir(),
                "config": default_config_path(),
                "sessions": default_sessions_dir(),
                "workspace": default_workspace(),
                "spill": default_spill_dir(),
            }
        self.assertEqual(resolved["home"], home_dir)
        self.assertEqual(resolved["state"], os.path.join(home_dir, home.STATE_DIR_NAME))
        self.assertEqual(resolved["config"], os.path.join(home_dir, home.STATE_DIR_NAME, "config.json"))
        self.assertEqual(resolved["sessions"], os.path.join(home_dir, home.STATE_DIR_NAME, "sessions"))
        self.assertEqual(resolved["workspace"], os.path.join(home_dir, "pyto_harness_workspace"))
        self.assertEqual(resolved["spill"], os.path.join(home_dir, "pyto_harness_workspace", "tool-output"))
        for label, value in resolved.items():
            if label == "home":
                continue
            self.assertTrue(os.path.isabs(value), value)
            self.assertTrue(no_tilde(value), value)
            for part in os.path.relpath(value, home_dir).split(os.sep):
                self.assertFalse(part.startswith("."), "{} is hidden from Files: {}".format(label, value))


class TestLegacyStateMigration(HomeTestCase):
    """The one-time move of ``.pyto_harness`` to ``pyto_harness``.

    This is the only test module that spells the old name out: the rules below exist so
    the move can never lose data, never merge, and never touch the new directory.
    """

    LEGACY = ".pyto_harness"
    MESSAGE = "moved the old ~/.pyto_harness to ~/pyto_harness so the Files app can see it"

    def write(self, path: str, text: str = "{}\n") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_move_keeps_every_file_and_returns_the_users_message(self) -> None:
        home_dir = self.make_dir("home")
        self.write(os.path.join(home_dir, self.LEGACY, "config.json"), '{"api_key": "sk-not-real"}')
        self.write(os.path.join(home_dir, self.LEGACY, "sessions", "s.jsonl"), '{"kind": "header"}\n')

        message = home.migrate_legacy_state(home_dir)

        self.assertEqual(message, self.MESSAGE)
        self.assertEqual(message, home.MIGRATED_MESSAGE)
        self.assertFalse(os.path.lexists(os.path.join(home_dir, self.LEGACY)), "the hidden folder must be gone")
        moved = os.path.join(home_dir, home.STATE_DIR_NAME)
        self.assertEqual(self.read(os.path.join(moved, "config.json")), '{"api_key": "sk-not-real"}')
        self.assertEqual(self.read(os.path.join(moved, "sessions", "s.jsonl")), '{"kind": "header"}\n')
        self.assertEqual(home.state_dir_in(home_dir), moved)
        self.assertEqual(home.legacy_state_dir_in(home_dir), os.path.join(home_dir, self.LEGACY))

    def test_the_move_is_idempotent(self) -> None:
        home_dir = self.make_dir("home")
        self.write(os.path.join(home_dir, self.LEGACY, "config.json"), "{}")
        self.assertEqual(home.migrate_legacy_state(home_dir), self.MESSAGE)
        self.assertEqual(home.migrate_legacy_state(home_dir), "", "the second call must do nothing")
        self.assertTrue(os.path.isfile(os.path.join(home_dir, home.STATE_DIR_NAME, "config.json")))

    def test_nothing_to_do_when_neither_directory_exists(self) -> None:
        home_dir = self.make_dir("home")
        self.assertEqual(home.migrate_legacy_state(home_dir), "")
        self.assertEqual(os.listdir(home_dir), [], "nothing may be created by a no-op")

    def test_a_probe_created_new_directory_still_receives_the_old_data(self) -> None:
        """The regression: a write probe created an empty ``pyto_harness`` first.

        The old rule ("new directory exists → do nothing") left the user's key and
        sessions stranded in the hidden folder.  Now the entries are merged in one by one.
        """
        home_dir = self.make_dir("home")
        self.write(os.path.join(home_dir, self.LEGACY, "config.json"), '{"api_key": "sk-old-key"}')
        self.write(os.path.join(home_dir, self.LEGACY, "sessions", "s.jsonl"), '{"kind": "header"}\n')
        os.makedirs(os.path.join(home_dir, home.STATE_DIR_NAME))  # exactly what the probe leaves

        message = home.migrate_legacy_state(home_dir)

        self.assertIn(home.MERGED_MESSAGE, message)
        self.assertIn("moved: config.json, sessions", message)
        moved = os.path.join(home_dir, home.STATE_DIR_NAME)
        self.assertEqual(self.read(os.path.join(moved, "config.json")), '{"api_key": "sk-old-key"}')
        self.assertEqual(self.read(os.path.join(moved, "sessions", "s.jsonl")), '{"kind": "header"}\n')
        self.assertFalse(os.path.lexists(os.path.join(home_dir, self.LEGACY)), "the emptied folder must go")
        self.assertEqual(home.migrate_legacy_state(home_dir), "", "the second call must do nothing")

    def test_a_configured_new_directory_is_left_alone_and_reported(self) -> None:
        """Two configs are never merged: the new one wins, the old one is named, not touched."""
        home_dir = self.make_dir("home")
        new_config = self.write(os.path.join(home_dir, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-new"}')
        old_config = self.write(os.path.join(home_dir, self.LEGACY, "config.json"), '{"api_key": "sk-old"}')
        self.write(os.path.join(home_dir, self.LEGACY, "sessions", "s.jsonl"), "{}\n")

        message = home.migrate_legacy_state(home_dir)

        self.assertIn("left untouched", message)
        self.assertIn(home.legacy_state_dir_in(home_dir), message, "the report must name the folder")
        self.assertIn("rm -rf", message, "and say how to remove it")
        self.assertEqual(self.read(new_config), '{"api_key": "sk-new"}', "the new config wins")
        self.assertEqual(self.read(old_config), '{"api_key": "sk-old"}', "the old config is untouched")
        self.assertFalse(os.path.exists(os.path.join(home_dir, home.STATE_DIR_NAME, "sessions")), "never merged")
        self.assertTrue(os.path.isdir(os.path.join(home_dir, self.LEGACY)), "left where it is")
        self.assertEqual(home.migrate_legacy_state(home_dir), message, "still reported on the next run")

    def test_a_merge_moves_only_the_missing_entries_and_names_what_it_skipped(self) -> None:
        home_dir = self.make_dir("home")
        new = os.path.join(home_dir, home.STATE_DIR_NAME)
        self.write(os.path.join(new, "health.json"), "the new health\n")
        collided = self.write(os.path.join(new, "notes.txt"), "the new notes\n")
        old_only = os.path.join(home_dir, self.LEGACY, "notes.txt")
        self.write(old_only, "the old notes\n")
        self.write(os.path.join(home_dir, self.LEGACY, "memory.json"), "the old memory\n")

        message = home.migrate_legacy_state(home_dir)

        self.assertIn(home.MERGED_MESSAGE, message)
        self.assertIn("moved: memory.json", message)
        self.assertIn("left in {}".format(new), message)
        self.assertIn("notes.txt", message.split("left in", 1)[1], "the skipped entry must be named")
        self.assertEqual(self.read(os.path.join(new, "memory.json")), "the old memory\n")
        self.assertEqual(self.read(collided), "the new notes\n", "a collision is never overwritten")
        self.assertEqual(self.read(os.path.join(new, "health.json")), "the new health\n")
        self.assertTrue(os.path.isdir(os.path.join(home_dir, self.LEGACY)), "non-empty: it must survive")
        self.assertEqual(self.read(old_only), "the old notes\n", "the file it could not take stays")

    def test_a_symlinked_old_folder_is_refused_even_when_the_new_one_exists(self) -> None:
        home_dir = self.make_dir("home")
        os.makedirs(os.path.join(home_dir, home.STATE_DIR_NAME))
        elsewhere = self.make_dir("elsewhere")
        self.write(os.path.join(elsewhere, "precious.txt"), "somebody else's data\n")
        os.symlink(elsewhere, os.path.join(home_dir, self.LEGACY))

        message = home.migrate_legacy_state(home_dir)

        self.assertIn("symbolic link", message)
        self.assertTrue(os.path.islink(os.path.join(home_dir, self.LEGACY)), "the link must stay")
        self.assertEqual(os.listdir(os.path.join(home_dir, home.STATE_DIR_NAME)), [], "nothing may be merged")
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "precious.txt")), "the target must not be moved")

    def test_a_regular_file_in_the_new_path_is_refused_and_the_old_folder_kept(self) -> None:
        home_dir = self.make_dir("home")
        self.write(os.path.join(home_dir, home.STATE_DIR_NAME), "not a directory\n")
        self.write(os.path.join(home_dir, self.LEGACY, "config.json"), '{"api_key": "sk-old"}')

        message = home.migrate_legacy_state(home_dir)

        self.assertIn("is a file, not a folder", message)
        self.assertTrue(os.path.isdir(os.path.join(home_dir, self.LEGACY)), "the old folder is left alone")
        self.assertEqual(self.read(os.path.join(home_dir, self.LEGACY, "config.json")), '{"api_key": "sk-old"}')

    def test_an_empty_old_folder_is_left_for_the_doctor_to_report(self) -> None:
        """Nothing to take and nothing to refuse: stay silent, keep the folder, create nothing."""
        home_dir = self.make_dir("home")
        os.makedirs(os.path.join(home_dir, self.LEGACY))
        os.makedirs(os.path.join(home_dir, home.STATE_DIR_NAME))

        self.assertEqual(home.migrate_legacy_state(home_dir), "")
        self.assertTrue(os.path.isdir(os.path.join(home_dir, self.LEGACY)), "must not be deleted")
        self.assertEqual(os.listdir(os.path.join(home_dir, home.STATE_DIR_NAME)), [])

    def test_nothing_to_take_is_reported_when_every_entry_is_already_there(self) -> None:
        home_dir = self.make_dir("home")
        new = os.path.join(home_dir, home.STATE_DIR_NAME)
        self.write(os.path.join(new, "health.json"), "the new health\n")
        self.write(os.path.join(home_dir, self.LEGACY, "health.json"), "the old health\n")

        message = home.migrate_legacy_state(home_dir)

        self.assertIn(home.MERGE_KEPT_MESSAGE, message)
        self.assertIn("health.json", message, "the collision must be named")
        self.assertEqual(self.read(os.path.join(new, "health.json")), "the new health\n", "never overwritten")
        self.assertTrue(os.path.isdir(os.path.join(home_dir, self.LEGACY)), "non-empty: it survives")

    def test_a_regular_file_is_refused_reported_and_left_alone(self) -> None:
        home_dir = self.make_dir("home")
        legacy = self.write(os.path.join(home_dir, self.LEGACY), "not a directory\n")

        message = home.migrate_legacy_state(home_dir)

        self.assertIn("is a file, not a folder", message)
        self.assertIn(legacy, message)
        self.assertEqual(self.read(legacy), "not a directory\n", "the file must be untouched")
        self.assertFalse(os.path.lexists(os.path.join(home_dir, home.STATE_DIR_NAME)))

    def test_a_symlink_is_refused_reported_and_left_alone(self) -> None:
        home_dir = self.make_dir("home")
        elsewhere = self.make_dir("elsewhere")
        self.write(os.path.join(elsewhere, "precious.txt"), "somebody else's data\n")
        os.symlink(elsewhere, os.path.join(home_dir, self.LEGACY))

        message = home.migrate_legacy_state(home_dir)

        self.assertIn("symbolic link", message)
        self.assertTrue(os.path.islink(os.path.join(home_dir, self.LEGACY)), "the link must stay where it is")
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "precious.txt")), "the target must not be moved")
        self.assertFalse(os.path.lexists(os.path.join(home_dir, home.STATE_DIR_NAME)))

    def test_an_unwritable_parent_is_reported_not_raised(self) -> None:
        home_dir = self.make_dir("home")
        legacy = os.path.join(home_dir, self.LEGACY)
        self.write(os.path.join(legacy, "config.json"), "{}")
        if os.geteuid() == 0:  # pragma: no cover - root can rename inside a 0500 directory
            self.skipTest("running as root: rename succeeds regardless of the mode")
        os.chmod(home_dir, 0o500)
        self.addCleanup(os.chmod, home_dir, 0o700)

        message = home.migrate_legacy_state(home_dir)  # must not raise

        self.assertIn("could not move", message)
        self.assertIn(legacy, message)
        self.assertTrue(os.path.isfile(os.path.join(legacy, "config.json")), "a failed move leaves it alone")
        self.assertFalse(os.path.lexists(os.path.join(home_dir, home.STATE_DIR_NAME)))

    def test_an_empty_home_is_a_no_op(self) -> None:
        self.assertEqual(home.migrate_legacy_state(""), "")


class TestCandidateHomes(HomeTestCase):
    """The guess the startup hooks use *before* anything resolves a home.

    The resolver proves a candidate with a write probe, and that probe creates
    ``<home>/pyto_harness`` — which is what used to defeat the one-time move.  So the
    guess must find the plausible homes without creating, writing or probing anything.
    """

    def write(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def snapshot(self) -> "list[str]":
        return sorted(
            os.path.join(root, name) for root, dirs, files in os.walk(self.tmp) for name in list(dirs) + list(files)
        )

    def guesses(self, **env: str):
        """``candidate_homes`` with the cwd and entry points pinned inside the temp tree."""
        self.entry = self.make_dir("entry")
        self.cwd = self.make_dir("cwd")
        return mock.patch.object(home, "_cwd", return_value=self.cwd), mock.patch.object(
            home, "entry_point_dirs", return_value=[(self.entry, "the folder that holds run.py")]
        )

    def test_it_lists_the_resolver_candidates_in_order(self) -> None:
        escape = self.make_dir("escape")
        home_dir = self.make_dir("home")
        expanded = self.make_dir("expanded")
        cwd_patch, entry_patch = self.guesses()
        with cwd_patch, entry_patch, mock.patch.object(home.os.path, "expanduser", return_value=expanded):
            candidates = home.candidate_homes({"PYTO_HARNESS_HOME": escape, "HOME": home_dir})
        self.assertEqual(candidates, [escape, home_dir, expanded, self.entry, self.cwd])

    def test_it_lists_the_state_override_the_pointer_and_the_install_parent(self) -> None:
        """The new rules are guesses too: migration must look where the resolver will."""
        install = self.path("install", "pyto-agent")
        os.makedirs(install)
        with open(os.path.join(install, "run.py"), "w", encoding="utf-8") as handle:
            handle.write("# fake\n")
        remembered = self.make_dir("remembered")
        with open(os.path.join(install, home.POINTER_FILE_NAME), "w", encoding="utf-8") as handle:
            handle.write("{}\n".format(remembered))
        with mock.patch.object(home, "_cwd", return_value=self.make_dir("cwd")), mock.patch.object(
            home, "entry_point_dirs", return_value=[(install, "the folder that holds run.py")]
        ), self.patch_broken_expanduser():
            candidates = home.candidate_homes(
                {
                    "PYTO_HARNESS_HOME": "",
                    "HOME": "",
                    "PYTO_HARNESS_STATE_DIR": self.path("state", home.STATE_DIR_NAME),
                    "PYTO_HARNESS_CONFIG": "",
                }
            )
        self.assertEqual(
            candidates,
            [self.path("state"), remembered, self.path("install"), install, self.path("cwd")],
            "state override, remembered choice, install parent, install, cwd -- in that order",
        )

    def test_it_skips_what_the_resolver_would_skip(self) -> None:
        cwd_patch, entry_patch = self.guesses()
        with cwd_patch, entry_patch, mock.patch.object(home.os.path, "expanduser", return_value="~"):
            candidates = home.candidate_homes({"PYTO_HARNESS_HOME": "~/escape", "HOME": "relative/home"})
        self.assertEqual(
            candidates, [self.entry, self.cwd], "an unexpandable '~' and a relative HOME are not homes"
        )

    def test_it_never_creates_anything(self) -> None:
        cwd_patch, entry_patch = self.guesses()
        before = self.snapshot()
        with cwd_patch, entry_patch, mock.patch.object(home.os.path, "expanduser", return_value=self.path("expanded")):
            candidates = home.candidate_homes(
                {
                    "PYTO_HARNESS_HOME": self.path("guessed"),
                    "HOME": self.path("home-ish"),
                    "PYTO_HARNESS_STATE_DIR": self.path("state-dir"),
                }
            )
            self.assertIn(self.path("guessed"), candidates)
            self.assertEqual(self.snapshot(), before, "guessing must not create a directory")
        self.assertEqual(self.snapshot(), before)

    def test_a_pyto_like_run_migrates_the_working_directory_without_probing_it(self) -> None:
        """No HOME, an unexpandable '~', an old folder in the folder Pyto opened."""
        cwd_patch, entry_patch = self.guesses()
        self.write(os.path.join(self.cwd, home.LEGACY_STATE_DIR_NAME, "config.json"), '{"api_key": "sk-old"}')
        with cwd_patch, entry_patch, mock.patch.object(home.os.path, "expanduser", return_value="~"):
            message = home.migrate_candidate_homes({"PYTO_HARNESS_HOME": "", "HOME": ""})
        self.assertEqual(message, home.MIGRATED_MESSAGE)
        self.assertEqual(
            self.read(os.path.join(self.cwd, home.STATE_DIR_NAME, "config.json")), '{"api_key": "sk-old"}'
        )
        self.assertFalse(os.path.lexists(os.path.join(self.cwd, home.LEGACY_STATE_DIR_NAME)))

    def test_it_is_silent_and_creates_nothing_when_there_is_no_old_folder(self) -> None:
        cwd_patch, entry_patch = self.guesses()
        before = self.snapshot()
        with cwd_patch, entry_patch, mock.patch.object(home.os.path, "expanduser", return_value="~"):
            message = home.migrate_candidate_homes({"PYTO_HARNESS_HOME": "", "HOME": ""})
        self.assertEqual(message, "")
        self.assertEqual(self.snapshot(), before, "the startup hook must not create a state directory")


class TestInstallAnchoredState(HomeTestCase):
    """The bug report from the iPhone: three ``pyto_harness`` folders and one key.

    The install sits in ``<root>/pyto-agent``, the key is in ``<root>/pyto_harness``, and
    the harness is started from inside the install -- so the old resolver fell back to the
    current directory, looked for ``<install>/pyto_harness/config.json`` and announced "no
    API key" next to the user's key.  The state is now anchored to the install: the folder
    *above* it is adopted because it already holds the config, and nothing is moved.
    """

    def write(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def phone_tree(self):
        """The reported layout, exactly: install, key one level up, a stray state folder."""
        root = self.make_dir("root")
        install = self.pin_install("root", "pyto-agent")
        key = self.write(os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-phone-key"}')
        stray = os.path.join(install, home.STATE_DIR_NAME)  # what the cwd fallback left there
        self.write(os.path.join(stray, "health.json"), "{}\n")
        self.write(os.path.join(stray, "capabilities.json"), '{"modules": {}}\n')
        os.makedirs(os.path.join(stray, "sessions"), exist_ok=True)
        return root, install, key

    def test_the_key_one_level_above_the_install_is_found(self) -> None:
        root, install, key = self.phone_tree()
        os.chdir(install)
        env = {"PYTO_HARNESS_HOME": "", "HOME": ""}
        with self.unset_env(), self.patch_broken_expanduser():
            resolved = home.resolve_home(environ=env)
            choice = home.resolve_home_choice(environ=env)
            config = default_config_path()
        self.assertEqual(resolved, root, "the folder that already holds the key must be adopted")
        self.assertEqual(choice.source, "adopted")
        self.assertIn("adopted {}".format(root), choice.note)
        self.assertEqual(config, key)
        self.assertEqual(json.loads(self.read(config))["api_key"], "sk-phone-key")

    def test_the_cwd_fallback_folder_inside_the_install_is_left_alone(self) -> None:
        """Nothing is created, moved or deleted inside the install except the pointer."""
        root, install, _key = self.phone_tree()
        stray = os.path.join(install, home.STATE_DIR_NAME)
        before = sorted(os.listdir(stray))
        os.chdir(install)
        with self.unset_env(), self.patch_broken_expanduser():
            home.resolve_home()
        self.assertEqual(sorted(os.listdir(stray)), before, "the stray state folder must be untouched")
        self.assertFalse(
            os.path.exists(os.path.join(stray, "config.json")),
            "no config may be invented inside the install",
        )
        self.assertEqual(
            sorted(os.listdir(install)),
            sorted(["run.py", home.STATE_DIR_NAME, home.POINTER_FILE_NAME]),
            "the install folder gains the pointer file and nothing else",
        )

    def test_nothing_at_all_is_created_inside_a_clean_install(self) -> None:
        root = self.make_dir("root")
        install = self.pin_install("root", "pyto-agent")
        self.write(os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-x"}')
        os.chdir(install)
        with self.unset_env(), self.patch_broken_expanduser():
            resolved = home.resolve_home()
        self.assertEqual(resolved, root)
        self.assertEqual(
            sorted(os.listdir(install)),
            sorted(["run.py", home.POINTER_FILE_NAME]),
            "the install folder gains the pointer file and nothing else",
        )

    def test_the_same_home_from_the_parent_the_install_and_an_unrelated_folder(self) -> None:
        """Stability: the rule may change (adopt, then the pointer), the answer may not."""
        root, install, _key = self.phone_tree()
        elsewhere = self.make_dir("elsewhere")
        seen = []
        for where in (root, install, elsewhere):
            home.reset_home_cache()
            os.chdir(where)
            with self.unset_env(), self.patch_broken_expanduser():
                seen.append((home.resolve_home(), home.resolve_home_choice().source))
        self.assertEqual([item[0] for item in seen], [root, root, root], seen)
        self.assertEqual(seen[0][1], "adopted", "the first run finds the existing state")
        for _path, source in seen[1:]:
            self.assertEqual(source, "pointer", "later runs follow the remembered choice")


class TestRememberedChoice(HomeTestCase):
    """The visible pointer file next to the entry point: written, honoured, validated."""

    def write(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def install_with_state(self, *parts: str):
        """A fake install in the temp tree plus a home that already holds a config."""
        root = self.make_dir(*parts)
        install = self.pin_install(*parts, "pyto-agent")
        self.write(os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-remembered"}')
        return root, install

    def test_it_is_written_next_to_the_entry_point_and_honoured_on_the_next_call(self) -> None:
        root, install = self.install_with_state("root")
        pointer = os.path.join(install, home.POINTER_FILE_NAME)
        os.chdir(install)
        with self.unset_env(), self.patch_broken_expanduser():
            home.resolve_home()
            self.assertTrue(os.path.isfile(pointer), "the choice must be remembered")
            self.assertIn(root, self.read(pointer))
            self.assertIn(home.POINTER_HEADER, self.read(pointer))

            home.reset_home_cache()
            os.chdir(self.make_dir("somewhere-else"))  # a different start directory
            resolved = home.resolve_home()
            choice = home.resolve_home_choice()
        self.assertEqual(resolved, root)
        self.assertEqual(choice.source, "pointer")
        self.assertTrue(choice.pointer_used)
        self.assertEqual(choice.pointer, pointer)

    def test_a_pointer_that_names_an_unwritable_folder_is_ignored_and_reported(self) -> None:
        root, install = self.install_with_state("root")
        gone = self.unwritable("gone")
        pointer = self.write(os.path.join(install, home.POINTER_FILE_NAME), gone + "\n")
        stderr = io.StringIO()
        os.chdir(self.make_dir("Documents"))
        with self.unset_env(), self.patch_broken_expanduser(), contextlib.redirect_stderr(stderr):
            resolved = home.resolve_home()
            choice = home.resolve_home_choice()
        self.assertNotEqual(resolved, gone)
        self.assertIsNone(home.writability_problem(resolved))
        self.assertIn("the remembered choice was ignored", choice.note)
        report = stderr.getvalue()
        self.assertIn(pointer, report, "the ignored pointer file must be named")
        self.assertIn(gone, report)
        self.assertIn(resolved, self.read(pointer), "the pointer is repaired with the home that won")

    def test_a_stale_or_foreign_pointer_file_is_ignored_without_raising(self) -> None:
        root, install = self.install_with_state("root")
        pointer = os.path.join(install, home.POINTER_FILE_NAME)
        cases = {
            "garbage": "just some notes, not a path\n",
            "relative": "pyto_harness\n",
            "empty": "# nothing but a comment\n",
            "unexpandable tilde": "~/pyto_harness\n",
            "binary junk": "\x00\x01\x02 not utf-8 \xff\n",
        }
        for label, text in cases.items():
            home.reset_home_cache()
            with self.subTest(content=label):
                with open(pointer, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(text)
                os.chdir(self.make_dir("Documents"))
                with self.unset_env(), self.patch_broken_expanduser(), contextlib.redirect_stderr(io.StringIO()):
                    resolved = home.resolve_home()  # must not raise
                    choice = home.resolve_home_choice()
                self.assertEqual(resolved, root, label)
                self.assertIn("the remembered choice was ignored", choice.note, label)
                self.assertIn(resolved, self.read(pointer), "the pointer is rewritten, not trusted")


class TestAmbiguityAndDuplicates(HomeTestCase):
    """Two configs, or an iCloud copy: name every folder, merge nothing, delete nothing."""

    def write(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_two_candidates_with_a_config_warn_and_the_first_one_wins(self) -> None:
        root = self.make_dir("root")                       # the install's parent
        install = self.pin_install("root", "pyto-agent")
        home_dir = self.make_dir("home")                   # HOME: rule 5, ahead of the install
        near_install = self.write(
            os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-near-install"}'
        )
        in_home = self.write(
            os.path.join(home_dir, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-in-home"}'
        )
        stderr = io.StringIO()
        os.chdir(install)
        with self.unset_env(HOME=home_dir), self.patch_broken_expanduser(), contextlib.redirect_stderr(stderr):
            resolved = home.resolve_home()
            choice = home.resolve_home_choice()
        self.assertEqual(resolved, home_dir, "the highest-precedence candidate wins")
        self.assertEqual(choice.source, "adopted")
        self.assertEqual(len(choice.candidates), 2)
        self.assertEqual([item.home for item in choice.unchosen_candidates()], [root])
        warning = stderr.getvalue()
        self.assertIn(in_home, warning, "the config in use must be named")
        self.assertIn(near_install, warning, "the other config must be named too")
        self.assertIn("in use", warning)
        self.assertIn("PYTO_HARNESS_HOME", warning, "and how to switch")
        self.assertIn(near_install, choice.warning)
        self.assertEqual(self.read(in_home), '{"api_key": "sk-in-home"}', "nothing is merged")
        self.assertEqual(self.read(near_install), '{"api_key": "sk-near-install"}', "nothing is moved")

    def test_a_pyto_harness_2_folder_is_reported_as_a_likely_icloud_copy(self) -> None:
        root = self.make_dir("root")
        install = self.pin_install("root", "pyto-agent")
        self.write(os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-one"}')
        copy_dir = self.make_dir("root", "{} 2".format(home.STATE_DIR_NAME))
        copy_config = self.write(os.path.join(copy_dir, "config.json"), '{"api_key": "sk-two"}')
        install_copy = self.make_dir("root", "pyto-agent 2")
        stderr = io.StringIO()
        os.chdir(install)
        with self.unset_env(), self.patch_broken_expanduser(), contextlib.redirect_stderr(stderr):
            resolved = home.resolve_home()
            choice = home.resolve_home_choice()
        self.assertEqual(resolved, root, "an iCloud copy is never adopted")
        self.assertIn(copy_dir, choice.duplicates)
        self.assertIn(install_copy, choice.duplicates, "a duplicated install folder counts too")
        report = stderr.getvalue()
        self.assertIn("iCloud/Files copies", report)
        self.assertIn(copy_dir, report)
        self.assertIn("keep one", report)
        self.assertEqual(self.read(copy_config), '{"api_key": "sk-two"}', "a copy is never rewritten")
        self.assertTrue(os.path.isdir(copy_dir), "a copy is never deleted")
        self.assertTrue(os.path.isdir(install_copy), "a duplicated install is never touched")

    def test_no_duplicate_warning_when_there_is_nothing_to_report(self) -> None:
        root = self.make_dir("root")
        install = self.pin_install("root", "pyto-agent")
        self.write(os.path.join(root, home.STATE_DIR_NAME, "config.json"), '{"api_key": "sk-one"}')
        stderr = io.StringIO()
        os.chdir(install)
        with self.unset_env(), self.patch_broken_expanduser(), contextlib.redirect_stderr(stderr):
            home.resolve_home()
            choice = home.resolve_home_choice()
        self.assertEqual(choice.duplicates, ())
        self.assertEqual(choice.warning, "")
        self.assertNotIn("iCloud", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

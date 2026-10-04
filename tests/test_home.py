"""The home resolver: a writable, absolute ``~`` even where Pyto has none.

The bug these tests pin down was reported from a real iPhone:

    [Errno 1] Operation not permitted: '~/.pyto_harness'

Pyto has no usable home directory, so ``os.path.expanduser("~")`` returns the string
``"~"`` and every derived path became a *relative* path with a literal tilde.  The first
write tried to create a directory literally called ``~`` and iOS refused it.

The simulation used everywhere here is the honest one: ``os.path.expanduser`` is
monkeypatched to return its argument unchanged for a leading ``~`` — exactly what Pyto
effectively does — and the candidate directories are made genuinely unwritable with a
mode change, so the resolver's write probe really fails.  No device, no network.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import io
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

    # -- helpers -------------------------------------------------------------------

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
            "the resolver must prove it can create .pyto_harness where it will live",
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
            ".pyto_harness must be created in the folder Pyto opened",
        )
        self.assertEqual(choice.source, "cwd")
        self.assertIn("Pyto", choice.note)

    def test_the_run_py_folder_is_tried_after_the_working_directory(self) -> None:
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
        self.assertIn("~/.pyto_harness", message)
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
        self.assertFalse(os.path.exists(os.path.join(cwd, ".pyto_harness")))

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


if __name__ == "__main__":
    unittest.main()

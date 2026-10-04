"""Tests for install.py — the GitHub installer and the one-stop setup.

Everything here is offline: the network path is exercised through ``--zip`` and through
an injected opener, and the setup phase talks to ``tests/mock_provider.py`` on
127.0.0.1 only.  The real ``~/.pyto_harness`` is never touched: every setup test points
``PYTO_HARNESS_CONFIG``/``PYTO_HARNESS_STATE_DIR``/``PYTO_HARNESS_WORKSPACE`` at a
private temp directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import install  # noqa: E402
from tests.mock_provider import MockProvider, error_response, text_response  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_zip(entries, *, root="pyto-agent-main"):
    """A zip in GitHub's archive shape: one top-level directory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            path = "{}/{}".format(root, name) if root else name
            archive.writestr(path, payload)
    buffer.seek(0)
    return buffer


VALID = {
    "run.py": "import sys\nprint('ok')\n",
    "harness/__init__.py": '__version__ = "9.9.9"\n',
    "harness/simple.py": "VALUE = 1\n",
    "README.md": "# hi\n",
}


class TestSafeMembers(unittest.TestCase):
    def test_strips_the_single_top_level_directory(self):
        with zipfile.ZipFile(make_zip(VALID)) as archive:
            pairs = install.safe_members(archive)
        relatives = sorted(relative for _member, relative in pairs)
        self.assertEqual(relatives, ["README.md", "harness/__init__.py", "harness/simple.py", "run.py"])

    def test_skips_caches_and_compiled_files(self):
        entries = dict(VALID)
        entries["harness/__pycache__/simple.cpython-310.pyc"] = "junk"
        entries["harness/simple.pyc"] = "junk"
        entries[".git/config"] = "junk"
        with zipfile.ZipFile(make_zip(entries)) as archive:
            pairs = install.safe_members(archive)
        relatives = [relative for _member, relative in pairs]
        self.assertNotIn("harness/simple.pyc", relatives)
        self.assertFalse(any("__pycache__" in name for name in relatives))
        self.assertFalse(any(name.startswith(".git") for name in relatives))

    def test_refuses_a_path_that_escapes_the_target(self):
        with zipfile.ZipFile(make_zip({"../evil.txt": "pwned", "run.py": "x"})) as archive:
            with self.assertRaises(install.InstallError) as caught:
                install.safe_members(archive)
        self.assertIn("unsafe path", str(caught.exception))

    def test_refuses_an_absolute_path(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("/etc/passwd", "nope")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            with self.assertRaises(install.InstallError):
                install.safe_members(archive)

    def test_empty_archive_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            with self.assertRaises(install.InstallError):
                install.safe_members(archive)

    def test_refuses_a_windows_style_traversal(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("pyto-agent-main/run.py", "x")
            archive.writestr("pyto-agent-main/..\\evil.txt", "pwned")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            with self.assertRaises(install.InstallError):
                install.safe_members(archive)

    def test_accepts_a_single_root_with_backslashes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("pyto-agent-main\\run.py", "x")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            pairs = install.safe_members(archive)
        self.assertEqual([rel for _m, rel in pairs], ["run.py"])


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp(prefix="pyto-install-test-")
        self.addCleanup(shutil.rmtree, self.target, True)

    def test_writes_the_tree_and_reports_it(self):
        with zipfile.ZipFile(make_zip(VALID)) as archive:
            written = install.extract(archive, self.target)
        self.assertIn("run.py", written)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "run.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "harness", "__init__.py")))

    def test_requires_run_py(self):
        entries = {name: payload for name, payload in VALID.items() if name != "run.py"}
        with zipfile.ZipFile(make_zip(entries)) as archive:
            with self.assertRaises(install.InstallError) as caught:
                install.extract(archive, self.target)
        self.assertIn("run.py", str(caught.exception))

    def test_extraction_overwrites_but_keeps_unrelated_files(self):
        os.makedirs(self.target, exist_ok=True)
        with open(os.path.join(self.target, "mine.txt"), "w", encoding="utf-8") as handle:
            handle.write("keep me")
        with zipfile.ZipFile(make_zip(VALID)) as archive:
            install.extract(archive, self.target)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "mine.txt")))


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp(prefix="pyto-install-verify-")
        self.addCleanup(shutil.rmtree, self.target, True)
        with zipfile.ZipFile(make_zip(VALID)) as archive:
            install.extract(archive, self.target)

    def test_imports_the_installed_package_and_reports_its_version(self):
        before = sys.modules.get("harness")
        notes = install.verify(self.target)
        self.assertTrue(any("9.9.9" in note for note in notes), notes)
        self.assertIs(
            sys.modules.get("harness"), before,
            "verify must not import, replace or remove a harness under its real name",
        )

    def test_verifying_never_disturbs_an_already_imported_harness(self):
        """Regression: verify() used to delete the real `harness` from sys.modules."""
        import harness as real

        marker = real.__version__
        before = {key: value for key, value in sys.modules.items() if key == "harness" or key.startswith("harness.")}
        install.verify(self.target)
        after = {key: value for key, value in sys.modules.items() if key == "harness" or key.startswith("harness.")}
        self.assertEqual(set(before), set(after), "sys.modules changed for the real package")
        self.assertIs(after["harness"], before["harness"], "the real package object was replaced")
        self.assertEqual(sys.modules["harness"].__version__, marker)

    def test_a_file_that_is_not_python_310_fails(self):
        path = os.path.join(self.target, "harness", "future.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("def f[T](x: T) -> T:\n    return x\n")  # PEP 695: 3.12 only
        with self.assertRaises(install.InstallError) as caught:
            install.verify(self.target)
        self.assertIn("does not parse", str(caught.exception))

    def test_an_unimportable_package_fails(self):
        with open(os.path.join(self.target, "harness", "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write("raise RuntimeError('boom')\n")
        with self.assertRaises(install.InstallError) as caught:
            install.verify(self.target)
        self.assertIn("does not import", str(caught.exception))


class TestDownload(unittest.TestCase):
    def test_rejects_a_suspicious_ref(self):
        for ref in ("../main", "-x", "a/b"):
            with self.assertRaises(install.InstallError):
                install.download(ref)

    def test_tries_the_tag_url_after_a_404_and_reports_both(self):
        import urllib.error

        seen = []

        def opener(request, timeout=None):  # noqa: ARG001 - signature parity with urlopen
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)

        original = install.urllib.request.urlopen if hasattr(install, "urllib") else None
        import urllib.request

        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            with self.assertRaises(install.InstallError) as caught:
                install.download("nope")
        finally:
            urllib.request.urlopen = original

        self.assertEqual(len(seen), 2)
        self.assertIn("/heads/nope.zip", seen[0])
        self.assertIn("/tags/nope.zip", seen[1])
        self.assertIn("HTTP 404", str(caught.exception))


class TestMain(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pyto-install-main-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.zip_path = os.path.join(self.base, "archive.zip")
        with open(self.zip_path, "wb") as handle:
            handle.write(make_zip(VALID).read())

    def run_main(self, *argv, expect=None):
        stream = io.StringIO()
        original = sys.stdout
        sys.stdout = stream
        try:
            code = install.main(list(argv))
        finally:
            sys.stdout = original
        if expect is not None:
            self.assertEqual(code, expect, stream.getvalue())
        return code, stream.getvalue()

    def test_installs_from_a_local_zip(self):
        target = os.path.join(self.base, "app")
        code, output = self.run_main("--zip", self.zip_path, "--into", target, "--no-setup", expect=0)
        self.assertTrue(os.path.isfile(os.path.join(target, "run.py")))
        self.assertIn("Installed to", output)
        self.assertIn("runpy.run_path", output, "the next steps must be paste-ready")
        self.assertNotIn("Traceback", output)

    def test_a_second_run_is_an_update(self):
        target = os.path.join(self.base, "app")
        self.run_main("--zip", self.zip_path, "--into", target, "--no-setup", expect=0)
        _code, output = self.run_main("--zip", self.zip_path, "--into", target, "--no-setup", expect=0)
        self.assertIn("updating the existing install", output)

    def test_refuses_an_unrelated_directory(self):
        target = os.path.join(self.base, "notmine")
        os.makedirs(target)
        with open(os.path.join(target, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("mine")
        code, _output = self.run_main("--zip", self.zip_path, "--into", target, expect=2)
        self.assertEqual(code, 2)
        self.assertTrue(os.path.isfile(os.path.join(target, "notes.txt")), "must not delete anything")

    def test_force_overrides_the_refusal(self):
        target = os.path.join(self.base, "notmine")
        os.makedirs(target)
        with open(os.path.join(target, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("mine")
        self.run_main("--zip", self.zip_path, "--into", target, "--force", "--no-setup", expect=0)
        self.assertTrue(os.path.isfile(os.path.join(target, "run.py")))

    def test_missing_zip_is_a_usage_error(self):
        self.run_main("--zip", os.path.join(self.base, "nope.zip"), expect=2)

    def test_a_corrupt_zip_is_reported_not_raised(self):
        broken = os.path.join(self.base, "broken.zip")
        with open(broken, "wb") as handle:
            handle.write(b"this is not a zip file")
        code, _output = self.run_main("--zip", broken, "--into", os.path.join(self.base, "x"))
        self.assertEqual(code, 1)

    def test_no_verify_skips_the_import_check(self):
        from unittest import mock

        target = os.path.join(self.base, "app")
        with mock.patch.object(install, "verify", side_effect=AssertionError("verify must not run")):
            _code, output = self.run_main(
                "--zip", self.zip_path, "--into", target, "--no-verify", "--no-setup", expect=0
            )
        self.assertNotIn("verify:", output)

    def test_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as caught:
            install.main(["--help"])
        self.assertEqual(caught.exception.code, 0)


class TestDefaults(unittest.TestCase):
    def test_points_at_the_real_repository(self):
        self.assertEqual(install.REPO, "unkloud/pyto-agent")
        self.assertEqual(install.DEFAULT_REF, "main")
        self.assertIn("{repo}", install.ARCHIVE)
        self.assertIn("{ref}", install.ARCHIVE)

    def test_next_steps_mention_the_state_directory(self):
        text = install.next_steps("pyto-agent")
        self.assertIn("~/.pyto_harness", text)
        self.assertIn("never touched by an update", text)

    def test_start_line_names_the_absolute_directory_and_one_command(self):
        line = install.start_line("pyto-agent")
        self.assertIn(repr(os.path.abspath("pyto-agent")), line)
        self.assertEqual(line.count("runpy.run_path"), 1)
        self.assertIn("sys.argv = ['run.py']", line)

    def test_placeholder_keys_are_recognised(self):
        for value in ("sk-REPLACE-ME", "your-key-here", "", None, "sk-", "<set>"):
            self.assertTrue(install.is_placeholder_key(value), value)
        self.assertFalse(install.is_placeholder_key("sk-0123456789abcdef"))


# --------------------------------------------------------------------------------------
# The one-stop setup phase
# --------------------------------------------------------------------------------------

_REPO_ZIP = None


def repo_zip_bytes() -> bytes:
    """This repository as a GitHub-shaped archive, built once (offline, in memory).

    The setup phase runs the *installed* harness's own doctor, so the tests need a real
    tree rather than the four-file stub: everything except ``tests/`` goes in, which keeps
    extraction fast while the doctor still sees the code it audits.
    """
    global _REPO_ZIP
    if _REPO_ZIP is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for base, dirs, files in os.walk(ROOT):
                dirs[:] = [name for name in dirs if name not in install.SKIP_DIRS | {"tests"}]
                for name in sorted(files):
                    if name.endswith(install.SKIP_SUFFIXES):
                        continue
                    full = os.path.join(base, name)
                    archive.write(full, "pyto-agent-main/" + os.path.relpath(full, ROOT))
        _REPO_ZIP = buffer.getvalue()
    return _REPO_ZIP


class PathScriptMock(MockProvider):
    """A mock whose answer depends on the request path.

    ``MockProvider`` scripts responses by call order; the 404 -> ``/v1`` fallback needs a
    server that 404s ``/chat/completions`` and answers ``/v1/chat/completions``, so this
    subclass keys the spec on the path the handler already recorded.
    """

    def __init__(self, by_path, **kwargs):
        self.by_path = dict(by_path)
        super().__init__(list(by_path.values()), **kwargs)

    def _spec_for(self, index, body):
        with self.lock:
            path = self.requests[index]["path"] if index < len(self.requests) else ""
        spec = self.by_path.get(path)
        if spec is None:
            return {"status": 404, "json": {"error": {"message": "no such endpoint: " + path}}}
        return spec(body) if callable(spec) else spec


class SetupTestCase(unittest.TestCase):
    """An install target, a private config/state/workspace, and no network but the mock."""

    KEY = "sk-test-key-0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="pyto-install-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, "pyto-agent")
        self.config_path = os.path.join(self.tmp, "home", "config.json")
        self.state_dir = os.path.join(self.tmp, "home", "state")
        self.workspace = os.path.join(self.tmp, "workspace")
        self.sessions_dir = os.path.join(self.tmp, "sessions")
        self.zip_path = os.path.join(self.tmp, "archive.zip")
        with open(self.zip_path, "wb") as handle:
            handle.write(repo_zip_bytes())
        # Empty, not absent: the harness treats "" as unset, and a real DEEPSEEK_API_KEY in
        # the developer's environment must not leak into these tests.
        self.env = mock.patch.dict(
            os.environ,
            {
                "PYTO_HARNESS_CONFIG": self.config_path,
                "PYTO_HARNESS_STATE_DIR": self.state_dir,
                "PYTO_HARNESS_WORKSPACE": self.workspace,
                "PYTO_HARNESS_SESSIONS_DIR": self.sessions_dir,
                "DEEPSEEK_API_KEY": "",
                "OPENAI_API_KEY": "",
                "PYTO_HARNESS_API_KEY": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    # -- helpers -------------------------------------------------------------------

    def base_args(self, *extra):
        return ("--zip", self.zip_path, "--into", self.target) + tuple(extra)

    def run_install(self, *argv, expect=None, stdin=""):
        """Run ``install.main`` with stdout/stderr captured and stdin pinned to a non-tty."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = install.main(list(argv))
        if expect is not None:
            self.assertEqual(code, expect, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def mock_provider(self, *script):
        provider = MockProvider(list(script) or [text_response("ok")])
        self.addCleanup(provider.close)
        return provider

    def path_mock(self, by_path):
        provider = PathScriptMock(by_path)
        self.addCleanup(provider.close)
        return provider

    def write_config(self, payload):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return payload

    def read_config(self):
        with open(self.config_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def assert_private_mode(self, path):
        if os.name != "posix":  # pragma: no cover - the suite runs on POSIX
            return
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, "{} is not 0600".format(path))

    # -- the tests -----------------------------------------------------------------

    def test_no_key_without_a_tty_finishes_with_the_single_command(self):
        code, out, err = self.run_install(*self.base_args(), expect=0)
        self.assertIn("no API key", out)
        self.assertIn("Next step - one command", out)
        self.assertIn(install.start_line(self.target), out)
        self.assertEqual(out.count("runpy.run_path"), 1, "exactly one thing to paste")
        self.assertIn("doctor:", out)
        self.assertFalse(os.path.exists(self.config_path), "no key -> no config file")
        self.assertNotIn("Traceback", out + err)

    def test_api_key_is_validated_saved_0600_and_never_printed(self):
        provider = self.mock_provider(text_response("ok"))
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base, "--model", "mock-model"),
            expect=0,
        )
        payload = self.read_config()
        self.assertEqual(payload["api_key"], self.KEY)
        self.assertEqual(payload["api_base"], provider.api_base)
        self.assertEqual(payload["model"], "mock-model")
        self.assert_private_mode(self.config_path)
        self.assertTrue(provider.requests, "the key must be proven with a real request")
        self.assertIn("doctor:", out)
        self.assertIn("Next step - one command", out)
        self.assertIn("<set:{} chars".format(len(self.KEY)), out)
        for text in (out, err):
            self.assertNotIn(self.KEY, text)
        launcher = os.path.join(self.target, "start.py")
        self.assertTrue(os.path.isfile(launcher), "start.py must be written")

    def test_a_rejected_key_is_asked_for_three_times_then_gives_up(self):
        provider = self.mock_provider(error_response(401, "invalid api key"))
        typed = []

        def fake_hidden(prompt):
            typed.append(prompt)
            return "sk-wrong-key-{}".format(len(typed))

        with mock.patch.object(install, "can_prompt", return_value=True), mock.patch.object(
            install, "read_hidden", side_effect=fake_hidden
        ):
            code, out, err = self.run_install(*self.base_args("--api-base", provider.api_base), expect=1)
        self.assertEqual(len(typed), install.KEY_ATTEMPTS)
        self.assertIn("rejected", err)
        self.assertFalse(os.path.exists(self.config_path), "a rejected key is never saved")
        for index in range(1, install.KEY_ATTEMPTS + 1):
            self.assertNotIn("sk-wrong-key-{}".format(index), out + err)

    def test_an_explicit_rejected_key_exits_1_without_writing(self):
        provider = self.mock_provider(error_response(403, "forbidden"))
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base), expect=1
        )
        self.assertFalse(os.path.exists(self.config_path))
        self.assertNotIn(self.KEY, out + err)

    def test_a_404_falls_back_to_the_v1_variant(self):
        provider = self.path_mock({"/v1/chat/completions": text_response("ok")})
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base), expect=0
        )
        self.assertEqual(self.read_config()["api_base"], provider.api_base + "/v1")
        paths = [record["path"] for record in provider.requests]
        self.assertEqual(paths[0], "/chat/completions")
        self.assertIn("/v1/chat/completions", paths)

    def test_a_404_with_no_working_variant_writes_nothing(self):
        provider = self.path_mock({})  # every path 404s
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base), expect=1
        )
        self.assertIn("404", err)
        self.assertFalse(os.path.exists(self.config_path))

    def test_no_setup_does_no_key_work_and_prints_the_old_steps(self):
        with mock.patch.object(install, "setup", side_effect=AssertionError("setup must not run")):
            code, out, err = self.run_install(*self.base_args("--no-setup"), expect=0)
        self.assertIn("Next, in this order", out)
        self.assertIn("--init", out)
        self.assertIn("--doctor", out)
        self.assertEqual(out.count("runpy.run_path"), 4, "the old multi-step text is unchanged")
        self.assertFalse(os.path.exists(self.config_path))
        self.assertFalse(os.path.exists(os.path.join(self.target, "start.py")))

    def test_reconfigure_asks_again_and_keeps_the_other_fields(self):
        self.write_config(
            {
                "api_key": "sk-old-key-0123456789abcdef",
                "workspace": "~/elsewhere",
                "sessions_dir": "~/elsewhere/sessions",
                "extra_headers": {"x-org": "acme"},
                "max_turns": 3,
                "model": "old-model",
            }
        )
        provider = self.mock_provider(text_response("ok"))
        new_key = "sk-new-key-fedcba9876543210"

        def fake_hidden(prompt):
            return new_key

        with mock.patch.object(install, "can_prompt", return_value=True), mock.patch.object(
            install, "read_hidden", side_effect=fake_hidden
        ):
            code, out, err = self.run_install(
                *self.base_args("--reconfigure", "--api-base", provider.api_base, "--model", "mock-model"),
                expect=0,
            )
        payload = self.read_config()
        self.assertEqual(payload["api_key"], new_key)
        self.assertEqual(payload["workspace"], "~/elsewhere")
        self.assertEqual(payload["sessions_dir"], "~/elsewhere/sessions")
        self.assertEqual(payload["extra_headers"], {"x-org": "acme"})
        self.assertEqual(payload["max_turns"], 3)

    def test_an_existing_working_key_is_kept_and_the_file_is_left_alone(self):
        provider = self.mock_provider(text_response("ok"))
        self.write_config({"api_key": self.KEY, "api_base": provider.api_base, "model": "mock-model"})
        before = open(self.config_path, "r", encoding="utf-8").read()
        code, out, err = self.run_install(*self.base_args(), expect=0)
        self.assertIn("keeping the API key already in", out)
        self.assertEqual(open(self.config_path, "r", encoding="utf-8").read(), before)
        self.assertTrue(provider.requests, "the stored key is re-checked with a real request")
        self.assertNotIn(self.KEY, out + err)

    def test_a_placeholder_key_in_the_file_is_not_treated_as_configured(self):
        self.write_config({"api_key": "sk-REPLACE-ME", "api_base": "http://127.0.0.1:1"})
        with mock.patch("builtins.input", side_effect=AssertionError("--yes must never prompt")):
            code, out, err = self.run_install(*self.base_args("--yes"), expect=0)
        self.assertIn("no API key", out)
        self.assertEqual(self.read_config()["api_key"], "sk-REPLACE-ME")

    def test_yes_never_blocks_on_input(self):
        with mock.patch.object(install, "can_prompt", return_value=True), mock.patch(
            "builtins.input", side_effect=AssertionError("--yes must never prompt")
        ), mock.patch.object(install, "read_hidden", side_effect=AssertionError("--yes must never prompt")):
            code, out, err = self.run_install(*self.base_args("--yes"), expect=0)
        self.assertFalse(os.path.exists(self.config_path))
        self.assertIn("Next step - one command", out)

    def test_yes_saves_the_key_even_when_the_endpoint_is_unreachable(self):
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", "http://127.0.0.1:1", "--yes"), expect=0
        )
        self.assertEqual(self.read_config()["api_key"], self.KEY)
        self.assertIn("saving the key unverified", out)

    def test_a_network_failure_with_save_anyway_still_writes_the_config(self):
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", "http://127.0.0.1:1", "--save-anyway"),
            expect=0,
        )
        self.assertEqual(self.read_config()["api_key"], self.KEY)
        self.assertIn("could not reach", out)
        self.assertNotIn(self.KEY, out + err)

    def test_a_network_failure_without_save_anyway_writes_nothing(self):
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", "http://127.0.0.1:1"), expect=0
        )
        self.assertFalse(os.path.exists(self.config_path))
        self.assertIn("not saving the key", out)

    def test_no_network_saves_without_touching_the_network(self):
        provider = self.mock_provider(text_response("ok"))
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base, "--no-network"), expect=0
        )
        self.assertEqual(self.read_config()["api_key"], self.KEY)
        self.assertEqual(provider.requests, [], "--no-network must not send anything")

    def test_key_file_is_read_and_used(self):
        key_file = os.path.join(self.tmp, "key.txt")
        with open(key_file, "w", encoding="utf-8") as handle:
            handle.write(self.KEY + "\n")
        provider = self.mock_provider(text_response("ok"))
        code, out, err = self.run_install(
            *self.base_args("--key-file", key_file, "--api-base", provider.api_base), expect=0
        )
        self.assertEqual(self.read_config()["api_key"], self.KEY)
        self.assertNotIn(self.KEY, out + err)

    def test_an_environment_key_is_used_without_prompting(self):
        provider = self.mock_provider(text_response("ok"))
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}):
            with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
                code, out, err = self.run_install(
                    *self.base_args("--api-base", provider.api_base), expect=0
                )
        self.assertEqual(self.read_config()["api_key"], self.KEY)

    def test_an_unreadable_config_is_backed_up_before_it_is_replaced(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        provider = self.mock_provider(text_response("ok"))
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base), expect=0
        )
        self.assertIn("not valid JSON", out)
        self.assertEqual(self.read_config()["api_key"], self.KEY)
        self.assertTrue(os.path.isfile(self.config_path + ".bak"))
        self.assert_private_mode(self.config_path + ".bak")

    def test_skip_fixes_checks_but_does_not_repair(self):
        provider = self.mock_provider(text_response("ok"))
        code, out, err = self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base, "--skip-fixes"), expect=0
        )
        self.assertIn("0 fixed", out)
        self.assertIn("--skip-fixes", out)
        self.assertFalse(os.path.isdir(self.workspace), "no fix ran, so nothing was created")

    def test_the_doctor_writes_the_workspace_documents(self):
        provider = self.mock_provider(text_response("ok"))
        self.run_install(
            *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base), expect=0
        )
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "SHORTCUTS.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "PYTO_LIBS.md")))

    def _launch_case(self, flag, expected_tail):
        provider = self.mock_provider(text_response("ok"))
        calls = []

        def fake_run_path(path, run_name=None):
            calls.append((path, run_name, list(sys.argv)))
            return {"__name__": "__main__"}

        saved = list(sys.argv)
        with mock.patch("runpy.run_path", side_effect=fake_run_path):
            code, out, err = self.run_install(
                *self.base_args("--api-key", self.KEY, "--api-base", provider.api_base) + flag, expect=0
            )
        self.assertEqual(sys.argv, saved, "sys.argv must be restored after the launch")
        self.assertEqual(len(calls), 1)
        path, run_name, argv = calls[0]
        self.assertEqual(path, os.path.join(os.path.abspath(self.target), "run.py"))
        self.assertEqual(run_name, "__main__")
        self.assertEqual(argv, [path] + expected_tail)
        self.assertIn("starting:", out)
        self.assertEqual(self.read_config()["api_key"], self.KEY)

    def test_chat_launches_the_installed_run_py(self):
        self._launch_case(("--chat",), [])

    def test_ui_launches_the_installed_run_py_with_ui(self):
        self._launch_case(("--ui",), ["--ui"])

    def test_task_launches_the_installed_run_py_with_the_task(self):
        self._launch_case(("--task", "rename my screenshots"), ["rename my screenshots"])

    def test_start_py_is_a_launcher_with_the_chdir_and_runpy_lines(self):
        self.run_install(*self.base_args("--yes"), expect=0)
        path = os.path.join(self.target, "start.py")
        self.assertTrue(os.path.isfile(path))
        text = open(path, "r", encoding="utf-8").read()
        self.assertIn("os.chdir(", text)
        self.assertIn('runpy.run_path("run.py", run_name="__main__")', text)
        self.assertIn("sys.argv = [os.path.join(HERE, \"run.py\")] + sys.argv[1:]", text)

    def test_setup_leaves_the_install_directory_with_run_py(self):
        self.run_install(*self.base_args("--yes"), expect=0)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "run.py")))
        self.assertTrue(os.path.isdir(os.path.join(self.target, "harness")))


class TestForcedPrompt(unittest.TestCase):
    """``--ask`` (and Pyto detection) must reach the user even without a tty.

    Pyto's console has no tty, so ``sys.stdin.isatty()`` is false on the target device;
    without these paths the installer would fall back to "edit the JSON by hand", which is
    exactly the complaint that produced the one-stop flow.  This class is standalone: it
    does not inherit the other setup cases, which would otherwise run again here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pyto-install-ask-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, "app")
        self.config_path = os.path.join(self.tmp, "home", "config.json")
        self.zip_path = os.path.join(self.tmp, "archive.zip")
        with open(self.zip_path, "wb") as handle:
            handle.write(make_zip(VALID).read())
        self.env = mock.patch.dict(
            os.environ,
            {
                "PYTO_HARNESS_CONFIG": self.config_path,
                "PYTO_HARNESS_STATE_DIR": os.path.join(self.tmp, "home"),
                "DEEPSEEK_API_KEY": "",
                "OPENAI_API_KEY": "",
                "PYTO_HARNESS_API_KEY": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_install(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = install.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_ask_prompts_on_a_piped_stdin_and_saves_the_key_0600(self):
        provider = MockProvider([text_response("ok")])
        self.addCleanup(provider.close)
        key = "sk-forced-prompt-canary-0123456789"
        code, out, _err = self.run_install(
            "--zip", self.zip_path, "--into", self.target, "--api-base", provider.api_base,
            "--ask", stdin=key + "\n",
        )
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.config_path), out)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode), 0o600)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["api_key"], key)
        self.assertNotIn(key, out, "the key must never be echoed")

    def test_without_ask_a_non_tty_run_does_not_prompt(self):
        code, out, _err = self.run_install(
            "--zip", self.zip_path, "--into", self.target, stdin="sk-should-not-be-read\n"
        )
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.config_path), "no key should be written")
        self.assertIn("no API key", out)

    def test_can_prompt_is_true_when_pyto_is_present(self):
        class FakeIos:
            @staticmethod
            def is_pyto():
                return True

        self.assertTrue(install.can_prompt(FakeIos))

    def test_can_prompt_is_false_without_tty_or_pyto(self):
        class FakeIos:
            @staticmethod
            def is_pyto():
                return False

        with mock.patch.object(sys, "stdin", io.StringIO("")):
            self.assertFalse(install.can_prompt(FakeIos))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

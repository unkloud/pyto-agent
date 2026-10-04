"""Tests for install.py — the GitHub installer.

Everything here is offline: the network path is exercised through ``--zip`` and through
an injected opener, so the suite never calls GitHub.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import install  # noqa: E402


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
        code, output = self.run_main("--zip", self.zip_path, "--into", target, expect=0)
        self.assertTrue(os.path.isfile(os.path.join(target, "run.py")))
        self.assertIn("Installed to", output)
        self.assertIn("runpy.run_path", output, "the next steps must be paste-ready")
        self.assertNotIn("Traceback", output)

    def test_a_second_run_is_an_update(self):
        target = os.path.join(self.base, "app")
        self.run_main("--zip", self.zip_path, "--into", target, expect=0)
        _code, output = self.run_main("--zip", self.zip_path, "--into", target, expect=0)
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
        self.run_main("--zip", self.zip_path, "--into", target, "--force", expect=0)
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
                "--zip", self.zip_path, "--into", target, "--no-verify", expect=0
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
